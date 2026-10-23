# -*- coding: utf-8 -*-
"""One lifecycle-owned sweeper for process-local transient memory."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)
Cleanup = Callable[[], Awaitable[object]]


class TransientMemoryCleanup:
    """Schedule cleanup without owning business entries or request context."""

    def __init__(
        self,
        *,
        cleaners: Sequence[Cleanup] | None = None,
        resetters: Sequence[Cleanup] | None = None,
        interval_seconds: float = 60.0,
    ) -> None:
        self._cleaners = cleaners
        self._resetters = resetters
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self.cleanup_errors_total = 0

    async def start(self) -> None:
        """Start once in an empty Context; imports never start background work."""
        if self._task is None or self._task.done():
            self._task = contextvars.Context().run(
                asyncio.create_task,
                self._run(),
            )

    async def stop(self) -> None:
        """Stop sweeping; producers must stop before the separate reset call."""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            # Child cancellation is a result; cancellation of this caller
            # must still propagate (including on Python 3.10).
            await asyncio.gather(task, return_exceptions=True)
        finally:
            if self._task is task and task.done():
                self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            await self.cleanup_once()

    async def _call_all(self, callbacks: Sequence[Cleanup]) -> None:
        for callback in callbacks:
            try:
                await callback()
            except Exception:
                self.cleanup_errors_total += 1
                logger.exception("Transient memory maintenance failed")

    async def cleanup_once(self) -> None:
        """One isolated sweep; each store yields between bounded batches."""
        cleaners = self._cleaners
        if cleaners is None:
            from ..agents.memory.base_memory_manager import (
                prune_summary_task_results,
            )
            from .console_push_store import prune_expired as prune_push
            from .suggestions.store import prune_expired as prune_suggestions

            cleaners = (
                prune_push,
                prune_suggestions,
                prune_summary_task_results,
            )
        await self._call_all(cleaners)

    async def reset(self) -> None:
        """Drop text entries after all application producers have stopped."""
        resetters = self._resetters
        if resetters is None:
            from .console_push_store import reset as reset_push
            from .suggestions.store import reset as reset_suggestions

            resetters = (reset_push, reset_suggestions)
        await self._call_all(resetters)
