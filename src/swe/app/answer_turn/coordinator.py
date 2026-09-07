# -*- coding: utf-8 -*-
"""Authoritative state machine for Console answer turns."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .models import (
    TERMINAL_STATUSES,
    StopClaim,
    TurnIdentity,
    TurnLease,
    TurnOutcome,
    TurnStatus,
)
from .ports import (
    TurnApprovalPort,
    TurnExecutionPort,
    TurnGoalPort,
    Producer,
    TurnSessionPort,
    TurnStreamPort,
    TurnSubAgentPort,
)

logger = logging.getLogger(__name__)


class TurnSettlementPendingError(RuntimeError):
    """A terminal turn is waiting for its durable outcome to be retried."""


@dataclass
class _TurnState:
    identity: TurnIdentity
    status: TurnStatus
    outcome: TurnOutcome | None = None
    stop_effects_started: bool = False
    hard_cancel_watcher_started: bool = False
    settlement_started: bool = False
    transport_ended: bool = False


class AnswerTurnCoordinator:
    """Own admission, stop claims, terminal settlement, and active identity."""

    def __init__(
        self,
        *,
        stream: TurnStreamPort,
        execution: TurnExecutionPort,
        session: TurnSessionPort,
        goal: TurnGoalPort,
        subagent: TurnSubAgentPort,
        approval: TurnApprovalPort,
        hard_cancel_delay: float = 5.0,
    ) -> None:
        self.stream = stream
        self.execution = execution
        self.session = session
        self.goal = goal
        self.subagent = subagent
        self.approval = approval
        self.hard_cancel_delay = hard_cancel_delay
        self._turns: dict[str, _TurnState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._settlement_retry_tasks: dict[str, asyncio.Task[Any]] = {}
        self._global_lock = asyncio.Lock()

    async def _chat_lock(self, chat_id: str) -> asyncio.Lock:
        async with self._global_lock:
            return self._locks.setdefault(chat_id, asyncio.Lock())

    async def start_or_attach(
        self,
        chat_id: str,
        payload: Any,
        producer: Producer,
        *,
        msgid: str | None = None,
        before_start: Any | None = None,
    ) -> TurnLease:
        lock = await self._chat_lock(chat_id)
        async with lock:
            state = self._turns.get(chat_id)
            if state is not None and state.settlement_started:
                raise TurnSettlementPendingError(
                    f"settlement pending for chat {chat_id}",
                )
            if state is not None and state.status not in TERMINAL_STATUSES:
                queue = await self.stream.attach(state.identity)
                if queue is None:
                    state.transport_ended = True
                    raise TurnSettlementPendingError(
                        f"transport ended before settlement for chat {chat_id}",
                    )
                return TurnLease(state.identity, queue, False)

            identity = TurnIdentity.create(
                chat_id=chat_id,
                msgid=msgid or f"msg-{uuid4().hex}",
            )
            new_state = _TurnState(identity, TurnStatus.ADMITTING)
            self._turns[chat_id] = new_state
            try:
                queue, _ = await self.stream.attach_or_start(
                    identity,
                    payload,
                    producer,
                    before_start=before_start,
                )
            except asyncio.CancelledError:
                self._turns.pop(chat_id, None)
                raise
            except Exception:
                self._turns.pop(chat_id, None)
                raise
            if self._turns.get(chat_id) is new_state:
                new_state.status = TurnStatus.RUNNING
            return TurnLease(identity, queue, True)

    async def attach(
        self,
        chat_id: str,
        *,
        msgid: str | None = None,
    ) -> TurnLease | None:
        lock = await self._chat_lock(chat_id)
        async with lock:
            state = self._turns.get(chat_id)
            if (
                state is None
                or state.settlement_started
                or state.status in TERMINAL_STATUSES
            ):
                return None
            if msgid is not None and state.identity.msgid != msgid:
                return None
            queue = await self.stream.attach(state.identity)
            if queue is None:
                state.transport_ended = True
                return None
            return TurnLease(state.identity, queue, False)

    async def recover_current(
        self,
        chat_id: str,
        terminal_snapshot: Any,
    ) -> Any:
        """Select an active stream or terminal snapshot atomically per Chat."""
        lock = await self._chat_lock(chat_id)
        async with lock:
            state = self._turns.get(chat_id)
            if state is not None and state.settlement_started:
                raise TurnSettlementPendingError(
                    f"settlement pending for chat {chat_id}",
                )
            if state is not None and state.status not in TERMINAL_STATUSES:
                queue = await self.stream.attach(state.identity)
                if queue is None:
                    state.transport_ended = True
                    raise TurnSettlementPendingError(
                        f"transport ended before settlement for chat {chat_id}",
                    )
                return TurnLease(state.identity, queue, False)
            return await terminal_snapshot()

    async def settlement_pending(self, chat_id: str) -> bool:
        """Return whether a terminal outcome is not durable yet."""
        lock = await self._chat_lock(chat_id)
        async with lock:
            state = self._turns.get(chat_id)
            return bool(
                state is not None
                and (state.settlement_started or state.transport_ended),
            )

    async def status(
        self,
        identity: TurnIdentity | str,
    ) -> TurnStatus | None:
        if isinstance(identity, str):
            lock = await self._chat_lock(identity)
            async with lock:
                state = self._turns.get(identity)
                return state.status if state is not None else None
        lock = await self._chat_lock(identity.chat_id)
        async with lock:
            state = self._turns.get(identity.chat_id)
            if state is None or state.identity != identity:
                return None
            return state.status

    async def statuses(
        self,
        chat_ids: list[str],
    ) -> dict[str, TurnStatus | None]:
        """Return current statuses for several chats in one read operation."""
        async with self._global_lock:
            return {
                chat_id: (
                    state.status
                    if (state := self._turns.get(chat_id))
                    else None
                )
                for chat_id in chat_ids
            }

    async def current_identity(
        self,
        chat_id: str,
    ) -> TurnIdentity | None:
        lock = await self._chat_lock(chat_id)
        async with lock:
            state = self._turns.get(chat_id)
            if state is None or state.status in TERMINAL_STATUSES:
                return None
            return state.identity

    async def claim_stop(
        self,
        identity: TurnIdentity | str,
        *,
        msgid: str | None = None,
        internal: bool = False,
    ) -> StopClaim:
        if msgid is None and not internal:
            return StopClaim(False, status=None)
        if isinstance(identity, str):
            async with self._global_lock:
                current = self._turns.get(identity)
            if current is None:
                return StopClaim(False, status=None)
            identity = current.identity
        lock = await self._chat_lock(identity.chat_id)
        run_effects = False
        start_watcher = False
        async with lock:
            state = self._turns.get(identity.chat_id)
            if state is None or state.identity != identity:
                return StopClaim(False, identity=None, status=None)
            if msgid is not None and msgid != identity.msgid:
                return StopClaim(False, identity=identity, status=state.status)
            if state.status in TERMINAL_STATUSES:
                return StopClaim(False, identity=identity, status=state.status)
            if state.status == TurnStatus.STOPPING:
                return StopClaim(True, identity=identity, status=state.status)
            state.status = TurnStatus.STOPPING
            if not state.stop_effects_started:
                state.stop_effects_started = True
                run_effects = True
            if not state.hard_cancel_watcher_started:
                state.hard_cancel_watcher_started = True
                start_watcher = True
        if run_effects:
            await self._run_stop_effects(identity)
            await self.execution.request_cooperative_stop(identity)
        if start_watcher:
            asyncio.create_task(self._hard_cancel_watch(identity))
        return StopClaim(True, identity=identity, status=TurnStatus.STOPPING)

    async def _run_stop_effects(self, identity: TurnIdentity) -> None:
        await self.approval.supersede_for_turn(identity)
        await self.goal.interrupt_if_matches(identity, "stopped")
        await self.subagent.cancel_for_turn(identity)

    async def _hard_cancel_watch(self, identity: TurnIdentity) -> None:
        await asyncio.sleep(self.hard_cancel_delay)
        lock = await self._chat_lock(identity.chat_id)
        async with lock:
            state = self._turns.get(identity.chat_id)
            if state is None or state.identity != identity:
                return
            if state.status in TERMINAL_STATUSES:
                return
        lock = await self._chat_lock(identity.chat_id)
        async with lock:
            state = self._turns.get(identity.chat_id)
            if state is None or state.identity != identity:
                return
            if state.status in TERMINAL_STATUSES:
                return
            await self.execution.hard_cancel(identity)

    async def settle(
        self,
        identity: TurnIdentity | TurnOutcome,
        outcome: TurnOutcome | None = None,
    ) -> bool:
        if isinstance(identity, TurnOutcome):
            outcome = identity
            identity = outcome.identity
        if outcome is None:
            raise TypeError("settle requires an outcome")
        if outcome.identity != identity:
            raise ValueError("outcome identity must match settled turn")
        lock = await self._chat_lock(identity.chat_id)
        retry_needed = False
        async with lock:
            state = self._turns.get(identity.chat_id)
            if state is None or state.identity != identity:
                return False
            if state.settlement_started:
                return False
            state.settlement_started = True
            state.status = outcome.status
            state.outcome = outcome
            # The coordinator lock is the turn linearization point.  Keep it
            # through the short durable outcome transaction so a following
            # submission can never start between terminal state and persistence.
            try:
                await self.session.persist_outcome(outcome)
            except asyncio.CancelledError:
                retry_needed = True
                logger.warning(
                    "Cancelled while persisting answer turn outcome; retrying "
                    "chat_id=%s msgid=%s",
                    identity.chat_id,
                    identity.msgid,
                )
            except Exception:
                retry_needed = True
                logger.exception(
                    "Failed to persist answer turn outcome; retrying "
                    "chat_id=%s msgid=%s",
                    identity.chat_id,
                    identity.msgid,
                )
            else:
                await self.stream.close(identity)
                release = getattr(self.session, "release_outcome", None)
                if release is not None:
                    await release(identity)
                state = self._turns.get(identity.chat_id)
                if state is not None and state.identity == identity:
                    self._turns.pop(identity.chat_id, None)
        if retry_needed:
            self._schedule_settlement_retry(identity)
            return False
        return True

    def _schedule_settlement_retry(self, identity: TurnIdentity) -> None:
        if identity.chat_id in self._settlement_retry_tasks:
            return
        task = asyncio.create_task(self._retry_settlement(identity))
        self._settlement_retry_tasks[identity.chat_id] = task

    async def _retry_settlement(self, identity: TurnIdentity) -> None:
        delay = 0.05
        try:
            while True:
                await asyncio.sleep(delay)
                lock = await self._chat_lock(identity.chat_id)
                async with lock:
                    state = self._turns.get(identity.chat_id)
                    if (
                        state is None
                        or state.identity != identity
                        or state.outcome is None
                    ):
                        return
                    try:
                        await self.session.persist_outcome(state.outcome)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Retry failed for answer turn outcome "
                            "chat_id=%s msgid=%s",
                            identity.chat_id,
                            identity.msgid,
                        )
                    else:
                        await self.stream.close(identity)
                        release = getattr(
                            self.session,
                            "release_outcome",
                            None,
                        )
                        if release is not None:
                            await release(identity)
                        self._turns.pop(identity.chat_id, None)
                        return
                delay = min(delay * 2, 1.0)
        finally:
            self._settlement_retry_tasks.pop(identity.chat_id, None)
