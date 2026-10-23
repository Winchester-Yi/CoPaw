# -*- coding: utf-8 -*-
"""Driver-independent SQL results and task-owned database sessions."""

import asyncio
import logging
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    affected_rows: int
    lastrowid: int | None


class DatabaseSession:
    """One exclusive lease. Parameters remain separate from trusted SQL text."""

    def __init__(self, pool, entry):
        self._pool = pool
        self._entry = entry
        self._connection = entry.connection
        self._prepared_cache = entry.prepared_cache
        self._owner = asyncio.current_task()
        self._usable = True
        self._in_transaction = False

    def _check(self):
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("Database session belongs to a different task")
        if not self._usable or not self._entry.usable:
            raise RuntimeError("Database session is no longer usable")

    def _discard(self):
        self._usable = False
        self._entry.usable = False

    async def _run(self, operation, timeout=None):
        try:
            self._check()
        except BaseException:
            operation.close()
            raise
        try:
            return await self._pool.run(
                self._entry,
                operation,
                timeout
                or max(
                    self._pool.config.read_timeout,
                    self._pool.config.write_timeout,
                ),
            )
        except BaseException:
            self._discard()
            raise

    async def _query(self, query, params, mode):
        cursor = await self._connection.cursor(buffered=True, dictionary=True)
        try:
            if mode == "many":
                await cursor.executemany(query, params)
            else:
                await cursor.execute(query, params)
            if mode == "one":
                row = await cursor.fetchone()
                return dict(row) if row is not None else None
            if mode == "all":
                return [dict(row) for row in await cursor.fetchall()]
            return ExecutionResult(cursor.rowcount, cursor.lastrowid)
        finally:
            primary_error = sys.exc_info()[1]
            try:
                await cursor.close()
            except BaseException:
                if primary_error is None:
                    raise
                logger.warning("Database cursor cleanup failed", exc_info=True)

    async def execute_result(self, query, params=None):
        return await self._run(self._query(query, params, "execute"))

    async def execute(self, query, params=None):
        return (await self.execute_result(query, params)).affected_rows

    async def insert(self, query, params=None):
        """Return the driver's last insert ID (None/0 when none was produced)."""
        return (await self.execute_result(query, params)).lastrowid

    async def execute_many(self, query, params_list):
        self._check()
        if not params_list:
            return 0
        return (
            await self._run(self._query(query, params_list, "many"))
        ).affected_rows

    async def fetch_one(self, query, params=None):
        return await self._run(self._query(query, params, "one"))

    async def fetch_all(self, query, params=None):
        return await self._run(self._query(query, params, "all"))

    async def insert_prepared(
        self,
        template,
        rows,
        *,
        max_rows=100,
        max_bytes=256 * 1024,
    ):
        from .prepared import execute_prepared

        self._check()
        if not self._in_transaction:
            raise RuntimeError(
                "Prepared inserts require an active transaction",
            )
        try:
            return await execute_prepared(
                self,
                template,
                rows,
                max_rows=max_rows,
                max_bytes=max_bytes,
            )
        except BaseException:
            # Lazy row generation may fail after earlier chunks were executed.
            # Even a caller that catches the error must not commit that prefix.
            self._discard()
            raise
