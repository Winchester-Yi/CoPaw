# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.chat_sharing.models import ChatShareRecord
from swe.app.chat_sharing.service import ChatSharingService
from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.commit_plan import CommitPlan
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    StorageUnavailable,
    UnknownCommit,
)
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.publications import (
    PublicationRequest,
    PublicationStore,
)
from swe.app.conversation_storage.shares import (
    ShareInstall,
    ShareStorage,
    install_share,
    lock_share_selection,
    validate_share_request,
)


def mutation(**changes):
    values = {
        "token": "token",
        "chat_id": "chat",
        "creator_id": "alice",
        "tenant_id": "tenant",
        "snapshot_key": "legacy/key.json",
        "created_at": "2026-10-30T00:00:00Z",
        "block_name": "share",
    }
    return ShareInstall(**{**values, **changes})


def test_share_plan_preserves_legacy_fields_and_rejects_content_or_bad_counts():
    plan = mutation(access_count=7, last_access_at="2026-10-30T01:00:00Z")
    assert ShareInstall.from_dict(plan.to_dict()) == plan
    assert plan.snapshot_key == "legacy/key.json"
    assert mutation(snapshot_key="中" * 512).snapshot_key == "中" * 512
    with pytest.raises(ValueError):
        mutation(access_count=-1)
    with pytest.raises(ValueError):
        ShareInstall.from_dict({**plan.to_dict(), "messages": ["body"]})


def test_commit_plan_share_roundtrip_and_recovery_are_exclusive():
    share = mutation()
    plan = CommitPlan(share=share)
    assert CommitPlan.parse(plan.serialize()) == plan
    with pytest.raises(ValueError, match="share"):
        CommitPlan(share=share, recovery_block="recovery").serialize()


@pytest.mark.parametrize(
    "field",
    [
        "result_ids",
        "chat_metadata",
        "messages",
        "turn_install",
        "archive",
        "checkpoint",
        "events",
        "reset",
        "prune_ids",
    ],
)
def test_share_plan_is_exclusive_with_every_installation_family(field):
    with pytest.raises(ValueError, match="share"):
        CommitPlan(share=mutation(), **{field: object()}).serialize()


def share_request(**changes):
    values = {
        "operation_key": new_operation_key(),
        "scope": WorkspaceScope("tenant", "source", "agent"),
        "kind": "share",
        "owner_key": "share-owner",
        "generation": 0,
        "session_id": 1,
        "chat_id": 2,
    }
    return PublicationRequest(**{**values, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "state"},
        {"turn_id": 3},
        {"session_id": None, "chat_id": None},
        {"scope": WorkspaceScope("other", "source", "agent")},
        {
            "scope": WorkspaceScope("tenant", "", ""),
            "scope_kind": "legacy_share",
            "session_id": None,
            "chat_id": None,
        },
    ],
)
def test_share_request_rejects_kind_turn_scope_and_guessed_workspace(changes):
    with pytest.raises(ValueError, match="share"):
        validate_share_request(share_request(**changes), mutation())


def test_new_share_requires_selected_source_questions():
    with pytest.raises(ValueError, match="selected"):
        validate_share_request(share_request(), mutation())


@pytest.mark.parametrize(
    "turn_changes,occurrences",
    [
        ({"history_status": "failed"}, [{"role_key": b"user", "turn_id": 3}]),
        ({"chat_id": 99}, [{"role_key": b"user", "turn_id": 3}]),
        ({}, []),
        (
            {},
            [
                {"role_key": b"assistant", "turn_id": 3},
                {"role_key": b"user", "turn_id": 3},
            ],
        ),
        ({}, [{"role_key": b"user", "turn_id": 99}]),
    ],
)
async def test_share_selection_rechecks_completed_and_first_visible_occurrence(
    turn_changes,
    occurrences,
):
    turn = {
        "id": 3,
        "question_msg_key": b"question",
        "chat_id": 2,
        "state": "succeeded",
        "history_status": "completed",
        **turn_changes,
    }
    db = SimpleNamespace(
        fetch_all=AsyncMock(side_effect=[[turn], occurrences]),
    )
    with pytest.raises(ConflictError):
        await lock_share_selection(
            db,
            share_request(),
            mutation(selected_question_keys=("question",)),
        )


async def test_share_selection_preserves_source_identity_and_duplicate_occurrences():
    turn = {
        "id": 3,
        "question_msg_key": b"question",
        "chat_id": 2,
        "state": "succeeded",
        "history_status": "completed",
    }
    db = SimpleNamespace(
        fetch_all=AsyncMock(
            side_effect=[
                [turn],
                [
                    {"role_key": b"user", "turn_id": 3},
                    {"role_key": b"assistant", "turn_id": 3},
                ],
            ],
        ),
    )
    plan = mutation(selected_question_keys=("question", "question"))
    assert ShareInstall.from_dict(plan.to_dict()).selected_question_keys == (
        "question",
        "question",
    )
    await lock_share_selection(db, share_request(), plan)
    assert db.fetch_all.call_args_list[0].args[1] == (1, b"question")
    assert all(
        "FOR UPDATE" in call.args[0] for call in db.fetch_all.call_args_list
    )


