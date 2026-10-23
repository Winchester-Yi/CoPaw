# -*- coding: utf-8 -*-
import asyncio
import contextvars
from unittest.mock import AsyncMock

import pytest

from swe.database import DatabaseConfig, DatabaseConnection, ExecutionResult
from swe.database import connection as module


class Cursor:
    rowcount = 2
    lastrowid = 17

    def __init__(self, conn):
        self.conn = conn

    async def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        if self.conn.failure:
            raise self.conn.failure

    async def executemany(self, sql, params):
        await self.execute(sql, params)

    async def fetchone(self):
        return {
            "acquired": 1,
            "released": 1,
            "value": "中文",
            "max_allowed_packet": 1048576,
            "sql_mode": self.conn.sql_mode,
        }

    async def fetchall(self):
        return [{"value": 1}, {"value": 2}]

    async def close(self):
        pass


class Connection:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.failure = None
        self.sql_mode = ""
        self.in_transaction = False
        self.closed = False
        self.autocommit = True
        self.__class__.instances.append(self)

    async def connect(self):
        assert self.kwargs["database"] == "swe"

    async def cursor(self, **kwargs):
        assert kwargs == {"buffered": True, "dictionary": True}
        return Cursor(self)

    async def ping(self, reconnect=False):
        assert reconnect is False

    async def start_transaction(self):
        self.calls.append(("BEGIN", None))
        self.in_transaction = True

    async def commit(self):
        self.calls.append(("COMMIT", None))
        self.in_transaction = False

    async def rollback(self):
        self.calls.append(("ROLLBACK", None))
        self.in_transaction = False

    async def get_autocommit(self):
        return self.autocommit

    async def shutdown(self):
        self.closed = True

    async def close(self):
        self.closed = True


@pytest.fixture
async def db(monkeypatch):
    Connection.instances = []
    monkeypatch.setattr(module, "MySQLConnection", Connection)
    database = DatabaseConnection(
        DatabaseConfig(
            min_connections=0,
            max_connections=1,
            acquire_timeout=0.05,
            connect_timeout=0.05,
            read_timeout=0.05,
            write_timeout=0.05,
            cleanup_timeout=0.02,
        ),
    )
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_crud_result_and_reuse(db):
    assert await db.execute_result("INSERT", (1,)) == ExecutionResult(2, 17)
    assert await db.insert("INSERT") == 17
    assert await db.execute("UPDATE") == 2
    assert await db.execute_many("UPDATE", []) == 0
    assert await db.execute_many("UPDATE", [(1,)]) == 2
    assert (await db.fetch_one("SELECT"))["value"] == "中文"
    assert len(await db.fetch_all("SELECT")) == 2
    assert len(Connection.instances) == 1


@pytest.mark.asyncio
async def test_nested_transaction_and_lock_share_one_session(db):
    async with db.named_lock("key"):
        async with db.transaction() as session:
            await db.execute("UPDATE")
            async with db.transaction() as nested:
                assert nested is session
                await nested.execute("UPDATE 2")
    calls = Connection.instances[0].calls
    assert sum(sql == "BEGIN" for sql, _ in calls) == 1
    assert sum(sql == "COMMIT" for sql, _ in calls) == 1
    assert any(
        sql == "SELECT RELEASE_LOCK(%s) AS released" for sql, _ in calls
    )


