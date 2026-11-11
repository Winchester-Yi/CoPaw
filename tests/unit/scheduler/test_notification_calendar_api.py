# -*- coding: utf-8 -*-
"""完成通知依赖已装配的日历HTTP入口，而非未提交的任务执行过滤。"""

from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from scheduler.app.routers import api_router, calendar


@pytest.mark.asyncio
@pytest.mark.parametrize("working", [False, True])
async def test_notification_calendar_route_uses_beijing_date(
    monkeypatch,
    working,
):
    service = AsyncMock()
    service.get_day.return_value = {
        "date": "2026-10-01",
        "is_workday": working,
        "source": "api",
    }
    monkeypatch.setattr(calendar, "get_calendar_service", lambda: service)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/scheduler/cron/calendar/resolve",
            json={"at": "2026-09-30T16:10:00+00:00"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "business_date": "2026-10-01",
        "is_workday": working,
        "source": "api",
        "allowed": working,
        "skipped": "" if working else "non_workday",
    }
    service.get_day.assert_awaited_once_with(date(2026, 10, 1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"at": "2026-10-01T00:10:00"},
        {"timezone": "invalid-zone"},
        {"count": -1},
    ],
)
async def test_notification_calendar_rejects_invalid_query(monkeypatch, body):
    service = AsyncMock()
    monkeypatch.setattr(calendar, "get_calendar_service", lambda: service)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/scheduler/cron/calendar/resolve",
            json=body,
        )
    assert response.status_code == 422
    service.get_day.assert_not_awaited()
