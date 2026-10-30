# -*- coding: utf-8 -*-
"""Archive transactions verified against disposable real MySQL schemas."""

from dataclasses import replace
import json

from agentscope.message import Msg
import pytest

from swe.agents.memory.chat_checkpoint import CheckpointEvent, CheckpointRecord
from swe.app.conversation_storage.archive import (
    ArchiveInstall,
    CheckpointInstall,
    EventInstall,
    ContextReset,
)
from swe.app.conversation_storage.bundle import (
    BundleStore,
    BundleBlock,
    new_operation_key,
)
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.message_installs import MessageInstall
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationRequest,
    PublicationStore,
)
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    UnknownCommit,
)
from tests.integrated.conversation_storage.test_schema import (
    insert_session,
    insert_chat,
    insert_row,
)
from tests.integrated.conversation_storage.test_publications_mysql import (
    InstallFailureDatabase,
)

STAMP = "2026-10-30T00:00:01Z"


def payload(key, role, content):
    value = Msg(role, content, role, timestamp=STAMP).to_dict()
    value["id"] = key
    value["metadata"]["unknown_original_field"] = {"preserve": True}
    return value


def block(name, kind, value):
    return BundleBlock(name, kind, json.dumps(value).encode())


async def seed(db, root):
    session = await insert_session(db)
    first = await insert_chat(db, session, b"first", epoch=1)
    second = await insert_chat(
        db,
        session,
        b"second",
        registry_order=2,
        epoch=1,
    )
    return session, first, second, PublicationStore(db, BundleStore(root))


async def operation(db, session, chat, kind="state"):
    s = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (session,),
    )
    c = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE id=%s",
        (chat,),
    )
    return PublicationRequest(
        new_operation_key(),
        WorkspaceScope("tenant", "source", "agent"),
        kind,
        "owner",
        s["generation"],
        session_id=session,
        chat_id=chat,
        base_state_revision=s["state_revision"],
        base_history_revision=c["history_revision"],
        epoch=c["epoch"],
    )


async def online(db, service, session, chat):
    request = await operation(db, session, chat)
    await service.reserve(
        request,
        CommitPlan(
            recovery_block="root",
            messages=(
                MessageInstall(
                    "question",
                    "memory",
                    "user",
                    source_msg_key="q",
                ),
                MessageInstall(
                    "answer",
                    "memory",
                    "assistant",
                    source_msg_key="a",
                ),
            ),
        ),
    )
    await service.write_bundle(
        request.operation_key,
        [
            block("root", "recovery", {"all_modules": {"keep": True}}),
            block("question", "message", payload("q", "user", "question")),
            block("answer", "message", payload("a", "assistant", "answer")),
        ],
    )
    await service.commit(request.operation_key)
    return [
        row["id"]
        for row in await db.fetch_all(
            "SELECT id FROM swe_conversation_messages ORDER BY id",
        )
    ]


def compact_plan(chat, ids, *, turn_id=None):
    messages = tuple(
        MessageInstall(
            name,
            "archive",
            role,
            source_msg_key=key,
            archive_chat_id=chat,
            turn_id=turn_id,
            message_timestamp=STAMP,
        )
        for name, role, key in [
            ("old-q", "user", "q"),
            ("old-a", "assistant", "a"),
        ]
    )
    return CommitPlan(
        recovery_block="root",
        messages=messages,
        archive=ArchiveInstall(
            "boundary",
            "batch",
            2,
            2,
            "q",
            "a",
            STAMP,
            STAMP,
        ),
        checkpoint=CheckpointInstall("checkpoint", 0, 1, 1, 2),
        events=EventInstall("events", 1, 1),
        prune_ids=tuple(ids),
    )