async def test_archived_anchor_is_not_a_visible_share_question():
    turn = {
        "id": 3,
        "question_msg_key": b"question",
        "chat_id": 2,
        "state": "succeeded",
        "history_status": "completed",
    }
    db = SimpleNamespace(
        fetch_all=AsyncMock(
            side_effect=[
                [turn],
                [{"layer": "anchor", "role_key": b"user", "turn_id": 3}],
                [{"id": 9}],
            ],
        ),
    )
    with pytest.raises(ConflictError, match="visible"):
        await lock_share_selection(
            db,
            share_request(),
            mutation(selected_question_keys=("question",)),
        )


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"deleted_at": datetime(2026, 10, 30)}, ConflictError),
        ({"external_chat_key": b"other"}, ConflictError),
        ({"user_key": b"other"}, ConflictError),
        ({"source_key": b"other"}, IntegrityError),
        ({"session_id": 99}, IntegrityError),
    ],
)
def test_share_commit_revalidates_live_chat_creator_and_scope(changes, error):
    request = share_request()
    session = {
        "id": 1,
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
    }
    chat = {
        **session,
        "id": 2,
        "session_id": 1,
        "deleted_at": None,
        "external_chat_key": b"chat",
        "user_key": b"alice",
        **changes,
    }
    with pytest.raises(error):
        PublicationStore._validate_share_authority(
            request,
            mutation(),
            (session, chat, None),
        )


async def test_share_install_is_parameterized_same_transaction_and_locator_exact(
    tmp_path,
):
    bundles = BundleStore(tmp_path)
    index = bundles.publish(
        new_operation_key(),
        [BundleBlock("share", "share", b"{}")],
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=1))
    request = SimpleNamespace(
        scope=WorkspaceScope("tenant", "source", "agent"),
        scope_kind="workspace",
        session_id=1,
        chat_id=2,
    )
    publication = {"id": 3}
    await install_share(db, request, mutation(), publication, index)
    query, params = db.execute.call_args.args
    assert "INSERT INTO swe_chat_shares" in query
    assert params[-3:] == (3, index.blocks["share"].offset, 2)
    assert params[:5] == (
        "token",
        "chat",
        "alice",
        "tenant",
        "legacy/key.json",
    )


async def test_new_share_service_uses_bundle_backend_and_never_legacy_files(
    tmp_path,
):
    backend = SimpleNamespace(
        create_snapshot=AsyncMock(return_value="record"),
        get_snapshot=AsyncMock(
            return_value={"chat_name": "x", "messages": []},
        ),
    )
    service = ChatSharingService(
        object(),
        tmp_path,
        conversation_shares=backend,
    )
    result = await service.create_snapshot(
        chat_id="chat",
        chat_name="x",
        messages=[
            {
                "id": "q",
                "role": "user",
                "type": "message",
                "content": [{"type": "text", "text": "question"}],
            },
        ],
        selected_turn_ids=["q"],
        turn_statuses={"q": "completed"},
        creator_id="alice",
        tenant_id="tenant",
    )
    assert result == "record"
    assert list(tmp_path.iterdir()) == []
    assert await service.get_snapshot("token") == {
        "chat_name": "x",
        "messages": [],
    }


async def test_legacy_unknown_db_create_never_unlinks_snapshot(tmp_path):
    store = SimpleNamespace(
        create=AsyncMock(side_effect=ConnectionError("lost response")),
    )
    service = ChatSharingService(store, tmp_path)
    with pytest.raises(ConnectionError):
        await service.create_snapshot(
            chat_id="chat",
            chat_name="x",
            messages=[
                {
                    "id": "q",
                    "role": "user",
                    "type": "message",
                    "content": [{"type": "text", "text": "question"}],
                },
            ],
            selected_turn_ids=["q"],
            turn_statuses={"q": "completed"},
            creator_id="alice",
        )
    assert len(list(tmp_path.rglob("*.json"))) == 1


async def test_access_count_requires_confirmed_affected_row():
    db = SimpleNamespace(execute=AsyncMock(return_value=0))
    with pytest.raises(RuntimeError, match="count"):
        await ChatShareStore(db).record_access("token")
    db.execute.return_value = 1
    await ChatShareStore(db).record_access("token")


async def test_new_mode_schema_check_is_read_only_and_refuses_unmigrated_rows():
    names = ("publication_id", "byte_offset", "byte_length")
    db = SimpleNamespace(
        fetch_all=AsyncMock(
            return_value=[
                {"COLUMN_NAME": name, "COLUMN_TYPE": "bigint unsigned"}
                for name in names
            ],
        ),
        fetch_one=AsyncMock(return_value=None),
        execute=AsyncMock(),
    )
    await ChatShareStore(db).verify_locator_schema()
    db.execute.assert_not_awaited()
    db.fetch_one.return_value = {"token": "unmigrated"}
    with pytest.raises(RuntimeError, match="migration"):
        await ChatShareStore(db).verify_locator_schema()
    db.fetch_all.return_value = []
    with pytest.raises(RuntimeError, match="schema"):
        await ChatShareStore(db).verify_locator_schema()
