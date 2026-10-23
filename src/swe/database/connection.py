# -*- coding: utf-8 -*-
"""Shared async SQL facade for MySQL-compatible databases."""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import Any

from .config import DatabaseConfig
from .pool import ConnectionPool
from .session import DatabaseSession, ExecutionResult

try:
    from . import driver as _driver

    MySQLConnection: Any = _driver.ManagedMySQLConnection
except ImportError:
    MySQLConnection = None

logger = logging.getLogger(__name__)
_CURRENT_CONNECTION: ContextVar[dict | None] = ContextVar(
    "swe_db_connection",
    default=None,
)


def _named_lock_name(name: str) -> str:
    return f"swe:{sha256(name.encode()).hexdigest()[:60]}"


class DatabaseConnection:
    """Parameterized CRUD, task-owned transactions and bounded connection leases."""

    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._pool = None
        self._connected = False
        self._connect_lock = asyncio.Lock()

    @property
    def is_connected(self):
        return self._connected and self._pool is not None

    async def connect(self):
        async with self._connect_lock:
            if self._connected:
                return
            if MySQLConnection is None:
                raise RuntimeError(
                    "MySQL driver is not installed. Install swe with the [mysql] extra",
                )
            if self._pool is not None and self._pool._entries:
                raise RuntimeError(
                    "Previous database connections are still shutting down",
                )
            pool = ConnectionPool(self.config, MySQLConnection)
            # Publish before connecting so concurrent close can abort the handshake.
            self._pool = pool
            try:
                await pool.initialize()
                if pool._closed:
                    raise RuntimeError(
                        "Database pool closed during initialization",
                    )
            except BaseException:
                await pool.close()
                raise
            self._connected = True

    async def close(self):
        self._connected = False
        if self._pool is not None:
            await self._pool.close()

    def _current(self):
        binding = _CURRENT_CONNECTION.get() or {}
        session = binding.get(self)
        if session is not None:
            session._check()
        return session

    @asynccontextmanager
    async def acquire(self):
        current = self._current()
        if current is not None:
            yield current
            return
        if not self.is_connected:
            raise RuntimeError("Database not connected")
        pool = self._pool
        entry = await pool.acquire()
        session = DatabaseSession(pool, entry)
        token = _CURRENT_CONNECTION.set(
            {**(_CURRENT_CONNECTION.get() or {}), self: session},
        )
        try:
            yield session
        except BaseException:
            session._discard()
            raise
        finally:
            session._usable = False
            _CURRENT_CONNECTION.reset(token)
            await pool.release(entry)

    async def _rollback(self, session):
        entry = session._entry
        if session._pool._closed or any(
            not task.done() for task in entry.pending
        ):
            return
        try:
            await session._pool.run(
                entry,
                session._connection.rollback(),
                self.config.cleanup_timeout,
            )
        except (Exception, asyncio.CancelledError):
            logger.warning(
                "Database transaction rollback failed",
                exc_info=True,
            )

    @asynccontextmanager
    async def transaction(self):
        async with self.acquire() as session:
            if session._in_transaction:
                yield session
                return
            try:
                await session._run(session._connection.start_transaction())
                session._in_transaction = True
                yield session
                await session._run(session._connection.commit())
            except BaseException:
                session._discard()
                await self._rollback(session)
                raise
            finally:
                session._in_transaction = False

    @asynccontextmanager
    async def named_lock(
        self,
        name: str,
        timeout: int = 10,
        *,
        exact_name: bool = False,
    ):
        """Pin a named lock, hashing names by default.

        exact_name preserves an existing application's lock key during mixed
        version rollout; it requires a nonempty UTF-8 name of at most 64 bytes.
        """
        if exact_name and (not name or len(name.encode("utf-8")) > 64):
            raise ValueError(
                "Exact database lock names require 1 to 64 UTF-8 bytes",
            )
        lock_name = name if exact_name else _named_lock_name(name)
        async with self.acquire() as session:
            row = await session.fetch_one(
                "SELECT GET_LOCK(%s, %s) AS acquired",
                (lock_name, timeout),
            )
            if not row or int(row.get("acquired") or 0) != 1:
                raise TimeoutError(f"Failed to acquire database lock: {name}")
            try:
                yield
            except BaseException:
                session._discard()
                raise
            finally:
                if session._usable:
                    released = await session.fetch_one(
                        "SELECT RELEASE_LOCK(%s) AS released",
                        (lock_name,),
                    )
                    if not released or int(released.get("released") or 0) != 1:
                        session._discard()
                        raise RuntimeError(
                            "Database named lock release was not confirmed",
                        )

    async def execute_result(self, query, params=None):
        async with self.acquire() as session:
            return await session.execute_result(query, params)

    async def execute(self, query, params=None):
        return (await self.execute_result(query, params)).affected_rows

    async def insert(self, query, params=None):
        """Return last insert ID, preserving the driver's None/0 semantics."""
        return (await self.execute_result(query, params)).lastrowid

    async def execute_many(self, query, params_list):
        if not params_list:
            return 0
        async with self.acquire() as session:
            return await session.execute_many(query, params_list)

    async def fetch_one(self, query, params=None):
        async with self.acquire() as session:
            return await session.fetch_one(query, params)

    async def fetch_all(self, query, params=None):
        async with self.acquire() as session:
            return await session.fetch_all(query, params)

    async def insert_prepared(
        self,
        template,
        rows,
        *,
        max_rows=100,
        max_bytes=256 * 1024,
    ):
        async with self.transaction() as session:
            return await session.insert_prepared(
                template,
                rows,
                max_rows=max_rows,
                max_bytes=max_bytes,
            )


TDSQLConnection = DatabaseConnection
