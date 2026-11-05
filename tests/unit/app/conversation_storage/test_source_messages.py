# -*- coding: utf-8 -*-
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope

from swe.app.conversation_storage.message_installs import (
    MessageInstall,
    _message_values,
)
from swe.app.conversation_storage.bundle import BundleIndex


def test_archive_source_locator_retains_original_publication_and_extent():
    message = MessageInstall(
        "source:11",
        "archive",
        "user",
        source_msg_key="q",
        archive_chat_id=2,
        source_message_id=11,
    )
    sources = {
        11: {
            "service_unit_key": b"schema-test-service",
            "publication_id": 4,
            "byte_offset": 50,
            "byte_length": 27,
        },
    }
    values = _message_values(
        message,
        SimpleNamespace(
            scope=WorkspaceScope(
                "t",
                "s",
                "a",
                service_unit_id="schema-test-service",
            ),
            session_id=1,
            chat_id=2,
            turn_id=None,
        ),
        {"id": 8},
        BundleIndex("new", 100, {}, {}),
        9,
        sources=sources,
    )
    assert (
        values["publication_id"],
        values["byte_offset"],
        values["byte_length"],
    ) == (4, 50, 27)
    assert values["archive_publication_id"] == 8


def test_online_messages_cannot_reuse_another_occurrence_extent():
    with pytest.raises(ValueError, match="archive"):
        MessageInstall("source:11", "memory", "user", source_message_id=11)


def test_source_occurrence_identity_cannot_be_forged():
    from swe.app.conversation_storage.source_messages import _validate_row
    from swe.app.conversation_storage.errors import IntegrityError

    row = {
        "service_unit_key": b"schema-test-service",
        "session_id": 1,
        "layer": "memory",
        "deleted_at": None,
        "current_visible": 1,
        "source_msg_key": b"q",
        "source_occurrence_no": 0,
        "role_key": b"user",
        "kind_key": b"message",
        "turn_id": 3,
        "origin_chat_id": None,
        "context_epoch": 0,
        "task_run_key": None,
    }
    message = MessageInstall(
        "source:11",
        "archive",
        "user",
        source_msg_key="q",
        archive_chat_id=2,
        turn_id=3,
        source_message_id=11,
    )
    row["message_timestamp"] = None
    _validate_row(
        row,
        message,
        SimpleNamespace(
            scope=WorkspaceScope(
                "t",
                "s",
                "a",
                service_unit_id="schema-test-service",
            ),
            session_id=1,
        ),
    )
    with pytest.raises(IntegrityError, match="visible"):
        _validate_row(
            {**row, "current_visible": 0},
            message,
            SimpleNamespace(
                scope=WorkspaceScope(
                    "t",
                    "s",
                    "a",
                    service_unit_id="schema-test-service",
                ),
                session_id=1,
            ),
        )
    with pytest.raises(IntegrityError, match="identity"):
        _validate_row(
            {**row, "source_msg_key": b"other"},
            message,
            SimpleNamespace(
                scope=WorkspaceScope(
                    "t",
                    "s",
                    "a",
                    service_unit_id="schema-test-service",
                ),
                session_id=1,
            ),
        )


async def test_locator_change_after_pin_rejects_commit():
    from swe.app.conversation_storage.source_messages import (
        SourceMessages,
        SOURCE_FIELDS,
    )
    from swe.app.conversation_storage.errors import IntegrityError
    from tests.unit.app.conversation_storage.test_reference_queries import (
        ReferenceDatabase,
        SERVICE,
    )

    row = {
        **dict.fromkeys(SOURCE_FIELDS, 1),
        "service_unit_key": SERVICE,
        "in_context": 1,
    }
    sources = SourceMessages(rows={1: row})
    db = ReferenceDatabase(messages={1: {**row, "byte_offset": 99}})
    with pytest.raises(IntegrityError, match="changed"):
        await sources.revalidate(db)


async def test_source_publications_lock_older_then_current_in_integer_order():
    from swe.app.conversation_storage.source_messages import SourceMessages
    from unittest.mock import AsyncMock

    calls = []

    async def fetch(query, params):
        calls.append(params[0])
        return {
            "service_unit_key": b"schema-test-service",
            "id": params[0],
            "state": "prepared" if params[0] == 9 else "committed",
            "gc_state": "live",
        }

    sources = SourceMessages(
        rows={11: {"publication_id": 2}, 12: {"publication_id": 1}},
    )
    service = SimpleNamespace(
        service_unit_key=b"schema-test-service",
        db=SimpleNamespace(fetch_one=fetch),
    )
    assert (await sources.lock_publications(service, {"id": 9}))["id"] == 9
    assert calls == [1, 2, 9]
