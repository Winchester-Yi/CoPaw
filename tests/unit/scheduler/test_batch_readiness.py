import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_missing_schema_does_not_start_dispatch_loop(monkeypatch):
    from scheduler.app import _app as app
    from scheduler.app.services.cron.batch_run_state import RunStateNotReady

    service = SimpleNamespace(run_loop=AsyncMock())
    monkeypatch.setattr(app, "DB_HOST", "test")
    monkeypatch.setattr(
        app, "init_db_connection", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(app, "close_db_connection", AsyncMock())
    monkeypatch.setattr(app, "cron_scheduling_runtime_enabled", lambda: True)
    monkeypatch.setattr(app, "get_cron_scheduling_service", lambda: service)
    monkeypatch.setattr(
        app,
        "assert_run_state_schema_ready",
        AsyncMock(side_effect=RunStateNotReady("migration required")),
        raising=False,
    )
    async with app.lifespan(FastAPI()):
        await asyncio.sleep(0)
        service.run_loop.assert_not_awaited()
