# -*- coding: utf-8 -*-
"""Run against an explicitly configured, pre-provisioned isolated MySQL database."""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from monitor.app.routers import batch_status_report as monitor_api
from scheduler.app.database.connection import DatabaseConnection
from scheduler.app.routers import batch_status_reports as api
from scheduler.app.services.cron.batch_status_report.runtime import (
    BatchReportWorker,
)
from scheduler.app.services.cron.batch_status_report.store import (
    DELIVERIES,
    REPORTS,
    ReportStore,
)
from scheduler.config.constant import SchedulerDatabaseConfig


@pytest.fixture
async def report_mysql():
    raw = os.getenv("CRON_REPORT_TEST_MYSQL_JSON")
    if not raw:
        pytest.skip(
            "Requires a pre-provisioned isolated MySQL report test database",
        )
    settings = json.loads(raw)
    if not re.fullmatch(
        r"test_cron_report_[a-zA-Z0-9_]+",
        settings["database"],
    ):
        raise ValueError("isolated_report_database_required")
    db = DatabaseConnection(SchedulerDatabaseConfig(**settings))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def seed_batch(db, source, name, when, status, *, subtask=False):
    key = ("tenant", source, "default", name)
    meta = json.dumps(
        {
            "management_owner": "scheduler",
            "source_id": source,
            "agent_id": "default",
            "managed_execution_id": name,
        },
    )
    spec = json.dumps({"name": name, "task_type": "agent"})
    await db.execute(
        """INSERT INTO swe_cron_jobs_v2 (tenant_id,source_id,agent_id,job_id,scope_id,
           name,task_type,cron_expr,timezone,spec_json,management_owner,creator_user_id,created_at,updated_at)
           VALUES (%s,%s,%s,%s,'scope',%s,'agent','0 9 * * *','Asia/Shanghai',%s,
           'scheduler','tenant',%s,%s)""",
        (*key, name, spec, when, when),
    )
    await db.execute(
        """INSERT INTO swe_cron_job_states (tenant_id,source_id,agent_id,job_id,
           dispatch_state,management_status,created_at,updated_at)
           VALUES (%s,%s,%s,%s,'batch_enabled','ready',%s,%s)""",
        (*key, when, when),
    )
    history_id = None
    if subtask:
        trace = source + "-" + name
        await db.execute(
            """INSERT INTO swe_cron_executions (job_id,tenant_id,actual_time,status,trace_id,meta)
               VALUES (%s,'tenant',%s,'success',%s,%s)""",
            (name, when, trace, meta),
        )
        history = await db.fetch_one(
            "SELECT id FROM swe_cron_executions WHERE trace_id=%s",
            (trace,),
        )
        history_id = history["id"]
        await db.execute(
            """INSERT INTO swe_cron_subtasks (trace_id,task_id,filename,status)
               VALUES (%s,'a','fixture','FAIL'),(%s,'b','fixture','TIMEOUT')""",
            (trace, trace),
        )
    run_id = source + ":" + name
    if subtask:
        await db.execute(
            """UPDATE swe_cron_executions SET meta=JSON_SET(meta,'$.managed_execution_id',%s)
               WHERE id=%s""",
            (run_id, history_id),
        )
    await db.execute(
        """INSERT INTO swe_cron_execution_runs (execution_id,execution_key,tenant_id,source_id,
           agent_id,job_id,scope_id,actor_id,definition_version,provider_id,model_id,enqueued_state_version,
           kind,trigger_key,scheduled_fire_at,not_before_at,spec_json,dispatch_meta_json,status,
           history_execution_id,created_at,updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,'scope','tenant',1,'provider','model',1,'batch','period',%s,%s,
           %s,'{"protocol":"scheduler_managed_v1"}',%s,%s,%s,%s)""",
        (
            run_id,
            uuid4().hex,
            *key,
            when,
            when,
            spec,
            status,
            history_id,
            when,
            when,
        ),
    )
    manifest = json.dumps([{"identity": key, "execution_id": run_id}])
    await db.execute(
        """INSERT INTO swe_cron_managed_trigger_receipts (receipt_id,tenant_id,source_id,
           agent_id,job_id,external_job_id,operation_id,kind,logical_fire_at,physical_fire_at,
           batch_id,status,response_json,members_json,created_at)
           VALUES (%s,%s,%s,%s,%s,'external','operation','batch',%s,%s,%s,'accepted','{}',%s,%s)""",
        (run_id, *key, when, when, run_id, manifest, when),
    )
    await db.execute(
        """INSERT INTO swe_cron_managed_batch_members (batch_id,member_id,tenant_id,source_id,
           agent_id,job_id,initial_execution_id,current_execution_id,member_role,dispatch_order,
           created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'parent',0,%s,%s)""",
        (run_id, run_id, *key, run_id, run_id, when, when),
    )


