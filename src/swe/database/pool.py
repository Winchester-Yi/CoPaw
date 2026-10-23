# -*- coding: utf-8 -*-
"""Bounded physical connection leases and finite cancellation cleanup."""

import asyncio
import logging
import math
import sys
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
_PENDING_TASKS = set()


def _request_cancel(task):
    """Request cancellation once; repeated requests can interrupt cleanup."""
    if not task.done() and not getattr(task, "_swe_cancel_requested", False):
        task._swe_cancel_requested = True
        task._swe_deferred = True
        task.cancel()


@dataclass(eq=False)
class PoolEntry:
    connection: object
    prepared_cache: OrderedDict = field(default_factory=OrderedDict)
    pending: set = field(default_factory=set)
    usable: bool = True
    disposing: object = None
    max_allowed_packet: int = 0
    baseline: dict = field(default_factory=dict)


class ConnectionPool:
    def __init__(self, config, factory):
        self.config = config
        self.factory = factory
        self._entries = set()
        self._idle = []
        self._condition = asyncio.Condition()
        self._closed = False
        self._tasks = set()

    def _retain(self, task, log_errors=True):
        self._tasks.add(task)
        _PENDING_TASKS.add(task)

        def finished(done):
            self._tasks.discard(done)
            _PENDING_TASKS.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error and (
                    log_errors or getattr(done, "_swe_deferred", False)
                ):
                    logger.warning(
                        "Database background operation failed",
                        exc_info=(type(error), error, error.__traceback__),
                    )

        task.add_done_callback(finished)
        return task

    async def run(self, entry, operation, timeout):
        task = self._retain(asyncio.create_task(operation), log_errors=False)
        entry.pending.add(task)
        task.add_done_callback(entry.pending.discard)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise TimeoutError("Database operation timed out")
            return task.result()
        except BaseException:
            entry.usable = False
            _request_cancel(task)
            raise

    async def initialize(self):
        try:
            for _ in range(self.config.min_connections):
                entry = await self._create()
                self._idle.append(entry)
        except BaseException:
            await self.close()
            raise

    def _new_entry(self):
        config = self.config
        entry = PoolEntry(
            self.factory(
                host=config.host,
                port=config.port,
                user=config.user,
                password=config.password,
                database=config.database,
                charset=config.charset,
                use_unicode=config.use_unicode,
                ssl_disabled=config.ssl_disabled,
                time_zone=config.time_zone,
                autocommit=True,
                connection_timeout=math.ceil(config.connect_timeout),
                read_timeout=math.ceil(config.read_timeout),
                write_timeout=math.ceil(config.write_timeout),
            ),
        )
        self._entries.add(entry)
        return entry

    async def _create(self):
        entry = self._new_entry()
        try:
            await self._connect_reserved(entry)
            return entry
        except BaseException:
            entry.usable = False
            await self.discard(entry)
            raise

    @staticmethod
    async def _session_state(entry):
        cursor = await entry.connection.cursor(buffered=True, dictionary=True)
        try:
            await cursor.execute(
                "SELECT @@max_allowed_packet AS max_allowed_packet, "
                "@@session.sql_mode AS sql_mode, @@session.time_zone AS time_zone, "
                "@@session.character_set_client AS character_set_client, "
                "@@session.character_set_connection AS character_set_connection, "
                "@@session.character_set_results AS character_set_results, "
                "@@session.collation_connection AS collation_connection, "
                "DATABASE() AS database_name",
            )
            row = await cursor.fetchone()
            packet_limit = int(row["max_allowed_packet"])
            if packet_limit <= 0:
                raise ValueError("Invalid server max_allowed_packet")
            return row
        finally:
            primary_error = sys.exc_info()[1]
            try:
                await cursor.close()
            except BaseException:
                if primary_error is None:
                    raise
                logger.warning(
                    "Database baseline cursor cleanup failed",
                    exc_info=True,
                )

    async def acquire(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.acquire_timeout
        while True:
            async with self._condition:
                if self._closed:
                    raise RuntimeError("Database pool is closed")
                if self._idle:
                    entry = self._idle.pop()
                    create = False
                elif len(self._entries) < self.config.max_connections:
                    # _create reserves synchronously before its first await.
                    entry = None
                    create = True
                else:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError(
                            "Database connection acquisition timed out",
                        )
                    await asyncio.wait_for(self._condition.wait(), remaining)
                    continue
                if create:
                    # Reserve inside condition; creation itself does not hold it.
                    entry = self._new_entry()
            if create:
                # Share the initialization code with prewarming without reserving twice.
                try:
                    await self._connect_reserved(entry, deadline)
                except BaseException:
                    entry.usable = False
                    await self.discard(entry)
                    raise
            try:
                await self.run(
                    entry,
                    entry.connection.ping(reconnect=False),
                    min(
                        self.config.read_timeout,
                        max(0.001, deadline - loop.time()),
                    ),
                )
                if self._closed:
                    raise RuntimeError("Database pool is closed")
                return entry
            except asyncio.CancelledError:
                await self.discard(entry)
                raise
            except Exception:
                await self.discard(entry)
                if self._closed or loop.time() >= deadline:
                    raise

    async def _connect_reserved(self, entry, deadline=None):
        config = self.config

        def remaining(limit):
            if deadline is None:
                return limit
            return min(
                limit,
                max(0, deadline - asyncio.get_running_loop().time()),
            )

        await self.run(
            entry,
            entry.connection.connect(),
            remaining(config.connect_timeout),
        )
        entry.baseline = await self.run(
            entry,
            self._session_state(entry),
            remaining(config.read_timeout),
        )
        entry.max_allowed_packet = int(entry.baseline["max_allowed_packet"])
        if self._closed:
            raise RuntimeError("Database pool is closed")

    async def release(self, entry):
        if not entry.usable or self._closed:
            await self.discard(entry)
            return
        try:
            if entry.connection.in_transaction:
                raise RuntimeError(
                    "Unfinished transaction at database lease return",
                )
            if not await self.run(
                entry,
                entry.connection.get_autocommit(),
                self.config.read_timeout,
            ):
                raise RuntimeError("Database autocommit baseline changed")
            state = await self.run(
                entry,
                self._session_state(entry),
                self.config.read_timeout,
            )
            if state != entry.baseline:
                raise RuntimeError("Database session baseline changed")
        except BaseException:
            entry.usable = False
            await self.discard(entry)
            raise
        async with self._condition:
            if not self._closed:
                self._idle.append(entry)
                self._condition.notify()
                return
        await self.discard(entry)

    async def _destroy(self, entry):
        # If socket shutdown fails, retain this entry as quarantined: neither a
        # replacement connection nor reconnect may assume its capacity is free.
        pending = tuple(entry.pending)
        try:
            # Failed operations can leave an unusable socket even after exit.
            if pending or not entry.usable:
                await entry.connection.shutdown()
            else:
                try:
                    await entry.connection.close()
                except asyncio.CancelledError:
                    await entry.connection.shutdown()
                    raise
                except Exception:
                    await entry.connection.shutdown()
        finally:
            # Do not release a slot while cancellation-resistant I/O still owns it.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                # A cancellation-resistant handshake can create a socket after
                # the first shutdown. Close that late resource before freeing
                # capacity; a failed final shutdown keeps the slot quarantined.
                await entry.connection.shutdown()
        async with self._condition:
            self._entries.discard(entry)
            self._condition.notify_all()

    async def discard(self, entry):
        if entry.disposing is None:
            entry.disposing = self._retain(
                asyncio.create_task(self._destroy(entry)),
            )
        # asyncio.wait does not extend the deadline waiting for cancellation cleanup.
        await asyncio.wait(
            {entry.disposing},
            timeout=self.config.cleanup_timeout,
        )

    async def close(self):
        self._closed = True
        async with self._condition:
            idle = set(self._idle)
            self._idle.clear()
            self._condition.notify_all()
        tasks = set()
        for entry in tuple(self._entries):
            for task in tuple(entry.pending):
                _request_cancel(task)
            if entry.disposing is None:
                # Preserve the first close's policy across concurrent closes.
                if entry not in idle:
                    entry.usable = False
                entry.disposing = self._retain(
                    asyncio.create_task(self._destroy(entry)),
                )
            tasks.add(entry.disposing)
        if tasks:
            await asyncio.wait(tasks, timeout=self.config.cleanup_timeout)
            if self._entries:
                logger.warning(
                    "Database pool close left %d unconfirmed connection(s)",
                    len(self._entries),
                )
