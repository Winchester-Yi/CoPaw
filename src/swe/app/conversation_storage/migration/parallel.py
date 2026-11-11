# -*- coding: utf-8 -*-
"""Bounded migration work with SQL commands executed by the calling task."""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from dataclasses import dataclass
from functools import partial

HEALTH_CHECK_INTERVAL = 15.0


async def _drain(task, *, propagate_cancel=False):
    """Observe real completion despite repeated cancellation of the caller."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            continue
        except Exception:
            break
    result = task.result()
    if cancelled and propagate_cancel:
        raise asyncio.CancelledError
    return result


class MigrationStopped(RuntimeError):
    """The coordinator has stopped accepting commands."""


class MigrationIO:
    """Bound submitted threads as well as active threads; never abandon IO."""

    def __init__(self, workers=4):
        if not 1 <= workers <= 32:
            raise ValueError("IO workers must be between 1 and 32")
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="migration-io",
        )
        self._slots = asyncio.Semaphore(workers)
        self._closed = False
        self._pending = set()

    async def call(self, function, *args, **kwargs):
        async with self._slots:
            if self._closed:
                raise MigrationStopped("migration IO is closed")
            # run_in_executor deliberately does not copy the SQL task context.
            future = asyncio.get_running_loop().run_in_executor(
                self._executor,
                Context().run,
                partial(function, *args, **kwargs),
            )
            self._pending.add(future)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                try:
                    await _drain(future)
                except (Exception, asyncio.CancelledError):
                    pass
                raise
            finally:
                self._pending.discard(future)

    async def close(self):
        self._closed = True
        cleanup = asyncio.ensure_future(
            asyncio.gather(*tuple(self._pending), return_exceptions=True),
        )
        try:
            await _drain(cleanup, propagate_cancel=True)
        finally:
            self._executor.shutdown(wait=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()


@dataclass
class _Command:
    name: str
    arguments: tuple
    result: asyncio.Future


class MigrationCoordinator:
    """Serve registered domain commands in the original SQL-owner task.

    Workers only receive ``request``. Handlers are registered by trusted migration
    code, never supplied as callbacks or SQL text by a worker. Each handler owns
    its complete transaction and must not wait for work done by a worker.
    """

    def __init__(self, handlers, *, workers=4, health_check=None):
        if not 1 <= workers <= 32:
            raise ValueError("workers must be between 1 and 32")
        self._handlers = dict(handlers)
        self._workers = workers
        self._commands = deque()
        self._slots = asyncio.Semaphore(2 * workers)
        self._wake = asyncio.Event()
        self._stopped = False
        self._started = False
        self._health_check = health_check
        self._next_health_check = 0.0

    async def request(self, name, *arguments):
        if name not in self._handlers:
            raise ValueError("unknown migration command: " + name)
        async with self._slots:
            if self._stopped:
                raise MigrationStopped("migration coordinator is stopping")
            result = asyncio.get_running_loop().create_future()
            self._commands.append(_Command(name, arguments, result))
            self._wake.set()
            try:
                return await asyncio.shield(result)
            except asyncio.CancelledError:
                # A submitted transaction may already have committed. Observe
                # its outcome before releasing its queue capacity.
                try:
                    await _drain(result)
                except (Exception, asyncio.CancelledError):
                    pass
                raise

    async def run(self, items, operation):
        if self._started:
            raise RuntimeError("migration coordinator cannot be reused")
        self._started = True
        iterator = iter(items)
        tasks = [
            asyncio.create_task(self._worker(iterator, operation))
            for _ in range(self._workers)
        ]
        for task in tasks:
            task.add_done_callback(lambda _task: self._wake.set())
        try:
            await self._serve(tasks)
        finally:
            self._stopped = True
            self._reject_pending()
            for task in tasks:
                if not task.done():
                    task.cancel()
            cleanup = asyncio.ensure_future(
                asyncio.gather(*tasks, return_exceptions=True),
            )
            await _drain(cleanup, propagate_cancel=True)

    async def _worker(self, iterator, operation):
        while not self._stopped:
            try:
                item = next(iterator)
            except StopIteration:
                return
            await operation(item, self.request)

    async def _serve(self, tasks):
        while True:
            # Check errors before starting another transaction.
            for task in tasks:
                if task.done():
                    task.result()
            if all(task.done() for task in tasks):
                return
            if self._health_check is not None:
                now = asyncio.get_running_loop().time()
                if now >= self._next_health_check:
                    # Await directly: SQL health probes belong to this task too.
                    await self._health_check()
                    self._next_health_check = (
                        asyncio.get_running_loop().time()
                        + HEALTH_CHECK_INTERVAL
                    )
            if self._commands:
                command = self._commands.popleft()
                try:
                    value = await self._handlers[command.name](
                        *command.arguments,
                    )
                except BaseException as error:
                    command.result.set_exception(error)
                    raise
                command.result.set_result(value)
                continue
            self._wake.clear()
            if self._health_check is None:
                await self._wake.wait()
            else:
                remaining = max(
                    0.0,
                    self._next_health_check
                    - asyncio.get_running_loop().time(),
                )
                try:
                    await asyncio.wait_for(self._wake.wait(), remaining)
                except asyncio.TimeoutError:
                    pass

    def _reject_pending(self):
        while self._commands:
            command = self._commands.popleft()
            command.result.set_exception(
                MigrationStopped("migration coordinator is stopping"),
            )
