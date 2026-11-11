# -*- coding: utf-8 -*-
"""Online Session saves install the numeric SQL read projection."""

import json
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.history import HistoryStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from tests.unit.app.conversation_storage.test_chat_repository import chat_row
from tests.unit.app.conversation_storage.test_context_usage import SNAPSHOT
from tests.unit.app.conversation_storage.test_publications import (
    PublicationDatabase,
)


class OnlineDatabase(PublicationDatabase):
    async def execute(self, query, params=None):
        if "context_usage_text=COALESCE" in query:
            assert self.transaction_depth > 0
            if params[0] is not None:
                self.session["context_usage_text"] = params[0]
            self.session["context_usage_invalid"] = params[1]
            return 1
        return await super().execute(query, params)


@pytest.mark.parametrize(
    "usage,invalid",
    [
        (SNAPSHOT, False),
        (SNAPSHOT, True),
        (None, False),
        ({"bad": True}, False),
    ],
)
async def test_session_save_updates_history_sql_usage(
    tmp_path,
    usage,
    invalid,
):
    db = OnlineDatabase()
    db.session.update(
        active_turn_id=None,
        context_usage_text=json.dumps({**SNAPSHOT, "used_tokens": 1}),
        context_usage_invalid=1,
    )
    scope = WorkspaceScope(
        "t",
        "s",
        "a",
        service_unit_id="schema-test-service",
    )
    bundles = BundleStore(tmp_path)
    adapter = MySQLSessionAdapter(db, scope, bundles, channel="console")
    adapter._ensure_session = AsyncMock(
        side_effect=lambda identity: dict(db.session),
    )
    await adapter._save_state(
        adapter.identity("session"),
        {"context_usage": usage, "context_usage_invalid": invalid},
    )

    # Only Chat authorization is stubbed; numeric usage comes from the SQL
    # projection installed by the real Session/publication path above.
    history = HistoryStore(db, scope, bundles, cursor_secret=b"usage-test")
    row = chat_row(projected_status="idle")
    original_fetch = db.fetch_one

    async def fetch_one(query, params=None):
        if "SELECT context_usage_text" in query:
            return dict(db.session)
        return await original_fetch(query, params)

    db.fetch_one = fetch_one
    history._chat = AsyncMock(return_value=row)
    history.chats._revalidate = AsyncMock()
    result = await history.context_usage("chat")
    assert result.available is True
    assert result.used_tokens == (10 if usage == SNAPSHOT else 1)
    assert result.stale is (invalid or usage != SNAPSHOT)
    assert db.session["state_revision"] == 1
