# -*- coding: utf-8 -*-
import pytest

from swe.app.conversation_storage.migration.importer import (
    display_order_messages,
    extract_messages,
    validate_inventory,
)
from swe.app.conversation_storage.identity import WorkspaceScope


def test_extract_messages_preserves_duplicate_occurrences_and_unknown_turn_status():
    from agentscope.message import Msg

    first = Msg("user", "one", "user")
    first.id = "duplicate"
    second = Msg("assistant", "two", "assistant")
    second.id = "duplicate"
    state = {
        "agent": {"memory": {"content": [first.to_dict(), second.to_dict()]}},
        "turn_states": {
            "old": {
                "message": {
                    "id": "old",
                    "role": "user",
                    "content": "question",
                },
                "chat_id": None,
            },
        },
        "task_messages": [],
        "unknown": {"x": True},
    }
    values = extract_messages(state, context_epoch=0)
    assert [item["payload"]["id"] for item in values] == [
        "duplicate",
        "duplicate",
        "old",
    ]
    assert [item["source_occurrence_no"] for item in values[:2]] == [0, 1]
    assert values[-1]["layer"] == "anchor" and values[-1]["turn_key"] == "old"
    assert "status" not in state["turn_states"]["old"]


def test_display_order_messages_stably_interleaves_error_layer():
    from agentscope.message import Msg

    question = Msg(
        "user",
        "question",
        "user",
        timestamp="2026-10-30T00:00:01Z",
    )
    question.id = "q"
    error = {
        "id": "e",
        "code": "model_call_failed",
        "message": "failed",
        "timestamp": "2026-10-30T00:00:02Z",
    }
    later = Msg("user", "later", "user", timestamp="2026-10-30T00:00:03Z")
    later.id = "later"
    messages = extract_messages(
        {
            "agent": {
                "memory": {"content": [question.to_dict(), later.to_dict()]},
            },
            "model_call_failed_messages": [error],
        },
        context_epoch=0,
    )

    ordered = display_order_messages(messages)

    assert [item["payload"]["id"] for item in ordered] == ["q", "e", "later"]
    assert [item["source_occurrence_no"] for item in ordered] == [0, 0, 1]


def test_display_order_messages_keeps_equal_and_missing_timestamp_order():
    messages = [
        {
            "layer": "memory",
            "payload": {
                "id": "equal-first",
                "timestamp": "2026-10-30T00:00:01Z",
            },
        },
        {
            "layer": "error",
            "payload": {"id": "missing-first"},
        },
        {
            "layer": "memory",
            "payload": {
                "id": "equal-second",
                "timestamp": "2026-10-30T00:00:01Z",
            },
        },
        {
            "layer": "task",
            "payload": {"id": "missing-second", "timestamp": "invalid"},
        },
    ]

    ordered = display_order_messages(messages)

    assert [item["payload"]["id"] for item in ordered] == [
        "equal-first",
        "equal-second",
        "missing-first",
        "missing-second",
    ]


def test_inventory_blockers_cannot_be_overridden_with_ready_boolean():
    with pytest.raises(ValueError, match="block"):
        validate_inventory(
            {
                "version": 1,
                "ready": True,
                "blockers": [{"code": "broken"}],
                "workspaces": [],
                "shares": [],
            },
        )


def test_unknown_message_schema_is_rejected_instead_of_skipped():
    with pytest.raises(ValueError, match="memory"):
        extract_messages(
            {"agent": {"memory": {"content": ["not-a-message"]}}},
            context_epoch=0,
        )


def test_offline_target_rejects_business_schema_and_untrusted_names():
    from swe.app.conversation_storage.migration.install_records import (
        OfflineTarget,
    )

    with pytest.raises(ValueError, match="staging"):
        OfflineTarget("copaw_dev", "copaw_dev", "migration")
    with pytest.raises(ValueError, match="staging"):
        OfflineTarget("test_staging;DROP", "business", "migration")
    assert (
        OfflineTarget("test_conversation_abc", "business", "migration").schema
        == "test_conversation_abc"
    )


def test_offline_timestamp_normalizes_offset_to_utc():
    from datetime import datetime
    from swe.app.conversation_storage.migration.install_records import (
        _sql_value,
    )

    assert _sql_value("created_at", "2026-10-30T08:00:00+08:00") == datetime(
        2026,
        10,
        30,
    )


async def test_partial_session_update_checks_locked_scope_without_scope_in_patch():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.migration.install_records import (
        OfflineImportStore,
        OfflineTarget,
        _LOCKED,
    )
    from swe.app.conversation_storage.bundle import BundleIndex

    db = SimpleNamespace(
        fetch_one=AsyncMock(
            return_value={
                "id": 1,
                "tenant_key": b"t",
                "source_key": b"s",
                "agent_key": b"a",
            },
        ),
        execute=AsyncMock(return_value=1),
    )
    service = OfflineImportStore(
        db,
        None,
        OfflineTarget("test_conversation_x", "business", "migration"),
    )
    token = _LOCKED.set(
        {"sessions": {1: db.fetch_one.return_value}, "chats": {}, "turns": {}},
    )
    try:
        await service._apply_record(
            {"table": "sessions", "values": {"id": 1, "history_high": 2}},
            {
                "id": 4,
                "tenant_key": b"t",
                "source_key": b"s",
                "agent_key": b"a",
            },
            BundleIndex("bundle", 0, {}, {}),
            update=True,
        )
    finally:
        _LOCKED.reset(token)
    db.execute.assert_awaited_once()
    db.fetch_one.assert_not_awaited()


