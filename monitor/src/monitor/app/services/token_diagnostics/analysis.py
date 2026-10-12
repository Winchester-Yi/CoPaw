# -*- coding: utf-8 -*-
"""Background rule evaluation with a revision-bound renewable lease."""

import asyncio
import logging

from .rules import analyze_branch

logger = logging.getLogger(__name__)


class Analyzer:
    def __init__(self, store):
        self.store = store
        self.task = None

    async def analyze_once(self):
        claim = await self.store.claim_next()
        if not claim:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            snapshot = await self.store.analysis_data(claim)
            result = await asyncio.to_thread(
                analyze_branch,
                snapshot["calls"],
                snapshot["items"],
                snapshot["events"],
            )
            if snapshot["truncated"]:
                for rule in result["rules"]:
                    rule["state"] = "insufficient_evidence"
                    rule["missing_fields"].append("analysis_window_limited")
            return await self.store.commit_analysis(
                claim, snapshot["run"]["data_revision"], result
            )
        except Exception as exc:
            await self.store.fail_analysis(claim, type(exc).__name__)
            logger.warning(
                "Token rule analysis failed: %s", type(exc).__name__
            )
            return False
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass

    async def _heartbeat(self, claim):
        while True:
            await asyncio.sleep(10)
            if not await self.store.renew(claim):
                return

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        backoff = 2
        while True:
            try:
                if not await self.analyze_once():
                    await asyncio.sleep(2)
                backoff = 2
            except Exception as exc:
                logger.warning(
                    "Token analyzer unavailable: %s", type(exc).__name__
                )
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
