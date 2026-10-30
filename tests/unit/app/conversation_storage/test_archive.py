# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.archive import (
    ArchiveInstall,
    CheckpointInstall,
    EventInstall,
    ContextReset,
    install_archive,
)
from swe.app.conversation_storage.bundle import BlockExtent, BundleIndex
from swe.app.conversation_storage.errors import ConflictError, IntegrityError


def test_archive_plan_preserves_boundary_identity_and_counts():
    plan = ArchiveInstall("boundary", "archive", 2, 3, "first", "last")
    assert ArchiveInstall.from_dict(plan.to_dict()) == plan
    with pytest.raises(ValueError):
        ArchiveInstall("boundary", "archive", 0, 0, "", "")


def test_checkpoint_and_event_plan_validation():
    assert CheckpointInstall("checkpoint", 0, 1, 2, 3).revision == 1
    assert EventInstall("events", 1, 2).last == 2
    with pytest.raises(ValueError):
        CheckpointInstall("checkpoint", 0, 2, 0, 0)
    with pytest.raises(ValueError):
        EventInstall("events", 3, 2)
    assert ContextReset(2, 1).base_checkpoint_epoch == 1


def context():
    request = SimpleNamespace(session_id=1, chat_id=2, epoch=0)
    session = {"id": 1, "context_epoch": 0, "history_high": 10}
    chat = {
        "id": 2,
        "epoch": 0,
        "event_high": 2,
        "checkpoint_revision": 0,
        "history_high": 4,
        "next_boundary_order": 1,
    }
    return request, (session, chat, None)


async def test_event_batch_must_continue_current_watermark():
    db = SimpleNamespace(execute=AsyncMock())
    request, associations = context()
    index = BundleIndex(
        "bundle",
        100,
        {},
        {"events": BlockExtent("events", 50, 20)},
    )
    with pytest.raises(ConflictError, match="continuous"):
        await install_archive(
            db,
            request,
            {"id": 3},
            index,
            associations,
            events=EventInstall("events", 4, 5),
        )
    db.execute.assert_not_awaited()


async def test_reset_advances_distinct_epochs_without_creating_compact_boundary():
    db = SimpleNamespace(execute=AsyncMock(return_value=1))
    request, associations = context()
    await install_archive(
        db,
        request,
        {"id": 3},
        BundleIndex("bundle", 100, {}, {}),
        associations,
        reset=ContextReset(0, 0),
    )
    queries = [call.args[0] for call in db.execute.await_args_list]
    assert any("context_epoch=context_epoch+1" in query for query in queries)
    assert any("epoch=epoch+1" in query for query in queries)
    assert not any("boundary_key=" in query for query in queries)
    assert not any("DELETE " in query for query in queries)


async def test_checkpoint_block_and_epoch_are_validated_before_reference_install():
    db = SimpleNamespace(execute=AsyncMock())
    request, associations = context()
    with pytest.raises(IntegrityError, match="block"):
        await install_archive(
            db,
            request,
            {"id": 3},
            BundleIndex("bundle", 100, {}, {}),
            associations,
            checkpoint=CheckpointInstall("missing", 0, 1, 2, 10),
        )
    db.execute.assert_not_awaited()


def test_archive_occurrences_require_boundary_even_without_other_operations():
    from swe.app.conversation_storage.publications import CommitPlan
    from swe.app.conversation_storage.message_installs import MessageInstall

    with pytest.raises(ValueError, match="boundary"):
        CommitPlan(
            messages=(
                MessageInstall("m", "archive", "user", archive_chat_id=2),
            ),
        ).serialize()


async def test_duplicate_prune_ids_use_matching_placeholders_and_parameters():
    from swe.app.conversation_storage.archive import _prune

    db = SimpleNamespace(
        fetch_all=AsyncMock(
            return_value=[{"id": 1, "session_id": 1, "layer": "memory"}],
        ),
        execute=AsyncMock(),
    )
    await _prune(db, SimpleNamespace(session_id=1), (1, 1))
    for call in (*db.fetch_all.await_args_list, *db.execute.await_args_list):
        assert call.args[0].count("%s") == len(call.args[1])


async def test_compact_reservation_requires_replacement_root(tmp_path):
    from dataclasses import replace
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationStore,
    )
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )

    service = PublicationStore(PublicationDatabase(), BundleStore(tmp_path))
    with pytest.raises(ValueError, match="root"):
        await service.reserve(replace(request(), kind="compact"), CommitPlan())


