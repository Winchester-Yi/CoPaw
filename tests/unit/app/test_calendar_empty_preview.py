# -*- coding: utf-8 -*-
"""日历无候选不是服务故障，SWE 对外只读查询同样接受空预览。"""

from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from swe.app.crons import calendar_client


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [[], ["2026-10-21T09:00:00+08:00"]])
async def test_server_may_return_fewer_eligible_dates_than_requested(
    monkeypatch,
    values,
):
    request = AsyncMock(
        return_value={
            "allowed": True,
            "is_workday": True,
            "source": "api",
            "business_date": "2026-10-20",
            "next_run_times": values,
        },
    )
    monkeypatch.setattr(calendar_client, "_request_calendar", request)
    monkeypatch.setattr(calendar_client, "_retry_after", 0)
    result = await calendar_client.resolve_calendar(
        cron="0 9 * * sun",
        at=datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
        count=3,
    )
    assert result["source"] == "api"
    assert len(result["next_run_times"]) == len(values)
    assert calendar_client._retry_after == 0


@pytest.mark.asyncio
async def test_weekend_fallback_also_allows_empty_preview(monkeypatch):
    monkeypatch.setattr(
        calendar_client,
        "_request_calendar",
        AsyncMock(side_effect=httpx.ConnectError("offline")),
    )
    monkeypatch.setattr(calendar_client, "_retry_after", 0)
    result = await calendar_client.resolve_calendar(
        cron="0 9 * * sun",
        at=datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
        count=3,
    )
    assert result["source"] == "fallback"
    assert result["next_run_times"] == []
