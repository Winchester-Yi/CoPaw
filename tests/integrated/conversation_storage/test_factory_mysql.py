# -*- coding: utf-8 -*-
"""Validate runtime readiness against the installed MySQL catalog."""

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.factory import WorkspaceConversationStorage
from swe.app.conversation_storage.identity import WorkspaceScope
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


async def test_runtime_readiness_accepts_formal_schema_and_rejects_missing_index(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"stable",
    )
    await storage.verify_schema()
    assert not list(tmp_path.iterdir())
    await db.execute(
        "ALTER TABLE swe_conversation_sessions DROP INDEX ix_conv_session_recovery",
    )
    with pytest.raises(RuntimeError, match="schema indexes"):
        await storage.verify_schema()


async def test_runtime_readiness_rejects_nontransactional_engine(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    await db.execute("ALTER TABLE swe_conversation_turns ENGINE=MyISAM")
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"stable",
    )
    with pytest.raises(RuntimeError, match="schema.*InnoDB"):
        await storage.verify_schema()
