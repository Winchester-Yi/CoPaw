# -*- coding: utf-8 -*-
"""Monitor 新看板的严格来源角色、缺表失败和只读接口验证。"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_callbacks import (
    body,
    callback_setup,
    create_source,
)
from tests.unit.scheduler.test_managed_batch_retry import failed_batch


@pytest.mark.asyncio
async def test_member_presence_filters_both_board_endpoints(callback_setup):
    from monitor.app.routers import managed_cron_board as api
    from monitor.app.services.cron.managed_board import ManagedBoardQuery
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    _, failed_id = await failed_batch(callback_setup)
    job = await create_source(callback_setup, "batch_enabled")
    db.conn.execute("UPDATE swe_cron_jobs_v2 SET enabled=0")
    db.conn.execute(
        "UPDATE swe_cron_job_states SET pause_reason='auto_unread_threshold'",
    )
    empty = await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(
        caller,
        body(db, job, kind="batch", trigger="empty"),
        "batch",
    )
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.board_service] = lambda: ManagedBoardQuery(db)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test/monitor/cron/managed-dispatch/",
        headers={
            "X-Source-Id": "bank",
            "X-User-Id": "alice",
            "X-User-Role": "manager",
        },
    ) as client:
        for value, batch_id, members in (
            ("true", failed_id, 1),
            ("false", empty["batch_id"], 0),
        ):
            params = {"has_members": value, "page_size": 1}
            result = (await client.get("batches", params=params)).json()
            assert result["total"] == 1
            assert [item["batch_id"] for item in result["items"]] == [batch_id]
            assert result["stats"]["total_members"] == members
            assert result["stats"]["failed_members"] == members
            failures = (
                await client.get("failure-summary", params=params)
            ).json()
            assert failures["total"] == members
        combined = await client.get(
            "batches",
            params={"has_members": "false", "status": "failed"},
        )
        assert combined.json()["total"] == 0
        db.conn.execute(
            "UPDATE swe_cron_managed_trigger_receipts SET members_json='{}' "
            "WHERE batch_id=?",
            (empty["batch_id"],),
        )
        for endpoint in ("batches", "failure-summary"):
            invalid = await client.get(
                endpoint,
                params={"has_members": "true"},
            )
            assert invalid.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "manual"])
async def test_worker_occupants_scopes_totals_and_current_attempt(
    callback_setup,
    kind,
):
    from monitor.app.routers import managed_cron_board as api
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, _ = callback_setup
    await failed_batch(callback_setup)
    job_id = db.conn.execute("SELECT job_id FROM swe_cron_jobs_v2").fetchone()[
        0
    ]
    db.conn.executescript("""
        UPDATE swe_cron_execution_runs
        SET kind='ordinary', status='success', settled_status=NULL,
            spec_json=JSON_SET(spec_json,'$.tenant_name','Alice','$.task_type','agent'),
            dispatch_meta_json=JSON_SET(
                dispatch_meta_json,'$.daily_failure_retry.call_id','call-one'
            );
        CREATE TABLE swe_cron_dispatch_intents (
            id INTEGER PRIMARY KEY, source_id TEXT COLLATE NOCASE, provider_id TEXT,
            model_id TEXT, tenant_id TEXT, job_id TEXT, batch_id TEXT,
            status TEXT, attempt_count INTEGER
        );
        INSERT INTO swe_cron_dispatch_intents VALUES
            (1,'bank','legacy','model','bob','old','batch','claimed',1),
            (2,'bank','legacy','model','bob','old','batch','pending',1),
            (3,'other','legacy','model','bob','old','batch','dispatched',1),
            (4,'Bank','legacy','model','bob','old','batch','dispatched',1);
        INSERT INTO swe_cron_jobs (id,name,tenant_id,source_id,tenant_name)
        VALUES ('old','Old job','bob','bank','Bob');
        ALTER TABLE swe_cron_executions ADD COLUMN dispatch_intent_id INTEGER;
        ALTER TABLE swe_cron_executions ADD COLUMN dispatch_batch_id TEXT;
        ALTER TABLE swe_cron_executions ADD COLUMN dispatch_attempt INTEGER;
    """)
    standalone, _ = await ExecutionStore(db).enqueue(
        caller,
        job_id,
        ExecutionRequest(kind, "standalone", datetime.now(timezone.utc)),
    )
    await db.execute(
        "UPDATE swe_cron_execution_runs SET status='running' WHERE execution_id=%s",
        (standalone["execution_id"],),
    )
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.board_service] = lambda: ManagedBoardQuery(db)
    headers = {
        "X-Source-Id": "bank",
        "X-User-Id": "alice",
        "X-User-Role": "manager",
    }
    path = "/monitor/cron/managed-dispatch/workers/occupants"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        by_tenant = {item["tenant_id"]: item for item in data["items"]}
        assert by_tenant["alice"]["tenant_name"] == "Alice"
        assert by_tenant["alice"]["execution_type"] == "daily_failure_retry"
        assert by_tenant["alice"]["status"] == "waiting_subtasks"
        assert by_tenant["alice"]["started_at"]
        assert by_tenant["bob"]["tenant_name"] == "Bob"
        assert by_tenant["bob"]["job_name"] == "Old job"
        assert by_tenant["bob"]["started_at"] is None
        assert by_tenant["bob"]["execution_type"] == "legacy_batch"
        page = await client.get(
            path,
            headers=headers,
            params={"provider_id": "legacy", "model_id": "model", "page": 2},
        )
        assert page.json()["total"] == 1
        assert page.json()["items"] == []
        forbidden = await client.get(
            path,
            headers={**headers, "X-User-Role": "user"},
        )
        assert forbidden.status_code == 403
        db.conn.execute("UPDATE swe_cron_execution_runs SET status='queued'")
        pending = await client.get(path, headers=headers)
        assert pending.json()["total"] == 1
        db.conn.execute(
            "UPDATE swe_cron_execution_runs SET status='running', "
            "spec_json=JSON_SET(spec_json,'$.task_type','text')",
        )
        text_only = await client.get(path, headers=headers)
        assert text_only.json()["total"] == 1
        assert text_only.json()["items"][0]["tenant_id"] == "bob"


@pytest.mark.asyncio
async def test_board_api_does_not_fallback_on_missing_schema(callback_setup):
    from monitor.app.routers import managed_cron_board as api
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = callback_setup[0]
    _, batch = await failed_batch(callback_setup)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.board_service] = lambda: ManagedBoardQuery(db)
    path = "/api/monitor/cron/managed-dispatch/batches"
    headers = {
        "X-Source-Id": "bank",
        "X-User-Id": "alice",
        "X-User-Role": "manager",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (
            await client.get(path, headers={**headers, "X-User-Role": "user"})
        ).status_code == 403
        assert (await client.get(path, headers=headers)).json()["total"] == 1
        assert (
            await client.get(path, headers=headers, params={"page": 0})
        ).status_code == 422
        assert (
            await client.get(
                path,
                headers=headers,
                params={"start_time": "2026-10-18", "end_time": "2026-10-17"},
            )
        ).status_code == 400
        assert (
            await client.get(
                path + "/" + batch,
                headers={**headers, "X-Source-Id": "other"},
            )
        ).status_code == 404
        detail_path = path + "/" + batch
        detail = (await client.get(detail_path, headers=headers)).json()
        execution = detail["members"][0]["execution_id"]
        response = await client.get(
            detail_path,
            headers=headers,
            params={"event_execution_id": execution, "event_type": "error"},
        )
        assert response.status_code == 200
        assert response.json()["event_total"] == 1
        assert response.json()["events"][0]["execution_id"] == execution
        response = await client.get(
            detail_path,
            headers=headers,
            params={
                "event_execution_id": "other-execution",
                "event_type": "error",
            },
        )
        assert response.json()["event_total"] == 0
        assert response.json()["events"] == []
        db.conn.execute("DROP TABLE swe_cron_managed_dispatch_events")
        assert (await client.get(path, headers=headers)).status_code == 503