def compact_blocks(*, checkpoint=None, events=None, batch=None, question=None):
    originals = [
        question or payload("q", "user", "question"),
        payload("a", "assistant", "answer"),
    ]
    record = replace(
        CheckpointRecord.new(chat_id="first", epoch=1),
        revision=1,
        applied_event_sequence=1,
        archived_through="a",
    )
    event = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="messages_archived",
        source_refs=("message:q", "message:a"),
    )
    return [
        block(
            "root",
            "recovery",
            {"all_modules": {"keep": True}, "compacted": True},
        ),
        block("batch", "archive", originals if batch is None else batch),
        block(
            "checkpoint",
            "checkpoint",
            record.to_dict() if checkpoint is None else checkpoint,
        ),
        block(
            "events",
            "events",
            [event.to_dict()] if events is None else events,
        ),
        block("old-q", "archive", originals[0]),
        block("old-a", "archive", originals[1]),
    ]


async def test_compact_checkpoint_events_root_and_visibility_commit_together(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, first, second, service = await seed(db, tmp_path)
    ids = await online(db, service, session, first)
    request = await operation(db, session, first, "compact")
    await service.reserve(request, compact_plan(first, ids + ids))
    await service.write_bundle(request.operation_key, compact_blocks())
    result = await service.commit(request.operation_key)
    publication = await db.fetch_one(
        "SELECT * FROM swe_conversation_publications WHERE id=%s",
        (result["id"],),
    )
    assert (
        publication["boundary_key"] == b"boundary"
        and publication["checkpoint_revision"] == 1
    )
    assert (
        publication["archive_first_order"] == 1
        and publication["archive_last_order"] == 2
    )
    chat = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE id=%s",
        (first,),
    )
    assert (
        chat["event_high"] == 1
        and chat["active_checkpoint_publication_id"] == result["id"]
    )
    assert chat["history_high"] == 2 and chat["next_boundary_order"] == 2
    other = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE id=%s",
        (second,),
    )
    assert (
        other["history_high"] == 0
        and other["active_checkpoint_publication_id"] is None
    )
    rows = await db.fetch_all(
        "SELECT * FROM swe_conversation_messages ORDER BY id",
    )
    assert [row["current_visible"] for row in rows[:2]] == [0, 0]
    assert [row["archive_chat_id"] for row in rows[2:]] == [first, first]
    assert len(rows) == 4
    archived = service.bundles.read_extent(
        result["relative_path"].decode(),
        rows[2]["byte_offset"],
        rows[2]["byte_length"],
        expected_length=result["byte_length"],
    )
    assert json.loads(archived)["metadata"]["unknown_original_field"] == {
        "preserve": True,
    }


async def test_reset_advances_epochs_and_keeps_historical_occurrences(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, first, _, service = await seed(db, tmp_path)
    await online(db, service, session, first)
    request = await operation(db, session, first, "reset")
    await service.reserve(
        request,
        CommitPlan(recovery_block="root", reset=ContextReset(0, 1)),
    )
    await service.write_bundle(
        request.operation_key,
        [
            block(
                "root",
                "recovery",
                {"all_modules": {"keep": True}, "cleared": True},
            ),
        ],
    )
    result = await service.commit(request.operation_key)
    s = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (session,),
    )
    c = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE id=%s",
        (first,),
    )
    assert (
        s["context_epoch"] == 1
        and c["epoch"] == 2
        and s["state_revision"] == 2
    )
    assert s["context_usage_invalid"] == 1 and c["next_boundary_order"] == 1
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages",
        )
    )["n"] == 2
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages WHERE current_visible=1",
        )
    )["n"] == 0
    assert result["boundary_key"] is None


async def test_failed_archive_install_rolls_back_all_metadata_and_visibility(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, first, _, service = await seed(db, tmp_path)
    ids = await online(db, service, session, first)
    request = await operation(db, session, first, "compact")
    await service.reserve(request, compact_plan(first, ids))
    await service.write_bundle(request.operation_key, compact_blocks())
    fault = PublicationStore(InstallFailureDatabase(db), BundleStore(tmp_path))
    with pytest.raises(UnknownCommit):
        await fault.commit(request.operation_key)
    chat = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE id=%s",
        (first,),
    )
    assert chat["event_high"] == 0
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages WHERE current_visible=1",
        )
    )["n"] == 2
    assert (await service.get(request.operation_key))["state"] == "prepared"