async def test_canonical_archive_payloads_reject_mismatched_declared_metadata(
    tmp_path,
):
    from dataclasses import replace
    from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationStore,
    )
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )

    db = PublicationDatabase()
    db.chat.update(
        epoch=1,
        event_high=0,
        checkpoint_revision=0,
        next_boundary_order=1,
    )
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = replace(request(), epoch=1)
    plan = CommitPlan(checkpoint=CheckpointInstall("checkpoint", 0, 1, 0, 0))
    await service.reserve(operation, plan)
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("checkpoint", "checkpoint", b'{"revision":999}')],
    )
    db.queries.clear()
    with pytest.raises(IntegrityError, match="checkpoint"):
        await service.commit(operation.operation_key)
    assert not any("FOR UPDATE" in query for query in db.queries)


async def test_event_payload_must_contain_the_claimed_contiguous_range(
    tmp_path,
):
    from dataclasses import replace
    from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationStore,
    )
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )

    db = PublicationDatabase()
    db.chat.update(
        epoch=1,
        event_high=0,
        checkpoint_revision=0,
        next_boundary_order=1,
    )
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = replace(request(), epoch=1)
    await service.reserve(
        operation,
        CommitPlan(events=EventInstall("events", 1, 1)),
    )
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("events", "events", b"[]")],
    )
    with pytest.raises(IntegrityError, match="event"):
        await service.commit(operation.operation_key)


def test_archive_boundary_source_ids_match_occurrence_order():
    from swe.app.conversation_storage.publications import CommitPlan
    from swe.app.conversation_storage.message_installs import MessageInstall

    with pytest.raises(ValueError, match="source identities"):
        CommitPlan(
            messages=(
                MessageInstall(
                    "m",
                    "archive",
                    "user",
                    source_msg_key="actual",
                    archive_chat_id=2,
                ),
            ),
            archive=ArchiveInstall("b", "batch", 1, 1, "wrong", "wrong"),
        ).serialize()


async def test_historical_turn_locks_are_complete_and_ascending_before_publication(
    tmp_path,
):
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.publications import PublicationStore
    from tests.unit.app.conversation_storage.test_publications import request

    turns = {
        1: {"id": 1, "chat_id": 5, "session_id": 1},
        3: {"id": 3, "chat_id": 2, "session_id": 1},
    }
    calls = []

    async def fetch(query, params):
        calls.append((query, params))
        if "sessions" in query:
            return {"id": 1}
        if "chats" in query:
            return {
                "id": params[0],
                "session_id": 1,
                "tenant_key": b"t",
                "source_key": b"s",
                "agent_key": b"a",
            }
        return turns[params[0]]

    service = PublicationStore(
        SimpleNamespace(fetch_one=fetch),
        BundleStore(tmp_path),
    )
    await service._lock_associations(request(), archive_turns={1: turns[1]})
    assert [args[0] for query, args in calls if "turns" in query] == [1, 3]
    assert [args[0] for query, args in calls if "chats" in query] == [2, 5]


async def test_archive_turn_snapshot_rejects_original_chat_mismatch(tmp_path):
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.message_installs import MessageInstall
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationStore,
    )
    from tests.unit.app.conversation_storage.test_publications import request

    db = SimpleNamespace(
        fetch_one=AsyncMock(
            return_value={"id": 7, "session_id": 1, "chat_id": 5},
        ),
    )
    service = PublicationStore(db, BundleStore(tmp_path))
    plan = CommitPlan(
        messages=(
            MessageInstall(
                "m",
                "archive",
                "user",
                archive_chat_id=2,
                origin_chat_id=6,
                turn_id=7,
            ),
        ),
    )
    with pytest.raises(IntegrityError, match="origin"):
        await service._archive_turns(request(), plan)


def test_archive_chat_must_belong_to_historical_session():
    from swe.app.conversation_storage.publications import PublicationStore
    from tests.unit.app.conversation_storage.test_publications import request

    with pytest.raises(IntegrityError, match="Session"):
        PublicationStore._validate_archive_chat(
            request(),
            {
                "id": 5,
                "session_id": 99,
                "tenant_key": b"t",
                "source_key": b"s",
                "agent_key": b"a",
            },
        )


async def test_direct_install_rejects_archive_occurrences_without_boundary(
    tmp_path,
):
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.message_installs import MessageInstall
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationStore,
    )
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )

    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    row = {**request().fields(), "id": 4}
    with pytest.raises(IntegrityError, match="boundary"):
        await service._install(
            row,
            CommitPlan(
                messages=(
                    MessageInstall("m", "archive", "user", archive_chat_id=2),
                ),
            ),
            BundleIndex(
                "bundle",
                100,
                {},
                {"m": BlockExtent("archive", 50, 10)},
            ),
            associations=(db.session, db.chat, db.turn),
        )
    assert not db.messages
