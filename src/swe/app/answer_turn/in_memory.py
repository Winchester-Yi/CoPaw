# -*- coding: utf-8 -*-
"""Deterministic in-memory adapters for coordinator tests and local wiring."""

from __future__ import annotations

import asyncio
from typing import Any

from .models import TurnIdentity, TurnOutcome


class InMemoryStream:
    def __init__(self) -> None:
        self.start_calls: list[TurnIdentity] = []
        self.producer_calls = 0
        self.attach_calls: list[TurnIdentity] = []
        self.close_calls: list[TurnIdentity] = []
        self._queues: dict[TurnIdentity, asyncio.Queue] = {}

    async def attach_or_start(
        self,
        identity,
        payload,
        producer,
        *,
        before_start=None,
    ):
        existing = await self.attach(identity)
        if existing is not None:
            return existing, False
        if before_start is not None:
            before_start()
        self.start_calls.append(identity)
        self.producer_calls += 1
        queue = self._queues.setdefault(identity, asyncio.Queue())
        asyncio.create_task(producer(identity, payload))
        return queue, True

    async def attach(self, identity):
        self.attach_calls.append(identity)
        return self._queues.get(identity)

    async def close(self, identity):
        self.close_calls.append(identity)


class InMemoryExecution:
    def __init__(self) -> None:
        self.cancel_calls: list[tuple[TurnIdentity, bool]] = []

    @property
    def hard_cancel_calls(self):
        return [identity for identity, hard in self.cancel_calls if hard]

    async def persist_stop(self, identity):
        return None

    async def request_cooperative_stop(self, identity):
        self.cancel_calls.append((identity, False))

    async def hard_cancel(self, identity):
        self.cancel_calls.append((identity, True))


class InMemorySession:
    def __init__(self) -> None:
        self.calls: list[tuple[TurnIdentity, TurnOutcome]] = []

    async def persist_outcome(self, outcome):
        self.calls.append((outcome.identity, outcome))


class _Calls:
    def __init__(self) -> None:
        self.calls: list[TurnIdentity] = []


class InMemoryGoal(_Calls):
    async def interrupt_if_matches(self, identity, _reason):
        self.calls.append(identity)


class InMemorySubagent(_Calls):
    async def cancel_for_turn(self, identity):
        self.calls.append(identity)


class InMemoryApproval(_Calls):
    async def supersede_for_turn(self, identity):
        self.calls.append(identity)