@pytest.mark.asyncio
async def test_cross_database_and_child_task_binding(db, monkeypatch):
    other = DatabaseConnection(db.config)
    await other.connect()
    try:
        async with db.transaction():
            await other.execute("OTHER")
            with pytest.raises(RuntimeError, match="task"):
                await asyncio.create_task(db.execute("CHILD"))
        assert len(Connection.instances) == 2
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_saturated_pool_wait_is_cancellable_and_session_expires(db):
    async with db.acquire() as session:
        waiter = contextvars.Context().run(
            asyncio.create_task,
            db.execute("WAIT"),
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    with pytest.raises(RuntimeError, match="session"):
        await session.execute("STALE")
    assert await db.execute("AGAIN") == 2


@pytest.mark.asyncio
async def test_sql_failure_preserves_error_and_discards(db):
    with pytest.raises(ValueError, match="query failure"):
        async with db.transaction():
            conn = Connection.instances[0]
            conn.failure = ValueError("query failure")
            await db.execute("FAIL")
    assert conn.closed
    await db.execute("NEXT")
    assert len(Connection.instances) == 2


@pytest.mark.asyncio
async def test_unresponsive_query_cleanup_keeps_capacity_until_exit(db):
    entered = asyncio.Event()
    exit_query = asyncio.Event()
    original = Cursor.execute

    async def stalled(self, sql, params=None):
        if sql != "STALL":
            return await original(self, sql, params)
        entered.set()
        try:
            await exit_query.wait()
        except asyncio.CancelledError:
            await exit_query.wait()
        raise ValueError("late cleanup")

    from unittest.mock import patch

    with patch.object(Cursor, "execute", stalled):
        task = asyncio.create_task(db.execute("STALL"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)
        with pytest.raises(TimeoutError):
            await db.execute("NO CAPACITY")
        assert len(Connection.instances) == 1
        await asyncio.wait_for(db.close(), 0.2)
        exit_query.set()
        await asyncio.sleep(0.02)
        assert not db._pool._entries


def test_config_bounds():
    with pytest.raises(ValueError):
        DatabaseConfig(min_connections=3, max_connections=2)
    with pytest.raises(ValueError):
        DatabaseConfig(read_timeout=0)


@pytest.mark.asyncio
async def test_cancelled_transaction_preserves_cancel_if_rollback_fails(db):
    with pytest.raises(asyncio.CancelledError):
        async with db.transaction():
            Connection.instances[0].rollback = AsyncMock(
                side_effect=ValueError("rollback failed"),
            )
            raise asyncio.CancelledError
    assert Connection.instances[0].closed


@pytest.mark.asyncio
async def test_session_state_cannot_leak_between_callers(db):
    with pytest.raises(RuntimeError, match="baseline"):
        async with db.acquire():
            Connection.instances[0].sql_mode = "ANSI"
    assert Connection.instances[0].closed


@pytest.mark.asyncio
async def test_connect_uses_official_constructor_contract(db):
    await db.execute("QUERY")
    conn = Connection.instances[0]
    assert conn.kwargs["autocommit"] is True
    assert isinstance(conn.kwargs["read_timeout"], int)
    assert isinstance(conn.kwargs["write_timeout"], int)


@pytest.mark.asyncio
async def test_cursor_cleanup_failure_does_not_replace_query_error(
    db,
    monkeypatch,
):
    async def fail_close(self):
        raise RuntimeError("cleanup failed")

    async with db.acquire():
        Connection.instances[0].failure = ValueError("primary query error")
        monkeypatch.setattr(Cursor, "close", fail_close)
        with pytest.raises(ValueError, match="primary query error"):
            await db.execute("FAIL")


@pytest.mark.asyncio
async def test_handshake_cancellation_holds_capacity_and_close_is_finite(
    db,
    monkeypatch,
):
    entered = asyncio.Event()
    released = asyncio.Event()

    async def stuck_connect(self):
        entered.set()
        try:
            await released.wait()
        except asyncio.CancelledError:
            await released.wait()

    monkeypatch.setattr(Connection, "connect", stuck_connect)
    query = asyncio.create_task(db.execute("QUERY"))
    await entered.wait()
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(query, 0.2)
    with pytest.raises(TimeoutError):
        await db.execute("SECOND")
    assert len(Connection.instances) == 1
    await asyncio.wait_for(db.close(), 0.2)
    released.set()
    await asyncio.sleep(0.02)
    assert not db._pool._entries


@pytest.mark.asyncio
async def test_acquire_deadline_bounds_handshake(db, monkeypatch):
    db.config.connect_timeout = 10

    async def slow_connect(self):
        await asyncio.sleep(10)

    monkeypatch.setattr(Connection, "connect", slow_connect)
    before = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(db.execute("QUERY"), 0.2)
    assert asyncio.get_running_loop().time() - before < 0.15


def test_timeout_environment_loading(monkeypatch):
    from swe.database import get_database_config

    monkeypatch.setenv("SWE_DB_ACQUIRE_TIMEOUT", "0.5")
    monkeypatch.setenv("SWE_DB_CONNECT_TIMEOUT", "bad-value")
    monkeypatch.delenv("SWE_DB_ACCESS", raising=False)
    config = get_database_config()
    assert config.acquire_timeout == 0.5
    assert config.connect_timeout == 10


@pytest.mark.asyncio
async def test_concurrent_connect_does_not_create_duplicate_pools(monkeypatch):
    Connection.instances = []
    monkeypatch.setattr(module, "MySQLConnection", Connection)
    database = DatabaseConnection(
        DatabaseConfig(min_connections=1, max_connections=1),
    )
    await asyncio.gather(database.connect(), database.connect())
    try:
        assert len(Connection.instances) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repeated_close_does_not_cancel_cleanup_again(db, monkeypatch):
    entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_finished = asyncio.Event()
    repeated_cancel = asyncio.Event()
    original_execute = Cursor.execute

    async def stalled(self, sql, params=None):
        if sql != "STALL":
            return await original_execute(self, sql, params)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_entered.set()
            try:
                await cleanup_finished.wait()
            except asyncio.CancelledError:
                repeated_cancel.set()
                await cleanup_finished.wait()

    monkeypatch.setattr(Cursor, "execute", stalled)
    query = asyncio.create_task(db.execute("STALL"))
    await entered.wait()
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    await cleanup_entered.wait()
    try:
        await db.close()
        await db.close()
        assert not repeated_cancel.is_set()
        assert len(db._pool._entries) == 1
    finally:
        cleanup_finished.set()
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_session_prepared_requires_transaction(db):
    from swe.database.prepared import PreparedInsert

    async with db.acquire() as session:
        with pytest.raises(RuntimeError, match="transaction"):
            await session.insert_prepared(
                PreparedInsert("INSERT VALUES ", "(%s)"),
                [(1,)],
            )


@pytest.mark.asyncio
async def test_swallowed_lazy_prepared_failure_cannot_partially_commit(
    db,
    monkeypatch,
):
    from swe.database.prepared import PreparedInsert

    original_cursor = Connection.cursor

    async def cursor(self, **kwargs):
        if kwargs == {"prepared": True}:
            return Cursor(self)
        return await original_cursor(self, **kwargs)

    def rows():
        yield (1,)
        raise ValueError("lazy source failed")

    monkeypatch.setattr(Connection, "cursor", cursor)
    with pytest.raises(RuntimeError, match="session"):
        async with db.transaction() as session:
            with pytest.raises(ValueError, match="lazy source failed"):
                await session.insert_prepared(
                    PreparedInsert("INSERT VALUES ", "(%s)"),
                    rows(),
                    max_rows=1,
                )
    commands = [sql for sql, _ in Connection.instances[0].calls]
    assert "INSERT VALUES (?)" in commands
    assert "COMMIT" not in commands
    assert "ROLLBACK" in commands


@pytest.mark.asyncio
async def test_baseline_cursor_cleanup_preserves_primary_error(
    db,
    monkeypatch,
):
    original_close = Cursor.close
    async with db.acquire() as session:
        conn = Connection.instances[0]
        conn.failure = ValueError("baseline query failed")
        monkeypatch.setattr(
            Cursor,
            "close",
            AsyncMock(side_effect=RuntimeError("cleanup failed")),
        )
        with pytest.raises(ValueError, match="baseline query failed"):
            await db._pool._session_state(session._entry)
        conn.failure = None
        monkeypatch.setattr(Cursor, "close", original_close)


@pytest.mark.asyncio
async def test_shutdown_failure_quarantines_slot_and_refuses_reconnect(db):
    await db.execute("QUERY")
    conn = Connection.instances[0]
    conn.ping = AsyncMock(side_effect=ConnectionError("lost socket"))
    conn.close = AsyncMock(wraps=conn.close)
    conn.shutdown = AsyncMock(side_effect=RuntimeError("shutdown unconfirmed"))
    with pytest.raises(TimeoutError):
        await db.execute("NEXT")
    assert len(db._pool._entries) == 1
    assert len(Connection.instances) == 1
    conn.close.assert_not_awaited()
    await asyncio.wait_for(db.close(), 0.2)
    with pytest.raises(RuntimeError, match="still shutting down"):
        await db.connect()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ping", "query", "baseline"])
async def test_completed_failure_forces_shutdown_before_replacing_connection(
    db,
    failure,
):
    await db.execute("WARM")
    conn = Connection.instances[0]
    finish_close = asyncio.Event()
    conn.close = AsyncMock(side_effect=finish_close.wait)
    conn.shutdown = AsyncMock(wraps=conn.shutdown)
    if failure == "ping":
        conn.ping = AsyncMock(side_effect=ConnectionError("lost socket"))
    elif failure == "query":
        conn.failure = ValueError("query failed")
    else:
        conn.sql_mode = "ANSI"
    try:
        if failure != "ping":
            with pytest.raises((ValueError, RuntimeError)):
                await db.execute("FAIL")
        assert await db.execute("NEXT") == 2
        conn.close.assert_not_awaited()
        conn.shutdown.assert_awaited_once()
        assert conn.closed
        assert len(Connection.instances) == 2
    finally:
        finish_close.set()
        await asyncio.gather(*tuple(db._pool._tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_handshake_forces_shutdown(db, monkeypatch):
    finish_close = asyncio.Event()
    close = AsyncMock(side_effect=finish_close.wait)
    shutdown = AsyncMock()
    with monkeypatch.context() as patch:
        patch.setattr(
            Connection,
            "connect",
            AsyncMock(side_effect=ConnectionError("handshake failed")),
        )
        patch.setattr(Connection, "close", close)
        patch.setattr(Connection, "shutdown", shutdown)
        try:
            with pytest.raises(ConnectionError, match="handshake failed"):
                await db.execute("FAIL")
            close.assert_not_awaited()
            shutdown.assert_awaited_once()
            assert not db._pool._entries
        finally:
            finish_close.set()
            await asyncio.gather(
                *tuple(db._pool._tasks),
                return_exceptions=True,
            )
    assert await db.execute("NEXT") == 2


@pytest.mark.asyncio
async def test_pool_close_gracefully_closes_healthy_idle_connection(db):
    await db.execute("QUERY")
    conn = Connection.instances[0]
    conn.close = AsyncMock(wraps=conn.close)
    conn.shutdown = AsyncMock(wraps=conn.shutdown)

    await db.close()

    conn.close.assert_awaited_once()
    conn.shutdown.assert_not_awaited()
    assert conn.closed
    assert not db._pool._entries


@pytest.mark.asyncio
async def test_concurrent_pool_close_keeps_idle_connection_graceful(db):
    await db.execute("QUERY")
    conn = Connection.instances[0]
    conn.close = AsyncMock(wraps=conn.close)
    conn.shutdown = AsyncMock(wraps=conn.shutdown)

    await asyncio.gather(db.close(), db.close())

    conn.close.assert_awaited_once()
    conn.shutdown.assert_not_awaited()
    assert not db._pool._entries


@pytest.mark.asyncio
async def test_pool_close_invalidates_and_shuts_down_outstanding_lease(db):
    async with db.acquire() as session:
        conn = session._connection
        conn.close = AsyncMock(wraps=conn.close)
        conn.shutdown = AsyncMock(wraps=conn.shutdown)

        await db.close()

        conn.close.assert_not_awaited()
        conn.shutdown.assert_awaited_once()
        assert not db._pool._entries
        with pytest.raises(RuntimeError, match="session"):
            await session.execute("AFTER CLOSE")


@pytest.mark.asyncio
async def test_close_failure_falls_back_to_shutdown_and_releases_slot(db):
    await db.execute("QUERY")
    conn = Connection.instances[0]

    async def shutdown():
        conn.closed = True

    conn.close = AsyncMock(
        side_effect=RuntimeError("prepared cursor close failed"),
    )
    conn.shutdown = AsyncMock(side_effect=shutdown)

    await db.close()

    assert conn.closed
    conn.close.assert_awaited_once()
    conn.shutdown.assert_awaited_once()
    assert not db._pool._entries
    await db.connect()
    assert await db.execute("NEXT") == 2
    assert len(Connection.instances) == 2


@pytest.mark.asyncio
async def test_pending_tasks_have_module_lifetime_retention(db, monkeypatch):
    from swe.database import pool as pool_module

    entered = asyncio.Event()
    finished = asyncio.Event()
    original_execute = Cursor.execute

    async def stalled(self, sql, params=None):
        if sql != "STALL":
            return await original_execute(self, sql, params)
        entered.set()
        try:
            await finished.wait()
        except asyncio.CancelledError:
            await finished.wait()

    monkeypatch.setattr(Cursor, "execute", stalled)
    query = asyncio.create_task(db.execute("STALL"))
    await entered.wait()
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    await db.close()
    retained = set(db._pool._tasks)
    try:
        assert retained
        assert retained <= pool_module._PENDING_TASKS
    finally:
        finished.set()
        await asyncio.sleep(0.02)
    assert not retained & pool_module._PENDING_TASKS


def test_prepared_template_is_exported():
    from swe.database import PreparedInsert

    assert PreparedInsert("INSERT VALUES ", "(%s)").columns == 1


@pytest.mark.asyncio
async def test_exact_named_lock_preserves_existing_mixed_version_key(db):
    name = "swe_skill_ready:" + "a" * 48
    async with db.named_lock(name, exact_name=True):
        pass
    commands = Connection.instances[0].calls
    assert ("SELECT GET_LOCK(%s, %s) AS acquired", (name, 10)) in commands
    assert ("SELECT RELEASE_LOCK(%s) AS released", (name,)) in commands


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "a" * 65, "中文" * 22])
async def test_exact_lock_rejects_invalid_name_before_connection_acquisition(
    db,
    name,
):
    with pytest.raises(ValueError, match="64"):
        async with db.named_lock(name, exact_name=True):
            pass
    assert not Connection.instances


@pytest.mark.asyncio
@pytest.mark.parametrize("final_shutdown_fails", [False, True])
async def test_late_handshake_socket_is_closed_before_capacity_release(
    db,
    monkeypatch,
    final_shutdown_fails,
):
    entered = asyncio.Event()
    finish_handshake = asyncio.Event()
    first_shutdown = asyncio.Event()
    sockets = {"live": False, "shutdowns": 0}

    async def late_connect(self):
        entered.set()
        try:
            await finish_handshake.wait()
        except asyncio.CancelledError:
            await finish_handshake.wait()
        # A driver can create its resource after cancellation was requested.
        sockets["live"] = True

    async def shutdown(self):
        sockets["shutdowns"] += 1
        if sockets["shutdowns"] == 2 and final_shutdown_fails:
            raise RuntimeError("late socket shutdown unconfirmed")
        sockets["live"] = False
        first_shutdown.set()

    monkeypatch.setattr(Connection, "connect", late_connect)
    monkeypatch.setattr(Connection, "shutdown", shutdown)
    query = asyncio.create_task(db.execute("QUERY"))
    await entered.wait()
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    await first_shutdown.wait()
    assert len(db._pool._entries) == 1
    finish_handshake.set()
    await asyncio.sleep(0.02)
    assert sockets["shutdowns"] == 2
    if final_shutdown_fails:
        assert sockets["live"]
        assert len(db._pool._entries) == 1
    else:
        assert not sockets["live"]
        assert not db._pool._entries


@pytest.mark.asyncio
async def test_closed_pool_does_not_start_rollback_io(db):
    async with db.acquire() as session:
        rollback = AsyncMock()
        session._connection.rollback = rollback
        await db.close()
        await db._rollback(session)
        rollback.assert_not_awaited()
