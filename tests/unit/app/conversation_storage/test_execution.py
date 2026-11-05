# -*- coding: utf-8 -*-
"""Session authority rejects replay, stale writers and independent busy requests."""

from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.execution import (
    ExecutionAuthority,
    ExecutionStore,
)
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    StorageUnavailable,
)
from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)
from swe.app.conversation_storage.retention import RetentionStore
from tests.unit.app.conversation_storage.test_retention import (
    RetentionDatabase,
)


class AuthorityDatabase:
    control_priority = staticmethod(nullcontext)

    def __init__(self):
        self.queries = []
        self.retention = RetentionDatabase()
        self.in_transaction = False
        self.session = {
            "service_unit_key": b"schema-test-service",
            "id": 1,
            "tenant_key": b"tenant",
            "source_key": b"source",
            "agent_key": b"agent",
            "user_key": b"",
            "channel_key": b"console",
            "external_session_key": b"same_session",
            "generation": 2,
            "owner_key": b"owner",
            "active_turn_id": 3,
            "lease_valid": 1,
            "state_revision": 4,
        }
        self.chat = {
            "service_unit_key": b"schema-test-service",
            "id": 2,
            "session_id": 1,
            "deleted_at": None,
            "tenant_key": b"tenant",
            "source_key": b"source",
            "agent_key": b"agent",
        }
        self.turn = {
            "service_unit_key": b"schema-test-service",
            "id": 3,
            "session_id": 1,
            "chat_id": 2,
            "generation": 2,
            "question_msg_key": b"question",
            "state": "admitted",
            "history_status": None,
        }
        self.turn["runtime_turn_key"] = b"runtime"
        self.turn["admission_publication_id"] = 10
        self.session["context_epoch"] = 0
        self.chat["next_turn_order"] = 1
        self.chat["history_revision"] = self.chat["epoch"] = 0
        self.reserved = []

    @asynccontextmanager
    async def transaction(self):
        if self.in_transaction:
            yield self
            return
        self.in_transaction = True
        before = deepcopy((self.session, self.chat, self.turn))
        try:
            yield self
        except BaseException:
            self.session, self.chat, self.turn = before
            raise
        finally:
            self.in_transaction = False

    async def fetch_one(self, query, params=None):
        assert query.count("%s") == len(params)
        self.queries.append((query, params))
        if "swe_conversation_retentions" in query:
            return await self.retention.fetch_one(query, params)
        if "swe_conversation_sessions" in query:
            return deepcopy(self.session)
        if "swe_conversation_chats" in query:
            return deepcopy(self.chat)
        return deepcopy(self.turn)

    async def fetch_all(self, query, params=None):
        assert query.count("%s") == len(params)
        self.queries.append((query, params))
        return await self.retention.fetch_all(query, params)

    async def insert(self, query, params=None):
        assert query.count("%s") == len(params)
        self.queries.append((query, params))
        if "swe_conversation_retentions" in query:
            return await self.retention.insert(query, params)
        columns = query.split("(", 1)[1].split(")", 1)[0].split(",")
        self.turn = dict(zip(columns, params))
        self.turn["id"] = 3
        return 3

    async def execute(self, query, params=None):
        assert query.count("%s") == len(params)
        self.queries.append((query, params))
        if "SET state=%s" in query:
            self.turn["state"] = params[0]
        elif "SET generation=%s,owner_key=%s,active_turn_id=%s" in query:
            self.session.update(
                generation=params[0],
                owner_key=params[1],
                active_turn_id=params[2],
                lease_valid=1,
            )
        elif "next_turn_order=next_turn_order+1" in query:
            self.chat["next_turn_order"] += 1
        return 1


class PublicationDouble:
    service_unit_key = b"schema-test-service"

    def __init__(self, db):
        self.db = db

    async def reserve(self, request, plan):
        assert self.db.in_transaction
        self.db.reserved.append((request, plan))
        return {
            "id": 10,
            "operation_key": request.operation_key.encode(),
            "state": "prepared",
        }

    async def get(self, operation):
        return {
            "id": 10,
            "operation_key": operation.encode(),
            "state": "prepared",
        }

    async def write_bundle(self, operation, blocks):
        assert not self.db.in_transaction
        self.blocks = blocks

    async def commit(self, operation):
        assert not self.db.in_transaction
        return {
            "id": 10,
            "operation_key": operation.encode(),
            "state": "committed",
        }


