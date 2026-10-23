# -*- coding: utf-8 -*-
"""Bounded execution boundary for synchronous backup SDK work."""

from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import threading
import time
from typing import Callable, TypeVar

from ...performance_metrics import timed_work

_Result = TypeVar("_Result")


class BackupIOWorker:
    """Reserve capacity before submitting; cancellation retains running slots."""

    def __init__(self, max_workers: int = 4):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="swe-backup",
        )
        self._available = max_workers
        self._lock = threading.Lock()
        self._waiters = deque()

    async def _acquire(self) -> None:
        loop = asyncio.get_running_loop()
        waiter = (loop, loop.create_future())
        with self._lock:
            if self._available:
                self._available -= 1
                return
            self._waiters.append(waiter)
        try:
            await asyncio.shield(waiter[1])
        except asyncio.CancelledError:
            with self._lock:
                try:
                    self._waiters.remove(waiter)
                    granted = False
                except ValueError:
                    granted = True
            if granted:
                self._release()
            raise

    def _release(self) -> None:
        with self._lock:
            if not self._waiters:
                self._available += 1
                return
            loop, future = self._waiters.popleft()
        try:
            loop.call_soon_threadsafe(future.set_result, None)
        except RuntimeError:
            # A closed event loop cannot consume its reserved slot.
            self._release()

    async def run(
        self,
        function: Callable[..., _Result],
        *args,
        **kwargs,
    ) -> _Result:
        loop = asyncio.get_running_loop()
        queued_at = time.monotonic()
        await self._acquire()
        try:
            operation = partial(function, *args, **kwargs)
            submitted = self._executor.submit(
                copy_context().run,
                partial(timed_work, "backup", queued_at, operation),
            )
        except BaseException:
            self._release()
            raise
        submitted.add_done_callback(lambda done: self._release())
        future = asyncio.wrap_future(submitted, loop=loop)

        def completed(done):
            # Retrieve failures even when the requesting coroutine was cancelled.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(completed)
        return await asyncio.shield(future)


_backup_io_worker = BackupIOWorker()


async def run_backup_io(
    function: Callable[..., _Result],
    *args,
    **kwargs,
) -> _Result:
    """Run complete synchronous backup work with the request's ContextVars."""
    return await _backup_io_worker.run(function, *args, **kwargs)