async def downstream_response(request, monitor, attempts):
    body = json.loads(request.content)
    assert request.url.host == "swe"
    assert "x-internal-token" not in request.headers
    if request.url.path.endswith("/snapshot"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=monitor),
            base_url="http://monitor",
        ) as client:
            response = await client.post(
                "/api/monitor/internal/cron/batch-status-report-snapshot",
                json=body,
            )
        assert response.status_code == 200
        snapshot = response.json()
        assert len(snapshot["batches"]) == 1
        assert snapshot["batches"][0]["status"] == "running"
        assert snapshot["batches"][0]["subtasks"] == {
            "failures": 1,
            "partial": 0,
            "timeouts": 1,
            "incomplete": 0,
        }
        assert snapshot["batches"][0]["workers"][0]["occupied"] == 1
        assert snapshot["pools"][0]["occupied"] == 1
        assert snapshot["pools"][0]["capacity"] == 10
        return response
    attempts.append(body["recipient_id"])
    assert "异步子任务异常 2" in body["text"]
    assert "1 / 10" in body["text"]
    if body["recipient_id"] == "00000001":
        return httpx.Response(
            200,
            json={"status": "failed", "error_code": "zhaohu_rejected"},
        )
    raise httpx.ReadTimeout("timeout", request=request)


@pytest.mark.asyncio
async def test_mysql_each_call_creates_a_report_and_each_delivery_is_attempted_once(
    report_mysql,
    monkeypatch,
):
    db = report_mysql
    source = "report_" + uuid4().hex[:12]
    when = datetime.now(timezone.utc).replace(microsecond=0)
    await seed_batch(
        db,
        source,
        "running",
        when.replace(tzinfo=None),
        "success",
        subtask=True,
    )
    await seed_batch(
        db,
        source,
        "skipped",
        when.replace(tzinfo=None),
        "skipped",
    )
    await db.execute(
        """INSERT INTO swe_cron_dispatch_worker_capacity
           (worker_id,source_id,provider_id,model_id,effective_workers,running_count)
           VALUES ('fixture',%s,'provider','model',7,99),
                  ('fixture',%s,'provider','model',10,99)""",
        (source, source),
    )
    monkeypatch.delenv("SWE_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr(api, "get_db_connection", lambda: db)
    app = FastAPI()
    app.state.batch_report_ready = True
    app.include_router(api.router, prefix="/api")
    monitor = FastAPI()
    monitor.include_router(monitor_api.router, prefix="/api")
    monkeypatch.setattr(monitor_api, "get_db_connection", lambda: db)
    attempts = []

    async def downstream(request):
        return await downstream_response(request, monitor, attempts)

    route = "/api/scheduler/cron/batch-status-reports"
    request = {
        "source_id": source,
        "recipient_ids": ["00000001", "00000002", "00000003"],
    }
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://scheduler",
        ) as client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(downstream),
        ) as services,
    ):
        first, second = await asyncio.gather(
            *[client.post(route, json=request) for _ in range(2)],
        )
        assert first.status_code == second.status_code == 202
        identity = first.json()["report_id"]
        second_id = second.json()["report_id"]
        try:
            assert second_id != identity
            assert (
                second.json()["business_date"] == first.json()["business_date"]
            )
            await db.execute(
                f"UPDATE {REPORTS} SET available_at=UTC_TIMESTAMP(6)+INTERVAL 1 HOUR WHERE report_id=%s",
                (second_id,),
            )
            cross_source = await client.get(
                f"{route}/{identity}",
                params={"source_id": "another-source"},
            )
            assert cross_source.status_code == 404
            store = ReportStore(db)
            workers = [
                BatchReportWorker(store, services, "http://swe")
                for _ in range(2)
            ]
            claims = await asyncio.gather(
                store.collect_next(),
                store.collect_next(),
            )
            assert len(claimed := [row for row in claims if row]) == 1
            await workers[0].collect(claimed[0])
            interrupted = next(
                part
                for part in await store.pending_deliveries()
                if part["recipient_id"] == "00000003"
            )
            assert await store.start_delivery(interrupted)
            await db.execute(
                f"UPDATE {DELIVERIES} SET started_at=UTC_TIMESTAMP(6)-INTERVAL 90 SECOND "
                "WHERE delivery_id=%s",
                (interrupted["delivery_id"],),
            )
            await asyncio.gather(*(worker.tick() for worker in workers))
            await asyncio.gather(*(worker.tick() for worker in workers))
            result = (
                await client.get(
                    f"{route}/{identity}",
                    params={"source_id": source},
                )
            ).json()
            assert result["status"] == "completed_with_errors"
            assert [item["status"] for item in result["recipients"]] == [
                "failed",
                "outcome_unknown",
                "outcome_unknown",
            ]
            assert (
                result["recipients"][2]["issues"][0]["error_code"]
                == "cron_report_delivery_expired"
            )
            assert sorted(attempts) == ["00000001", "00000002"]
        finally:
            await db.execute(
                f"DELETE FROM {DELIVERIES} WHERE report_id=%s",
                (identity,),
            )
            await db.execute(
                f"DELETE FROM {REPORTS} WHERE report_id IN (%s,%s)",
                (identity, second_id),
            )
            await db.execute(
                "DELETE FROM swe_cron_subtasks WHERE trace_id=%s",
                (source + "-running",),
            )
            await db.execute(
                "DELETE FROM swe_cron_executions WHERE trace_id=%s",
                (source + "-running",),
            )
            for table in (
                "swe_cron_managed_batch_members",
                "swe_cron_managed_trigger_receipts",
                "swe_cron_execution_runs",
                "swe_cron_job_states",
                "swe_cron_jobs_v2",
                "swe_cron_dispatch_worker_capacity",
            ):
                await db.execute(
                    f"DELETE FROM {table} WHERE source_id=%s",
                    (source,),
                )