def authority():
    return ExecutionAuthority(
        SessionIdentity(
            WorkspaceScope(
                "tenant",
                "source",
                "agent",
                service_unit_id="schema-test-service",
            ),
            "",
            "console",
            "same_session",
        ),
        session_id=1,
        chat_id=2,
        turn_id=3,
        generation=2,
        owner_key="owner",
    )


@pytest.mark.parametrize(
    "method",
    ["start", "renew", "request_stop", "reserve_admission", "finish"],
)
async def test_execution_writes_check_scope_fence_before_session_locks(
    monkeypatch,
    method,
):
    db = AuthorityDatabase()
    service = ExecutionStore(
        db,
        publications=PublicationDouble(db),
        service_unit_id="schema-test-service",
    )
    gate = AsyncMock(side_effect=ConflictError("backup fence"))
    monkeypatch.setattr(RetentionStore, "assert_writes_allowed", gate)
    arguments = {
        "start": ((authority(),), {}),
        "renew": ((authority(),), {}),
        "request_stop": ((authority().identity, "question"), {}),
        "reserve_admission": (
            (authority().identity, "chat", "question"),
            {
                "operation_key": "a" * 64,
                "runtime_turn_key": "runtime",
            },
        ),
        "finish": (
            (authority(), "completed", {}),
            {
                "operation_key": "a" * 64,
            },
        ),
    }
    args, kwargs = arguments[method]
    with pytest.raises(ConflictError, match="backup fence"):
        await getattr(service, method)(*args, **kwargs)
    assert not db.queries


async def test_start_and_renew_validate_database_time_lease_and_exact_authority():
    db = AuthorityDatabase()
    store = ExecutionStore(
        db,
        owner_key="owner",
        service_unit_id="schema-test-service",
    )
    result = await store.start(authority())
    assert result["state"] == "running"
    await store.renew(authority())
    assert not db.in_transaction
    assert any("UTC_TIMESTAMP(6)" in query for query, _ in db.queries)
    assert [
        query.split("FROM ")[1].split()[0]
        for query, _ in db.queries
        if "FOR UPDATE" in query and "swe_conversation_retentions" not in query
    ][:3] == [
        "swe_conversation_sessions",
        "swe_conversation_chats",
        "swe_conversation_turns",
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", 3),
        ("owner_key", b"other"),
        ("active_turn_id", None),
    ],
)
async def test_changed_owner_cannot_start_or_renew(field, value):
    db = AuthorityDatabase()
    db.session[field] = value
    store = ExecutionStore(
        db,
        owner_key="owner",
        service_unit_id="schema-test-service",
    )
    with pytest.raises(ConflictError, match="authority"):
        await store.start(authority())
    with pytest.raises(ConflictError, match="authority"):
        await store.renew(authority())
    assert db.turn["state"] == "admitted"


async def test_stop_is_persisted_before_return_and_terminal_is_not_overwritten():
    db = AuthorityDatabase()
    store = ExecutionStore(
        db,
        owner_key="owner",
        service_unit_id="schema-test-service",
    )
    first = await store.request_stop(authority().identity, "question")
    assert first["state"] == "stopping"
    assert not db.in_transaction
    db.turn.update(state="succeeded", history_status="completed")
    second = await store.request_stop(authority().identity, "question")
    assert second["state"] == "succeeded"
    assert second["history_status"] == "completed"


async def test_stop_rejects_another_question_and_scope_mismatch():
    db = AuthorityDatabase()
    store = ExecutionStore(
        db,
        owner_key="owner",
        service_unit_id="schema-test-service",
    )
    with pytest.raises(ConflictError, match="question"):
        await store.request_stop(authority().identity, "other")
    db.session["user_key"] = b"other"
    with pytest.raises(IntegrityError, match="identity"):
        await store.request_stop(authority().identity, "question")


async def test_same_question_returns_original_admission_without_execution_replay():
    db = AuthorityDatabase()
    store = ExecutionStore(
        db,
        owner_key="new-owner",
        publications=PublicationDouble(db),
        service_unit_id="schema-test-service",
    )
    result = await store.reserve_admission(
        authority().identity,
        "chat",
        "question",
        operation_key="a" * 64,
        runtime_turn_key="new-runtime",
    )
    assert not result.created
    assert result.turn["id"] == 3
    assert result.authority is None
    assert not db.reserved


