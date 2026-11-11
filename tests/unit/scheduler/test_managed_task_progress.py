# -*- coding: utf-8 -*-
"""Scheduler 日进度只读新任务，复用年份/工作日/日期范围规则。"""

import json
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.repository import (
    ManagedCronRepository,
)
from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase

CALLER = CallerIdentity("alice", "bank", "default", "alice")


class Calendar:
    async def get_day(self, day):
        return {
            "date": day.isoformat(),
            "source": "api",
            "is_workday": day == date(2026, 10, 10)
            or (day.weekday() < 5 and day != date(2026, 10, 1)),
        }


@pytest.fixture
def repo():
    db = QueryDatabase()
    yield ManagedCronRepository(
        db,
        calendar=Calendar(),
        clock=lambda: datetime.fromisoformat("2026-10-10T11:00:00+08:00"),
    )
    db.conn.close()


def job(repo, name, cron="0 9 * * * 2026", period=None, **kwargs):
    repo.db.add(name, **kwargs)
    spec = json.loads(
        repo.db.conn.execute(
            "SELECT spec_json FROM swe_cron_jobs_v2 WHERE job_id=?",
            (name,),
        ).fetchone()[0],
    )
    spec["schedule"]["cron"] = cron
    spec["run_date_range"] = period
    repo.db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET cron_expr=?,spec_json=?,run_start_date=?,run_end_date=? WHERE job_id=?",
        (
            cron,
            json.dumps(spec),
            (period or {}).get("start_date"),
            (period or {}).get("end_date"),
            name,
        ),
    )
    repo.db.conn.commit()


@pytest.mark.asyncio
async def test_progress_real_query_keeps_identity_and_binding(repo):
    from scheduler.app.services.cron.managed.task_progress import progress_page

    job(repo, "new", last_run_at="2026-10-10T01:00:01", last_status="success")
    job(repo, "old", owner="legacy")
    job(repo, "foreign", source="other")
    job(repo, "deleted", deleted=True)
    result = await progress_page(repo, CALLER, date(2026, 10, 10))
    assert [item["job_id"] for item in result["items"]] == ["new"]
    item = result["items"][0]
    assert item["scheduled_time"] == "2026-10-10T09:00:00+08:00"
    assert item["status"] == "completed"
    assert item["meta"]["task_chat_id"] == "chat-new"
    assert item["status_basis"] == "latest_task_state"
    assert repo.db.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day,cron,period,expected",
    [
        ("2026-10-01", "0 9 * * * 2026", None, False),
        ("2026-10-10", "0 0 * * * 2026", None, True),
        ("2026-10-10", "0 9 * * * 2027", None, False),
        (
            "2026-10-10",
            "0 9 * * * 2026",
            {"start_date": "2026-10-11", "end_date": "2026-10-12"},
            False,
        ),
        (
            "2026-10-10",
            "0 9 * * * 2026",
            {"start_date": "2026-10-10", "end_date": "2026-10-10"},
            True,
        ),
        ("2026-10-30", "0 9 LW * * 2026", None, True),
        ("2026-10-29", "0 9 LW * * 2026", None, False),
        ("2026-10-10", "0 9 10W * * 2026", None, True),
    ],
)
async def test_progress_calendar_date_and_year(
    repo,
    day,
    cron,
    period,
    expected,
):
    from scheduler.app.services.cron.managed.task_progress import progress_page

    job(repo, "job", cron, period)
    result = await progress_page(repo, CALLER, date.fromisoformat(day))
    assert bool(result["items"]) is expected


@pytest.mark.asyncio
async def test_progress_filters_paused_but_preserves_pagination(repo):
    from scheduler.app.services.cron.managed.task_progress import progress_page

    job(repo, "a", enabled=False)
    job(repo, "b", pause_reason="auto_unread_threshold")
    job(repo, "c", dispatch_state="batch_paused")
    job(repo, "d")
    first = await progress_page(repo, CALLER, date(2026, 10, 10), limit=3)
    assert first["items"] == [] and first["next_cursor"]
    second = await progress_page(
        repo,
        CALLER,
        date(2026, 10, 10),
        limit=3,
        cursor=first["next_cursor"],
    )
    assert [item["job_id"] for item in second["items"]] == ["d"]
    with pytest.raises(ValueError):
        await progress_page(
            repo,
            CALLER,
            date(2026, 10, 11),
            cursor=first["next_cursor"],
        )


@pytest.mark.asyncio
async def test_progress_http_and_database_failure(repo):
    from scheduler.app.routers.managed_cron import get_managed_repository
    from scheduler.app.routers.managed_cron_progress import router

    job(repo, "job")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_managed_repository] = lambda: repo
    headers = {"X-User-Id": "alice", "X-Source-Id": "bank"}
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        url = "/api/scheduler/cron/progress?date=2026-10-10"
        response = await client.get(url, headers=headers)
        assert response.status_code == 200
        assert response.json()["items"][0]["job_id"] == "job"
        assert (await client.get(url)).status_code == 400
        repo.db.broken = True
        assert (await client.get(url, headers=headers)).status_code == 503


@pytest.mark.asyncio
async def test_daily_query_does_not_calculate_unrelated_future_preview(repo):
    from unittest.mock import AsyncMock
    from scheduler.app.services.cron.managed.task_progress import progress_page

    job(repo, "job")
    observed = AsyncMock(wraps=repo.calendar.get_day)
    repo.calendar.get_day = observed
    await progress_page(repo, CALLER, date(2026, 10, 10))
    assert [call.args[0] for call in observed.await_args_list] == [
        date(2026, 10, 10),
    ]


@pytest.mark.asyncio
async def test_batch_child_uses_logical_business_date_and_holiday_override(
    repo,
):
    from scheduler.app.services.cron.managed.task_progress import progress_page

    job(repo, "child", "0 22 * * * 2026", dispatch_state="batch_enabled")
    raw = json.loads(
        repo.db.conn.execute(
            "SELECT spec_json FROM swe_cron_jobs_v2",
        ).fetchone()[0],
    )
    raw["meta"]["broadcast_original_cron"] = "0 2 * * * 2026"
    raw["schedule"]["run_on_non_workday"] = True
    repo.db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=?",
        (json.dumps(raw),),
    )
    result = await progress_page(repo, CALLER, date(2026, 10, 1))
    assert result["items"][0]["scheduled_time"] == "2026-10-01T02:00:00+08:00"


@pytest.mark.asyncio
async def test_swe_reader_and_scheduler_progress_real_http_chain(repo):
    from scheduler.app.routers.managed_cron import get_managed_repository
    from scheduler.app.routers.managed_cron_progress import router
    from swe.app.crons.managed_query_client import ManagedCronQueryClient

    job(repo, "job")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_managed_repository] = lambda: repo
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://scheduler",
    ) as http:
        result = await ManagedCronQueryClient(
            "http://scheduler/api",
            http=http,
        ).query_progress(
            "alice",
            "bank",
            "default",
            datetime.fromisoformat("2026-10-10T10:00:00+08:00"),
        )
    assert result[0]["job_id"] == "job"
    assert result[0]["meta"]["task_chat_id"] == "chat-job"
