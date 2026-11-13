# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.runtime_ids import (
    SharedIdDatabase,
    runtime_identity,
)
from swe.app.conversation_storage import message_installs
from swe.app.conversation_storage.identity import (
    WorkspaceScope,
    SessionIdentity,
)
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter

JOB = "00000000-0000-4000-8000-000000000001"


class Database:
    def __init__(self):
        self.sequence = 0
        self.tickets = []
        self.business = []

    async def insert(self, query, params):
        if "migration_entries" in query:
            self.sequence += 1
            self.tickets.append(params)
            return self.sequence
        self.business.append((query, params))
        return params[0]

    @asynccontextmanager
    async def transaction(self):
        yield self

    @asynccontextmanager
    async def acquire(self):
        yield self


async def test_runtime_and_migration_use_same_auto_increment_sequence():
    db = Database()
    runtime = SharedIdDatabase(db, JOB)
    migrated_a = await db.insert("migration_entries", ())
    runtime_a = await runtime_identity(runtime)
    # A's business table auto-increment can no longer collide with B's next ticket.
    await db.insert(
        "INSERT INTO swe_conversation_chats (id) VALUES (%s)",
        (runtime_a,),
    )
    migrated_b = await db.insert("migration_entries", ())
    runtime_b = await runtime_identity(runtime)
    assert [migrated_a, runtime_a, migrated_b, runtime_b] == [1, 2, 3, 4]
    assert db.tickets[1][0] == JOB.encode()
    assert db.tickets[1][1] != db.tickets[3][1]
    assert await runtime_identity(db) is None


async def test_allocator_survives_pinned_transaction_and_acquire():
    db = Database()
    runtime = SharedIdDatabase(db, JOB)
    async with runtime.transaction() as tx:
        assert isinstance(tx, SharedIdDatabase)
        assert await runtime_identity(tx) == 1
    async with runtime.acquire() as tx:
        assert await runtime_identity(tx) == 2


async def test_session_insert_uses_explicit_shared_ticket():
    db = Database()
    db.fetch_one = AsyncMock(return_value={"id": 1})
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    adapter = MySQLSessionAdapter(
        SharedIdDatabase(db, JOB),
        scope,
        None,
        channel="console",
    )
    await adapter._ensure_session(
        SessionIdentity(scope, "user", "console", "session"),
    )
    query, params = db.business[0]
    assert "(id,service_unit_key" in query
    assert params[0] == 1 and params[1] == b"svc"


async def test_message_batch_uses_explicit_shared_tickets(monkeypatch):
    db = Database()
    captured = []

    async def prepared(template, rows):
        assert "(id,service_unit_key" in template.prefix
        captured.extend(rows)

    db.insert_prepared = prepared
    db.execute = AsyncMock()
    monkeypatch.setattr(
        message_installs,
        "_message_values",
        lambda *args, **kwargs: {
            key: None for key in message_installs.MESSAGE_FIELDS
        },
    )
    messages = [
        SimpleNamespace(layer="memory", context_epoch=0) for _ in range(2)
    ]
    request = SimpleNamespace(session_id=10, chat_id=None)
    await message_installs.install_messages(
        SharedIdDatabase(db, JOB),
        request,
        messages,
        None,
        None,
        ({"history_high": 0, "context_epoch": 0}, None, None),
    )
    assert [row[0] for row in captured] == [1, 2]


async def test_outer_tenant_wrapper_checks_admission_before_ticket_allocation():
    from swe.app.conversation_storage.tenant_admission import (
        TenantDatabase,
        TenantNotPublished,
    )

    db = Database()
    admission = SimpleNamespace(
        require=AsyncMock(side_effect=TenantNotPublished()),
    )
    wrapped = TenantDatabase(SharedIdDatabase(db, JOB), admission, "tenant")
    with pytest.raises(TenantNotPublished):
        await runtime_identity(wrapped)
    assert db.sequence == 0
    admission.require.side_effect = None
    assert await runtime_identity(wrapped) == 1
    assert admission.require.call_args.args == ("tenant",)


async def test_allocator_keeps_wrapper_when_context_manager_yields_none():
    db = Database()

    @asynccontextmanager
    async def no_session():
        yield None

    db.transaction = no_session
    async with SharedIdDatabase(db, JOB).transaction() as tx:
        assert await runtime_identity(tx) == 1
