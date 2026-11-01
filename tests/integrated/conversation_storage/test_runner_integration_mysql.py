# -*- coding: utf-8 -*-
"""Observe durable settlement after the request-owned execution has closed."""

from types import SimpleNamespace

from swe.app.conversation_storage.bundle import new_operation_key
from swe.app.conversation_storage.factory import WorkspaceConversationStorage
from swe.app.conversation_storage.runner_integration import IndexedQuery
from swe.app.conversation_storage.session_adapter import (
    DatabaseSessionExecution,
)
from tests.integrated.conversation_storage.test_execution_mysql import (
    admit,
    identity,
    seed,
)


async def test_confirmed_terminal_clears_unknown_without_replaying_closed_execution(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    storage = WorkspaceConversationStorage(
        db,
        identity().scope,
        tmp_path,
        cursor_secret=b"secret",
    )
    admission = await admit(storage.executions)
    await storage.executions.start(admission.authority)
    operation = new_operation_key()
    await storage.executions.finish(
        admission.authority,
        "completed",
        {},
        operation_key=operation,
    )
    execution = DatabaseSessionExecution(
        storage.session_for("console"),
        identity(),
    )
    execution.pending_operation = operation
    query = IndexedQuery(
        storage,
        SimpleNamespace(),
        SimpleNamespace(channel="console"),
        execution,
    )
    query.authority = admission.authority
    query.pending_operation = operation
    before = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_conversation_publications",
    )
    await query.finish("failed")
    assert query.terminal_status == "completed"
    assert query.pending_operation is execution.pending_operation is None
    assert not execution.is_active
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
        == before
    )
