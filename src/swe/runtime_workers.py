# -*- coding: utf-8 -*-
"""Runtime worker boundaries for responsiveness-critical state work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import threading
import time
from typing import ParamSpec, TypeVar

from .performance_metrics import timed_work

P = ParamSpec("P")
T = TypeVar("T")
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="swe-runtime")
_SLOTS = threading.BoundedSemaphore(8)


async def run_runtime_state_work(
    func: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run responsiveness-critical runtime-state work off the event loop."""
    queued_at = time.monotonic()
    slots = _SLOTS
    # The real worker owns the permit after submission, even on cancellation.
    # pylint: disable=consider-using-with
    while not slots.acquire(blocking=False):
        await asyncio.sleep(0.005)
    context = copy_context()
    operation = partial(func, *args, **kwargs)

    def run() -> T:
        try:
            return context.run(
                partial(timed_work, "runtime_state", queued_at, operation),
            )
        finally:
            slots.release()

    try:
        submitted = _EXECUTOR.submit(run)
    except BaseException:
        slots.release()
        raise
    future = asyncio.wrap_future(submitted)

    def completed(done: asyncio.Future[T]) -> None:
        if not done.cancelled():
            done.exception()

    future.add_done_callback(completed)
    return await asyncio.shield(future)
