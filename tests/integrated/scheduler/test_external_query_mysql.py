# -*- coding: utf-8 -*-
"""查询提交的独立真实MySQL验证，不引入系统任务表或SWE运行时。"""

import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from scheduler.app.database import connection
from scheduler.app.routers import api_router
from scheduler.app.services.cron import workday_calendar
from scheduler.app.services.cron.managed.external_query import STATE_FIELDS
from scheduler.app.services.cron.workday_calendar import (
    CalendarService,
    CalendarStore,
)
from tests.fakes.cron_profile_mysql import mysql_db, private_mysql

ROOT = Path(__file__).resolve().parents[3]


async def calendar_schema(db):
    sql = (ROOT / "scripts/sql/cron_workday_calendar.sql").read_text(
        encoding="utf-8",
    )
    for statement in sql.split(";"):
        if statement.strip():
            await db.execute(statement)


async def user_schema(db):
    identity = ",".join(
        f"{key} VARCHAR(128) NOT NULL"
        for key in ("tenant_id", "source_id", "agent_id", "job_id")
    )
    await db.execute(
        f"CREATE TABLE swe_cron_jobs_v2 ({identity},scope_id VARCHAR(512),name TEXT,task_type VARCHAR(16),enabled TINYINT,cron_expr VARCHAR(255),timezone VARCHAR(64),spec_json JSON,creator_user_id VARCHAR(128),management_owner VARCHAR(32),deleted_at DATETIME,PRIMARY KEY(tenant_id,source_id,agent_id,job_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
    )
    dates = {
        "auto_paused_at",
        "last_run_at",
        "last_prefetch_at",
        "last_scheduled_run_at",
    }
    numeric = {"has_scheduled_result", "unread_execution_count"}
    fields = ",".join(
        f"{key} {'DATETIME' if key in dates else 'BIGINT' if key in numeric else 'TEXT'} NULL"
        for key in STATE_FIELDS
    )
    await db.execute(
        f"CREATE TABLE swe_cron_job_states ({identity},{fields},PRIMARY KEY(tenant_id,source_id,agent_id,job_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
    )
    spec = '{"task_type":"text","text":"demo","schedule":{"cron":"0 9 * * *","timezone":"Asia/Shanghai"},"meta":{},"dispatch":{"target":{"user_id":"alice","session_id":"s1"}}}'
    await db.execute(
        "INSERT INTO swe_cron_jobs_v2 VALUES ('alice','bank','default','new-job','scope','demo','text',1,'0 9 * * *','Asia/Shanghai',%s,'alice','scheduler',NULL)",
        (spec,),
    )
    await db.execute(
        "INSERT INTO swe_cron_job_states (tenant_id,source_id,agent_id,job_id,task_session_id,has_scheduled_result,unread_execution_count) VALUES ('alice','bank','default','new-job','s1',1,8)",
    )


@pytest.mark.asyncio
async def test_new_external_http_reads_real_mysql_and_only_writes_calendar(
    mysql_db,
    monkeypatch,
):
    await calendar_schema(mysql_db)
    await user_schema(mysql_db)
    service = CalendarService(
        store=CalendarStore(mysql_db),
        fetcher=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(connection, "get_db_connection", lambda: mysql_db)
    monkeypatch.setattr(workday_calendar, "_calendar_service", service)
    before_jobs = await mysql_db.fetch_all("SELECT * FROM swe_cron_jobs_v2")
    before_states = await mysql_db.fetch_all(
        "SELECT * FROM swe_cron_job_states",
    )
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://scheduler",
    ) as client:
        response = await client.get(
            "/api/scheduler/external/cron/jobs",
            headers={"X-Tenant-Id": "alice", "X-Source-Id": "bank"},
        )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["new-job"]
    assert response.json()[0]["task"]["unread_execution_count"] == 8
    assert len(response.json()[0]["state"]["next_run_times"]) == 3
    assert (
        await mysql_db.fetch_all("SELECT * FROM swe_cron_jobs_v2")
        == before_jobs
    )
    assert (
        await mysql_db.fetch_all("SELECT * FROM swe_cron_job_states")
        == before_states
    )
    assert await mysql_db.fetch_all("SELECT * FROM swe_workday_calendar")


@pytest.mark.asyncio
async def test_calendar_instances_share_a_real_database_refresh_lease(
    mysql_db,
):
    await calendar_schema(mysql_db)
    fetcher = AsyncMock(return_value=1)
    first = CalendarService(store=CalendarStore(mysql_db), fetcher=fetcher)
    second = CalendarService(store=CalendarStore(mysql_db), fetcher=fetcher)
    results = await asyncio.gather(
        first.get_day(date(2030, 1, 1)),
        second.get_day(date(2030, 1, 1)),
    )
    assert all(
        item["source"] == "api" and item["is_workday"] for item in results
    )
    assert fetcher.await_count == 1
    assert (
        len(await mysql_db.fetch_all("SELECT * FROM swe_workday_calendar"))
        == 1
    )