@pytest.mark.parametrize("state", ["reserved", "running", "settling"])
async def test_different_question_is_busy_even_if_original_owner_lease_expired(
    state,
):
    db = AuthorityDatabase()
    db.turn = None
    db.session["lease_valid"] = 0
    store = ExecutionStore(
        db,
        owner_key="new-owner",
        publications=PublicationDouble(db),
        service_unit_id="schema-test-service",
    )
    with pytest.raises(ConflictError, match="busy"):
        await store.reserve_admission(
            authority().identity,
            "chat",
            "different-question",
            operation_key="a" * 64,
            runtime_turn_key="new-runtime",
        )
    assert not db.reserved


async def test_new_admission_reserves_turn_slot_and_original_publication_atomically():
    db = AuthorityDatabase()
    db.turn = None
    db.session.update(active_turn_id=None, owner_key=None)
    store = ExecutionStore(
        db,
        owner_key="new-owner",
        publications=PublicationDouble(db),
        service_unit_id="schema-test-service",
    )
    result = await store.reserve_admission(
        authority().identity,
        "chat",
        "new-question",
        operation_key="a" * 64,
        runtime_turn_key="new-runtime",
    )
    assert result.created
    assert result.authority.generation == 3
    assert db.session["active_turn_id"] == 3
    assert db.turn["state"] == "reserved"
    assert db.turn["question_msg_key"] == b"new-question"
    assert db.chat["next_turn_order"] == 2
    request, plan = db.reserved[0]
    assert request.operation_key == "a" * 64
    assert plan.recovery_block == "recovery"
    assert plan.turn_install.phase == "admission"
    assert plan.messages[0].layer == "anchor"
    assert not db.in_transaction


async def test_terminal_publication_does_not_release_slot_before_confirmed_commit():
    db = AuthorityDatabase()
    publications = PublicationDouble(db)
    store = ExecutionStore(
        db,
        owner_key="owner",
        publications=publications,
        service_unit_id="schema-test-service",
    )
    result = await store.finish(
        authority(),
        "completed",
        {"custom_future_module": {"keep": True}},
        operation_key="a" * 64,
    )
    request, plan = db.reserved[0]
    assert request.kind == "terminal"
    assert plan.turn_install.history_status == "completed"
    assert result["state"] == "committed"
    # The double cannot install a terminal; execution.py must not bypass it.
    assert db.session["active_turn_id"] == 3
    assert db.turn["state"] == "admitted"
    assert (
        publications.blocks[0].content
        == b'{"custom_future_module":{"keep":true}}'
    )


async def test_natural_completion_after_stop_is_rejected_before_nas_write():
    db = AuthorityDatabase()
    db.turn["state"] = "stopping"
    publications = PublicationDouble(db)
    store = ExecutionStore(
        db,
        owner_key="owner",
        publications=publications,
        service_unit_id="schema-test-service",
    )
    with pytest.raises(ConflictError, match="Stop"):
        await store.finish(
            authority(),
            "completed",
            {},
            operation_key="a" * 64,
        )
    assert not db.reserved
    assert not hasattr(publications, "blocks")


async def test_foreign_service_execution_is_rejected_before_transaction_mutation():
    db = AuthorityDatabase()
    service = ExecutionStore(db, service_unit_id="other-service")
    with pytest.raises(IntegrityError, match="service"):
        await service.request_stop(authority().identity, "question")
    assert not db.queries


def test_execution_rejects_foreign_publication_store():
    with pytest.raises(IntegrityError, match="service"):
        ExecutionStore(
            AuthorityDatabase(),
            publications=PublicationDouble(None),
            service_unit_id="other-service",
        )


async def test_reserved_authority_can_renew_during_admission_publication():
    db = AuthorityDatabase()
    db.turn["state"] = "reserved"
    store = ExecutionStore(db, service_unit_id="schema-test-service")
    await store.renew(authority())
    assert db.turn["state"] == "reserved"


async def test_renewal_rechecks_identity_at_update_after_association_locks():
    db = AuthorityDatabase()
    db.execute = AsyncMock(return_value=0)
    store = ExecutionStore(db, service_unit_id="schema-test-service")
    with pytest.raises(ConflictError, match="authority"):
        await store.renew(authority())
    sql, params = db.execute.call_args.args
    assert "lease_until>UTC_TIMESTAMP(6)" not in sql
    assert "generation=%s" in sql and "owner_key=%s" in sql
    assert "active_turn_id=%s" in sql
    assert params == (30, 1, 2, b"owner", 3)


