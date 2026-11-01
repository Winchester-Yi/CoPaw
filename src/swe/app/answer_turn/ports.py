# -*- coding: utf-8 -*-
"""Narrow async ports used by :mod:`answer_turn.coordinator`."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from .models import TurnIdentity, TurnOutcome

Producer = Callable[[TurnIdentity, Any], Awaitable[Any]]


class TurnStreamPort(Protocol):
    async def attach_or_start(
        self,
        identity: TurnIdentity,
        payload: Any,
        producer: Producer,
        *,
        before_start: Any | None = None,
    ) -> Any:
        # Protocol declaration; the coordinator supplies the concrete stream.
        ...

    async def stream(self, identity: TurnIdentity, queue: Any):
        # Protocol declaration; implementations yield events from the queue.
        ...

    async def close(self, identity: TurnIdentity) -> None:
        # Protocol declaration; the concrete stream owner performs cleanup.
        ...


class TurnExecutionPort(Protocol):
    async def persist_stop(self, identity: TurnIdentity) -> None:
        # Commit the stop claim before local state or cancellation effects.
        ...

    async def request_cooperative_stop(self, identity: TurnIdentity) -> None:
        # Protocol declaration; Runner implements cooperative cancellation.
        ...

    async def hard_cancel(self, identity: TurnIdentity) -> None:
        # Protocol declaration; Runner implements task-level cancellation.
        ...


class TurnSessionPort(Protocol):
    async def persist_outcome(
        self,
        outcome: TurnOutcome,
    ) -> None:
        # Protocol declaration; the session adapter persists the outcome.
        ...


class TurnGoalPort(Protocol):
    async def interrupt_if_matches(
        self,
        identity: TurnIdentity,
        reason: str,
    ) -> None:
        # Protocol declaration; Goal service owns matching-turn interruption.
        ...


class TurnSubAgentPort(Protocol):
    async def cancel_for_turn(self, identity: TurnIdentity) -> None:
        # Protocol declaration; SubAgent manager cancels matching workers.
        ...


class TurnApprovalPort(Protocol):
    async def supersede_for_turn(self, identity: TurnIdentity) -> None:
        # Protocol declaration; approval service supersedes matching requests.
        ...


StreamPort = TurnStreamPort
ExecutionPort = TurnExecutionPort
SessionPort = TurnSessionPort
GoalPort = TurnGoalPort
SubagentPort = TurnSubAgentPort
ApprovalPort = TurnApprovalPort
