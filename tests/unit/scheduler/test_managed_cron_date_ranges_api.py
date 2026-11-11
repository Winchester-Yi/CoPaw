# -*- coding: utf-8 -*-
"""日期配置沿用用户 Header，维护入口只接受来源并返回异步受理。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    group,
    mode_setup,
)


@pytest.mark.asyncio
async def test_date_configuration_and_headerless_maintenance_http(mode_setup):
    from scheduler.app.routers import (
        api_router,
        managed_cron_controls,
        managed_cron,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )
    from scheduler.app.services.cron.managed.controls import ControlService

    db, _, management = mode_setup
    job = await group(mode_setup)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[managed_cron.get_managed_repository] = (
        lambda: ManagedCronRepository(db)
    )
    app.dependency_overrides[managed_cron_controls.management_database] = (
        lambda: db
    )
    app.dependency_overrides[managed_cron_controls.control_service] = (
        lambda: ControlService(db, management.platform)
    )
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states WHERE job_id=?",
        (job,),
    ).fetchone()[0]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        saved = await client.put(
            f"/api/scheduler/cron/jobs/{job}/run-date-range",
            headers={"X-User-Id": "alice", "X-Source-Id": "bank"},
            json={
                "request_id": "dates-http",
                "expected_management_version": version,
                "run_date_range": {
                    "start_date": "2000-01-01",
                    "end_date": "2000-01-02",
                },
            },
        )
        assert saved.status_code == 200
        assert saved.json()["operation"]["status"] == "completed"
        children = await client.get(
            f"/api/scheduler/cron/jobs/{job}/broadcast/children",
            headers={"X-User-Id": "alice", "X-Source-Id": "bank"},
        )
        assert children.status_code == 200
        assert children.json()["items"]
        assert all(
            item.get("run_date_range")
            == {"start_date": "2000-01-01", "end_date": "2000-01-02"}
            for item in children.json()["items"]
        )
        missing_source = await client.post(
            "/api/scheduler/cron/date-ranges/apply",
            json={},
        )
        assert missing_source.status_code == 422
        # 维护服务用真实日期算法与 SQL，只在平台边界替换 HTTP。
        from scheduler.app.routers import managed_cron_date_ranges
        from scheduler.app.services.cron.managed.date_ranges import (
            DateMaintenanceService,
        )

        app.dependency_overrides[managed_cron_date_ranges.date_service] = (
            lambda: DateMaintenanceService(db, management.platform)
        )
        intent_query = (
            "SELECT d.job_id,d.enabled,s.dispatch_state FROM swe_cron_jobs_v2 d "
            "JOIN swe_cron_job_states s ON d.job_id=s.job_id ORDER BY d.job_id"
        )
        intent_before = [tuple(row) for row in db.conn.execute(intent_query)]
        management.platform.trace.clear()
        applied = await client.post(
            "/api/scheduler/cron/date-ranges/apply",
            json={"source_id": "bank"},
        )
        assert applied.status_code == 202
        assert applied.json() == {"accepted": True, "source_id": "bank"}
        assert management.platform.trace
        assert all(not enabled for _, enabled in management.platform.trace)
        assert [
            tuple(row) for row in db.conn.execute(intent_query)
        ] == intent_before
        assert all(
            not row[0]
            for row in db.conn.execute(
                "SELECT date_window_sync_required FROM swe_cron_job_states",
            )
        )
