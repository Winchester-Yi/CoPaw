# -*- coding: utf-8 -*-
"""自动重试和当日失败补跑共用来源系统的在线覆盖。"""

import asyncio
from time import monotonic

import httpx

from .....config.constant import CRON_BATCH_MAX_ATTEMPTS


class BatchRetryPolicyUnavailable(RuntimeError):
    """无法确认当前上限，稍后重新判定。"""


class BatchRetryPolicy:
    def __init__(self, fetch_overrides, *, clock=monotonic):
        self._fetch_overrides = fetch_overrides
        self._clock = clock
        self._lock = asyncio.Lock()
        self._expires_at = float("-inf")
        self._overrides = {}

    async def read_overrides(self) -> dict[str, int]:
        async with self._lock:
            if self._clock() < self._expires_at:
                return self._overrides
            try:
                overrides = await self._fetch_overrides()
                if any(
                    type(value) is not int or not 1 <= value <= 2147483647
                    for value in overrides.values()
                ):
                    raise ValueError("invalid_cron_batch_retry_overrides")
            except (httpx.HTTPError, ValueError) as error:
                raise BatchRetryPolicyUnavailable(
                    "cron_batch_retry_config_unavailable",
                ) from error
            self._overrides = overrides
            self._expires_at = self._clock() + 5
            return overrides


def max_batch_attempts(overrides, source_id):
    if overrides is None:
        return 0
    return overrides.get(source_id, CRON_BATCH_MAX_ATTEMPTS)