@pytest.mark.parametrize(
    "damage",
    [
        "checkpoint_schema",
        "checkpoint_epoch",
        "checkpoint_revision",
        "checkpoint_watermark",
        "checkpoint_chat",
        "event_empty",
        "event_gap",
        "event_epoch",
        "event_duplicate",
        "batch",
        "display_count",
        "timestamp",
        "role",
    ],
)
async def test_invalid_immutable_archive_payload_never_installs(
    conversation_test_database,
    tmp_path,
    damage,
):
    db = conversation_test_database
    session, first, _, service = await seed(db, tmp_path)
    ids = await online(db, service, session, first)
    request = await operation(db, session, first, "compact")
    plan = compact_plan(first, ids)
    blocks = compact_blocks()
    values = {item.name: json.loads(item.content) for item in blocks}
    if damage.startswith("checkpoint_"):
        key = damage.removeprefix("checkpoint_")
        values["checkpoint"][
            {
                "schema": "schema_version",
                "watermark": "applied_event_sequence",
                "chat": "chat_id",
            }.get(key, key)
        ] = (
            "other" if key == "chat" else 99
        )
    elif damage == "event_empty":
        values["events"] = []
    elif damage in ("event_gap", "event_epoch"):
        values["events"][0][
            "sequence" if damage == "event_gap" else "epoch"
        ] = 2
    elif damage == "event_duplicate":
        plan = replace(plan, events=EventInstall("events", 1, 2))
        values["events"].append({**values["events"][0], "sequence": 2})
    elif damage == "batch":
        values["batch"][0]["content"] = "different body"
    elif damage == "display_count":
        plan = replace(plan, archive=replace(plan.archive, display_count=3))
    elif damage == "timestamp":
        plan = replace(
            plan,
            archive=replace(
                plan.archive,
                first_timestamp="2026-10-30T00:00:02Z",
            ),
        )
    elif damage == "role":
        values["old-q"]["role"] = "assistant"
        values["batch"][0]["role"] = "assistant"
    await service.reserve(request, plan)
    await service.write_bundle(
        request.operation_key,
        [block(item.name, item.kind, values[item.name]) for item in blocks],
    )
    with pytest.raises(IntegrityError):
        await service.commit(request.operation_key)
    assert (await service.get(request.operation_key))["state"] == "prepared"
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages",
        )
    )["n"] == 2
    assert (
        await db.fetch_one(
            "SELECT state_revision FROM swe_conversation_sessions WHERE id=%s",
            (session,),
        )
    )["state_revision"] == 1


async def test_archive_preserves_turn_originating_in_another_chat_of_shared_session(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, first, second, service = await seed(db, tmp_path)
    ids = await online(db, service, session, first)
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "chat_id": second,
            "session_id": session,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "succeeded",
            "history_status": "completed",
        },
    )
    request = await operation(db, session, first, "compact")
    plan = compact_plan(first, ids, turn_id=turn)
    plan = replace(
        plan,
        messages=tuple(
            replace(item, origin_chat_id=second) for item in plan.messages
        ),
    )
    await service.reserve(request, plan)
    await service.write_bundle(request.operation_key, compact_blocks())
    await service.commit(request.operation_key)
    rows = await db.fetch_all(
        "SELECT turn_id,archive_chat_id,origin_chat_id FROM swe_conversation_messages WHERE layer='archive' ORDER BY archive_order",
    )
    assert [(row["turn_id"], row["archive_chat_id"]) for row in rows] == [
        (turn, first),
        (turn, first),
    ]
    assert [row["origin_chat_id"] for row in rows] == [second, second]


async def test_archive_rejects_cross_session_historical_turn(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, first, _, service = await seed(db, tmp_path)
    ids = await online(db, service, session, first)
    foreign_session = await insert_session(db, external_session_key=b"foreign")
    foreign_chat = await insert_chat(db, foreign_session, b"foreign")
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "chat_id": foreign_chat,
            "session_id": foreign_session,
            "question_msg_key": b"foreign",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "succeeded",
        },
    )
    request = await operation(db, session, first, "compact")
    await service.reserve(request, compact_plan(first, ids, turn_id=turn))
    await service.write_bundle(request.operation_key, compact_blocks())
    with pytest.raises(IntegrityError, match="Session"):
        await service.commit(request.operation_key)
