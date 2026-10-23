# -*- coding: utf-8 -*-
"""In-memory SSE transport for active answer turns.

This adapter owns producer tasks, buffered replay, subscribers, and task
progress.  Answer-turn admission, stopping, and settlement belong to
``AnswerTurnCoordinator``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import weakref
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable

from ..answer_turn.models import TurnIdentity
from .task_progress import TaskProgressPayload, clone_task_progress
from .tool_output_frames import ToolOutputFrame, bind_tool_output_emitter
from .sse_replay import ReplayStream, ReplaySubscription, _await_stream_task

logger = logging.getLogger(__name__)

_SENTINEL = None
_REPLAY_BATCH_SIZE = 64
Producer = Callable[[TurnIdentity, Any], AsyncGenerator[str, None]]


@dataclass
class _RunState:
    """Transport state for one producer and its SSE subscribers."""

    task: asyncio.Task[None]
    identity: TurnIdentity
    queues: list[ReplaySubscription] = field(default_factory=list)
    replay: ReplayStream = field(default_factory=ReplayStream)
    closed: bool = False


class TaskTracker:
    """Per-workspace stream transport keyed by :class:`TurnIdentity`."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._lifecycle_locks: weakref.WeakValueDictionary[
            str,
            asyncio.Lock,
        ] = weakref.WeakValueDictionary()
        self._runs: dict[str, _RunState] = {}
        self._task_progress: dict[str, TaskProgressPayload] = {}

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @staticmethod
    def _chat_id(identity: TurnIdentity | str) -> str:
        return (
            identity.chat_id
            if isinstance(identity, TurnIdentity)
            else identity
        )

    def _lifecycle_lock(self, identity: TurnIdentity | str) -> asyncio.Lock:
        chat_id = self._chat_id(identity)
        return self._lifecycle_locks.setdefault(chat_id, asyncio.Lock())

    async def call_if_idle(
        self,
        identity: TurnIdentity | str,
        callback: Callable[[], Any],
    ) -> tuple[bool, Any]:
        """Run recovery only when no producer is active for the chat."""
        chat_id = self._chat_id(identity)
        async with self._lifecycle_lock(chat_id):
            async with self._lock:
                if any(
                    state.identity.chat_id == chat_id and not state.task.done()
                    for state in self._runs.values()
                ):
                    return False, None
            operation = asyncio.create_task(asyncio.to_thread(callback))
            try:
                return True, await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                try:
                    await operation
                except Exception:
                    logger.exception(
                        "idle callback failed after cancellation chat_id=%s",
                        chat_id,
                    )
                raise exc

    async def has_active_tasks(self) -> bool:
        async with self._lock:
            return any(not state.task.done() for state in self._runs.values())

    async def list_active_tasks(self) -> list[str]:
        async with self._lock:
            return [
                state.identity.chat_id
                for state in self._runs.values()
                if not state.task.done()
            ]

    async def wait_all_done(self, timeout: float = 300.0) -> bool:
        async def _wait_loop() -> None:
            while await self.has_active_tasks():
                await asyncio.sleep(0.5)

        try:
            await asyncio.wait_for(_wait_loop(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def attach(
        self,
        identity: TurnIdentity,
    ) -> ReplaySubscription | None:
        """Attach a subscriber and replay the buffered SSE events."""
        async with self._lock:
            return await self._attach_unlocked(identity)

    async def _attach_unlocked(
        self,
        identity: TurnIdentity,
    ) -> ReplaySubscription | None:
        state = self._runs.get(identity.chat_id)
        if (
            state is None
            or state.identity != identity
            or state.task.done()
            or state.closed
        ):
            return None
        # Cancellation before registration must not leak a subscription.
        await asyncio.sleep(0)
        subscriber = state.replay.subscribe()
        state.queues.append(subscriber)
        return subscriber

    def _has_live_run_unlocked(self, chat_id: str) -> bool:
        state = self._runs.get(chat_id)
        return state is not None and not state.task.done()

    async def update_task_progress(
        self,
        run_key: str,
        payload: TaskProgressPayload,
    ) -> TaskProgressPayload:
        async with self._lock:
            cloned_payload = clone_task_progress(payload)
            if cloned_payload is None:
                raise ValueError("task progress payload must not be empty")
            self._task_progress[run_key] = cloned_payload
            return clone_task_progress(cloned_payload) or cloned_payload

    async def get_task_progress(
        self,
        run_key: str,
    ) -> TaskProgressPayload | None:
        async with self._lock:
            return clone_task_progress(self._task_progress.get(run_key))

    async def detach_subscriber(
        self,
        identity: TurnIdentity,
        queue: asyncio.Queue | ReplaySubscription,
    ) -> None:
        if isinstance(queue, ReplaySubscription):
            async with self._lock:
                state = self._runs.get(identity.chat_id)
                if state is not None and queue in state.queues:
                    state.queues.remove(queue)
            queue.stream.detach(queue)
            if queue.stream.ended and not queue.stream.subscribers:
                await queue.stream.aclose()
            return

    async def attach_or_start(
        self,
        identity: TurnIdentity,
        payload: Any,
        producer: Producer,
        *,
        before_start: Callable[[], None] | None = None,
    ) -> tuple[ReplaySubscription, bool]:
        """Attach to *identity*'s transport or register its producer."""
        async with self._lifecycle_lock(identity):
            async with self._lock:
                queue = await self._attach_unlocked(identity)
                if queue is not None:
                    return queue, False
                if self._has_live_run_unlocked(identity.chat_id):
                    raise RuntimeError("live stream belongs to another turn")
            if before_start is not None:
                operation = asyncio.create_task(
                    asyncio.to_thread(before_start),
                )
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    logger.debug(
                        "delaying cancellation until stream registration chat_id=%s",
                        identity.chat_id,
                    )
                    await operation
            async with self._lock:
                queue = await self._attach_unlocked(identity)
                if queue is not None:
                    return queue, False
                if self._has_live_run_unlocked(identity.chat_id):
                    raise RuntimeError("live stream belongs to another turn")
                return self._start_unlocked(identity, payload, producer)

    def _start_unlocked(
        self,
        identity: TurnIdentity,
        payload: Any,
        producer: Producer,
    ) -> tuple[ReplaySubscription, bool]:
        replay = ReplayStream()
        queue = replay.subscribe()
        tracker_ref = weakref.ref(self)
        batch_writes = os.environ.get("SWE_SSE_BATCH_WRITES", "1") == "1"

        async def _producer() -> None:
            published_since_yield = 0

            async def _broadcast_sse(sse: str) -> None:
                nonlocal published_since_yield
                tracker = tracker_ref()
                if tracker is None:
                    return
                async with tracker.lock:
                    state = tracker._runs.get(identity.chat_id)
                    if state is None or state.closed:
                        return
                if batch_writes:
                    await state.replay.offer(sse)
                else:
                    await state.replay.append(sse)
                published_since_yield += 1
                if published_since_yield >= _REPLAY_BATCH_SIZE:
                    published_since_yield = 0
                    await asyncio.sleep(0)

            async def _emit_tool_output_frame(frame: ToolOutputFrame) -> None:
                await _broadcast_sse(
                    "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n",
                )

            try:
                with bind_tool_output_emitter(_emit_tool_output_frame):
                    async for sse in producer(identity, payload):
                        await _broadcast_sse(sse)
            except asyncio.CancelledError:
                logger.debug("stream producer cancelled identity=%s", identity)
            except Exception:
                logger.exception(
                    "stream producer failed identity=%s",
                    identity,
                )
                await _broadcast_sse(
                    "data: "
                    f"{json.dumps({'error': 'internal server error'})}\n\n",
                )
            finally:

                async def finish_transport():
                    try:
                        await replay.end()
                    finally:
                        tracker = tracker_ref()
                        if tracker is not None:
                            async with tracker.lock:
                                state = tracker._runs.get(identity.chat_id)
                                if (
                                    state is not None
                                    and state.identity == identity
                                ):
                                    tracker._task_progress.pop(
                                        identity.chat_id,
                                        None,
                                    )
                                    tracker._runs.pop(identity.chat_id, None)

                await _await_stream_task(
                    asyncio.create_task(finish_transport()),
                )

        task = asyncio.create_task(_producer())
        self._runs[identity.chat_id] = _RunState(
            task=task,
            identity=identity,
            replay=replay,
            queues=[queue],
        )
        return queue, True

    async def close(self, identity: TurnIdentity) -> None:
        """End this identity's subscriber streams without cancelling its work."""
        async with self._lock:
            state = self._runs.get(identity.chat_id)
            if state is None or state.identity != identity or state.closed:
                return
            state.closed = True
            replay = state.replay
            state.queues.clear()
        await replay.end()

    async def stream(
        self,
        identity: TurnIdentity,
        queue: asyncio.Queue | ReplaySubscription,
    ) -> AsyncGenerator[str, None]:
        """Yield transport events until the producer or transport closes."""
        delivered = 0
        try:
            while True:
                try:
                    if delivered == _REPLAY_BATCH_SIZE:
                        await asyncio.sleep(0)
                        delivered = 0
                    event = await queue.get()
                except asyncio.CancelledError:
                    break
                if event is _SENTINEL:
                    break
                delivered += 1
                yield event
        finally:
            await self.detach_subscriber(identity, queue)