async def test_undeclared_update_cannot_take_late_lock():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.migration.install_records import (
        OfflineImportStore,
        OfflineTarget,
    )
    from swe.app.conversation_storage.bundle import BundleIndex
    from swe.app.conversation_storage.errors import IntegrityError

    db = SimpleNamespace(fetch_one=AsyncMock(), execute=AsyncMock())
    service = OfflineImportStore(
        db,
        None,
        OfflineTarget("test_conversation_x", "business", "migration"),
    )
    with pytest.raises(IntegrityError, match="not locked"):
        await service._apply_record(
            {"table": "sessions", "values": {"id": 1, "history_high": 2}},
            {"id": 4},
            BundleIndex("bundle", 0, {}, {}),
            update=True,
        )
    db.fetch_one.assert_not_awaited()
    db.execute.assert_not_awaited()


def test_task_projection_preserves_raw_payload_and_final_section():
    from agentscope.message import Msg
    from swe.app.conversation_storage.migration.importer import (
        task_projections,
        message_body,
        _message_row,
    )

    state = {
        "agent": {
            "memory": {
                "content": [
                    Msg("user", "q", "user").to_dict(),
                    Msg("assistant", "answer", "assistant").to_dict(),
                ],
            },
        },
        "task_runs": [{"run_id": "run-1", "memory_start": 0, "memory_end": 2}],
    }
    messages = extract_messages(state, context_epoch=0)
    task_projections(messages, state)
    assert messages[0]["history_projection"]["task_run_section"] == "step"
    assert messages[1]["history_projection"]["task_run_section"] == "final"
    assert (
        message_body(messages[1])["message"]
        == state["agent"]["memory"]["content"][1]
    )
    assert (
        _message_row(1, 1, messages[1], 1, "message", None, None)[
            "task_run_key"
        ]
        == "run-1"
    )


def test_archive_union_retains_old_epoch_only_batches():
    from swe.app.conversation_storage.migration.importer import archive_batches

    batch = {"boundary": {"id": "boundary"}, "messages": [{"id": "message"}]}
    epochs = [
        {"epoch": 2, "batches": [], "evidence": {}},
        {"epoch": 1, "batches": [batch], "evidence": {}},
    ]
    assert archive_batches({"epochs": epochs}) == [{**batch, "epoch": 1}]
    epochs[0]["batches"] = [
        {"boundary": {"id": "boundary"}, "messages": [{"id": "changed"}]},
    ]
    with pytest.raises(ValueError, match="changed"):
        archive_batches({"epochs": epochs})


async def test_verification_reports_storage_error_without_green_result(
    monkeypatch,
    tmp_path,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.migration import verify
    from swe.app.conversation_storage.errors import StorageUnavailable

    monkeypatch.setattr(
        verify.OfflineImportStore,
        "validate_target",
        AsyncMock(side_effect=StorageUnavailable("db unavailable")),
    )
    manifest = SimpleNamespace(
        migration_id="migration",
        data={
            "target": {
                "schema": "test_conversation_x",
                "nas_root": str(tmp_path),
                "migration_id": "migration",
            },
            "source_boundary": {},
            "workspaces": [],
        },
    )
    target = SimpleNamespace(schema="test_conversation_x")
    report = await verify.verify_import(
        None,
        SimpleNamespace(root=tmp_path),
        manifest,
        {
            "version": 1,
            "ready": True,
            "blockers": [],
            "source_boundary": {},
            "workspaces": [],
            "shares": [],
        },
        target,
    )
    assert report["passed"] is False
    assert report["blockers"] == [
        {"position": "migration", "detail": "db unavailable"},
    ]


def test_importer_rejects_changed_source_root_and_boundary_before_sql():
    from types import SimpleNamespace
    from swe.app.conversation_storage.migration.importer import (
        ConversationImporter,
    )

    service = ConversationImporter.__new__(ConversationImporter)
    service.manifest = SimpleNamespace(
        data={
            "source_boundary": {"snapshot_id": "original"},
            "workspaces": [],
        },
    )
    with pytest.raises(ValueError, match="boundary"):
        service._validate_source_binding(
            {"source_boundary": {"snapshot_id": "changed"}},
        )


def test_large_recovery_is_explicitly_blocked_before_import():
    from swe.app.conversation_storage.migration.importer import _block
    from swe.app.conversation_storage.bundle import MAX_READ_BYTES

    with pytest.raises(ValueError, match="projection required"):
        _block("recovery", "recovery", {"huge": "x" * MAX_READ_BYTES})


async def test_archived_only_questions_are_unselectable_but_current_duplicates_remain():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from swe.app.runner.models import ChatMessage
    from swe.app.conversation_storage.migration.verify import (
        _verify_archived_questions,
    )

    history = SimpleNamespace(
        answer_turn=AsyncMock(side_effect=KeyError("archived")),
    )
    chat = {
        "archive": {
            "epochs": [
                {
                    "batches": [
                        {
                            "boundary": {"id": "b"},
                            "messages": [
                                {"id": "archived", "role": "user"},
                                {"id": "current", "role": "user"},
                            ],
                        },
                    ],
                    "evidence": {},
                },
            ],
        },
    }
    spec = SimpleNamespace(id="chat", user_id="user")
    current = [
        ChatMessage(
            id="current",
            role="user",
            content=[],
            metadata={"original_id": "current"},
        ),
    ]
    await _verify_archived_questions(history, spec, current, chat)
    history.answer_turn.assert_awaited_once_with(
        "archived",
        chat_id="chat",
        user_id="user",
    )
    history.answer_turn = AsyncMock(return_value=object())
    with pytest.raises(ValueError, match="fully archived"):
        await _verify_archived_questions(history, spec, current, chat)
