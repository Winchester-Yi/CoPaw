# -*- coding: utf-8 -*-
"""系统生命周期只校验本次两表，不依赖用户模型池并在共库关闭前停后台。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import pytest

from scheduler.app.services.cron.system import runtime


@pytest.mark.asyncio
async def test_system_backgrounds_start_and_stop_without_user_schema(
    monkeypatch,
):
    app = FastAPI()
    entered = []

    async def loop(stop):
        entered.append(True)
        await stop.wait()

    workers = [SimpleNamespace(run_loop=loop), SimpleNamespace(run_loop=loop)]
    factory = AsyncMock(return_value=workers)
    monkeypatch.setattr(runtime, "create_system_workers", factory)
    async with runtime.system_lifespan(app):
        assert app.state.system_cron_ready
        await asyncio.sleep(0)
        assert len(entered) == 2
    assert not app.state.system_cron_ready
    assert all(task.done() for task in app.state.system_cron_tasks)


@pytest.mark.asyncio
async def test_missing_system_tables_disable_only_system_runtime(monkeypatch):
    app = FastAPI()
    app.state.managed_cron_runtime_ready = True
    monkeypatch.setattr(
        runtime,
        "create_system_workers",
        AsyncMock(side_effect=RuntimeError("private database text")),
    )
    async with runtime.system_lifespan(app):
        assert app.state.system_cron_prepared
        assert not app.state.system_cron_ready
        assert app.state.managed_cron_runtime_ready
