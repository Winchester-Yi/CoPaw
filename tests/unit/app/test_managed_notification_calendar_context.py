# -*- coding: utf-8 -*-
"""真实新执行 HTTP 退出后，完成通知仍使用该来源的独立工作日开关。"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from swe.app.crons import calendar_client, managed_execution_api
from swe.app.crons.managed_execution import ManagedExecutionService
from swe.app.crons.managed_results import ExecutionWriter
from swe.app.source_system_config.runtime import (
    get_current_source_system_config,
)
from tests.fakes.managed_source_config import ManagedSourceConfigService
from tests.unit.app.test_managed_cron_execution import Engine
from tests.unit.routers.test_managed_cron_source_config import (
    configured_app,
    execute,
    queued,
)
from tests.unit.scheduler.test_managed_cron_executions import execution_setup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,filter_enabled,working,expected",
    [
        ("ordinary", True, False, "not_required"),
        ("batch", True, False, "not_required"),
        ("ordinary", True, True, "pending"),
        ("batch", False, False, "pending"),
        ("manual", True, False, "pending"),
    ],
)
async def test_new_execution_preserves_notification_policy_after_http_return(
    execution_setup,
    monkeypatch,
    kind,
    filter_enabled,
    working,
    expected,
):
    db = execution_setup[0]
    run = await queued(execution_setup, 0, kind=kind)
    config = ManagedSourceConfigService(
        {
            "bank": {
                "cron_notifications": {
                    "skip_weekend_zhaohu_enabled": filter_enabled,
                },
                "cron_unread_auto_pause": {"enabled": True, "threshold": 20},
            },
        },
    )
    app = configured_app(db, config)
    started, release = asyncio.Event(), asyncio.Event()
    engine = Engine(started=started, release=release)
    observations = []

    async def calendar(**kwargs):
        assert not db.conn.in_transaction
        bound = get_current_source_system_config()
        assert bound.source_id == "bank"
        observations.append(kwargs)
        return {"is_workday": working, "source": "api"}

    async def service(_request):
        return ManagedExecutionService(ExecutionWriter(db), engine)

    monkeypatch.setattr(calendar_client, "resolve_calendar", calendar)
    monkeypatch.setattr(
        managed_execution_api,
        "get_execution_service",
        service,
    )
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as http:
        try:
            assert (await execute(http, run)).status_code == 202
            await asyncio.wait_for(started.wait(), 2)
            assert get_current_source_system_config() is None
            config.overrides["bank"]["cron_notifications"][
                "skip_weekend_zhaohu_enabled"
            ] = not filter_enabled
        finally:
            tasks = tuple(getattr(app.state, "managed_cron_tasks", ()))
            release.set()
            await asyncio.gather(*tasks)
    result = await db.fetch_one("SELECT * FROM swe_cron_executions")
    assert result["notification_status"] == expected
    assert result["status"] == "success"
    assert result["is_final"] == 1 and result["is_read"] == 0
    state = await db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert state["unread_execution_count"] == 1
    # 既有状态表只存前十个字符；通知过滤不应清掉该预览。
    assert state["latest_scheduled_preview"] == "report res"
    assert len(observations) == int(filter_enabled and kind != "manual")