async def test_renewal_checks_elapsed_budget_when_loop_could_not_deliver_timer():
    import time

    db = AuthorityDatabase()
    store = ExecutionStore(
        db,
        service_unit_id="schema-test-service",
        lease_seconds=1,
    )

    async def blocked_update(*args):
        time.sleep(0.36)
        return 1

    db.execute = blocked_update
    with pytest.raises(StorageUnavailable, match="heartbeat"):
        await store.renew(authority())


@pytest.mark.parametrize("method", ["start", "renew", "finish"])
async def test_expired_heartbeat_does_not_revoke_matching_execution(method):
    db = AuthorityDatabase()
    db.session["lease_valid"] = 0
    publications = PublicationDouble(db)
    store = ExecutionStore(
        db,
        publications=publications,
        service_unit_id="schema-test-service",
    )
    if method == "finish":
        result = await store.finish(
            authority(),
            "completed",
            {},
            operation_key="c" * 64,
        )
        assert result["state"] == "committed"
    else:
        await getattr(store, method)(authority())
    assert not db.in_transaction


@pytest.mark.parametrize("method", ["start", "renew", "finish"])
@pytest.mark.parametrize(
    "changed",
    [
        {"generation": 3},
        {"owner_key": b"other"},
        {"owner_key": None},
        {"active_turn_id": 4},
    ],
)
async def test_expired_heartbeat_never_bypasses_execution_fencing(
    method,
    changed,
):
    db = AuthorityDatabase()
    db.session.update(lease_valid=0, **changed)
    store = ExecutionStore(
        db,
        publications=PublicationDouble(db),
        service_unit_id="schema-test-service",
    )
    with pytest.raises(ConflictError, match="authority"):
        if method == "finish":
            await store.finish(
                authority(),
                "completed",
                {},
                operation_key="d" * 64,
            )
        else:
            await getattr(store, method)(authority())
    assert not db.reserved


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("budget"),
        ConnectionError("disconnected"),
        OSError("network"),
        pytest.param(("InterfaceError", 2003), id="driver-connect"),
        pytest.param(
            ("ConnectionTimeoutError", None),
            id="driver-connect-timeout",
        ),
        pytest.param(("ReadTimeoutError", None), id="driver-read-timeout"),
        pytest.param(("WriteTimeoutError", None), id="driver-write-timeout"),
        pytest.param(("OperationalError", 2013), id="driver-lost"),
        pytest.param(("DatabaseError", 1040), id="connection-limit"),
        pytest.param(("DatabaseError", 1205), id="lock-timeout"),
        pytest.param(("InternalError", 1213), id="deadlock"),
    ],
)
async def test_heartbeat_transient_errors_are_reported_as_unavailability(
    failure,
):
    from mysql.connector import errors
    from swe.app.conversation_storage.errors import StorageUnavailable

    if isinstance(failure, tuple):
        failure = getattr(errors, failure[0])(
            "database temporarily unavailable",
            errno=failure[1],
        )
    db = AuthorityDatabase()
    store = ExecutionStore(db, service_unit_id="schema-test-service")
    store._assert_writes_allowed = AsyncMock(side_effect=failure)
    with pytest.raises(StorageUnavailable, match="heartbeat") as caught:
        await store.renew(authority())
    assert caught.value.__cause__ is failure
    assert not db.in_transaction


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("unexpected bug"),
        ConflictError("backup fence"),
        IntegrityError("association"),
        pytest.param(("ProgrammingError", 1064), id="sql-syntax"),
        pytest.param(("IntegrityError", 1062), id="duplicate-key"),
    ],
)
async def test_heartbeat_fatal_errors_are_not_reclassified(failure):
    from mysql.connector import errors

    if isinstance(failure, tuple):
        failure = getattr(errors, failure[0])(
            "invalid database operation",
            errno=failure[1],
        )
    db = AuthorityDatabase()
    store = ExecutionStore(db, service_unit_id="schema-test-service")
    store._assert_writes_allowed = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)) as caught:
        await store.renew(authority())
    assert caught.value is failure


async def test_expired_heartbeat_finish_still_honors_stop_and_deleted_chat():
    db = AuthorityDatabase()
    db.session["lease_valid"] = 0
    store = ExecutionStore(
        db,
        publications=PublicationDouble(db),
        service_unit_id="schema-test-service",
    )
    db.turn["state"] = "stopping"
    with pytest.raises(ConflictError, match="Stop"):
        await store.finish(
            authority(),
            "completed",
            {},
            operation_key="e" * 64,
        )
    db.chat["deleted_at"] = "deleted"
    with pytest.raises(ConflictError, match="Chat"):
        await store.renew(authority())
    assert not db.reserved
