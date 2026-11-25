# -*- coding: utf-8 -*-
import json
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from scheduler.app.routers import batch_status_reports as report_api
from scheduler.app.routers.batch_status_reports import ReportRequest
from scheduler.app.services.cron.batch_status_report.render import render_parts
from scheduler.app.services.cron.batch_status_report import (
    runtime,
    store as report_store,
)
from scheduler.app.services.cron.batch_status_report.store import (
    recipient_status,
)


@pytest.mark.asyncio
async def test_platform_submits_directly_without_identity_headers(monkeypatch):
    app = FastAPI()
    app.state.batch_report_ready = True
    app.include_router(report_api.router, prefix="/api")
    store = AsyncMock()
    store.submit.return_value = {"report_id": "original-report"}
    monkeypatch.setattr(report_api, "get_db_connection", object)
    monkeypatch.setattr(report_api, "ReportStore", lambda _: store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://scheduler",
    ) as client:
        response = await client.post(
            "/api/scheduler/cron/batch-status-reports",
            json={
                "source_id": "S",
                "recipient_ids": ["00000002"],
            },
        )
    assert response.status_code == 202
    assert response.json()["report_id"] == "original-report"
    store.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_report_worker_requires_dedicated_swe_address(
    monkeypatch,
    configured,
):
    monkeypatch.setenv("SCHEDULER_SWE_API_BASE_URL", "http://gateway/api/")
    monkeypatch.delenv("SCHEDULER_SWE_INTERNAL_API_BASE_URL", raising=False)
    if configured:
        monkeypatch.setenv(
            "SCHEDULER_SWE_INTERNAL_API_BASE_URL",
            "http://swe-internal/api/",
        )
    monkeypatch.delenv("SWE_INTERNAL_TOKEN", raising=False)
    store = AsyncMock()
    worker = Mock(return_value=AsyncMock())
    monkeypatch.setattr(runtime, "get_db_connection", object)
    monkeypatch.setattr(runtime, "ReportStore", lambda _: store)
    monkeypatch.setattr(runtime, "BatchReportWorker", worker)
    app = FastAPI()
    async with runtime.report_lifespan(app):
        assert app.state.batch_report_ready == configured
    assert not app.state.batch_report_ready
    if configured:
        store.ready.assert_awaited_once()
        assert worker.call_args.args[2] == "http://swe-internal"
    else:
        worker.assert_not_called()


@pytest.mark.asyncio
async def test_report_snapshot_and_delivery_use_internal_swe_address(
    monkeypatch,
):
    monkeypatch.setenv("SWE_INTERNAL_TOKEN", "report-test-token")
    store = AsyncMock()
    sources = {}

    def swe_gateway(request):
        body = json.loads(request.content)
        assert request.url.host == "swe-internal"
        assert (
            request.headers["X-Internal-Token"] == "Bearer report-test-token"
        )
        endpoint = request.url.path.rsplit("/", 1)[-1]
        sources[endpoint] = body["source_id"]
        if endpoint == "snapshot":
            return httpx.Response(
                200,
                json={
                    "snapshot_at": "2026-11-24T04:00:00+00:00",
                    "batches": [],
                    "pools": [],
                },
            )
        return httpx.Response(200, json={"status": "sent"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(swe_gateway),
    ) as client:
        worker = runtime.BatchReportWorker(
            store,
            client,
            "http://swe-internal",
        )
        await worker.collect(
            {"source_id": "bank", "business_date": "2026-11-24"},
        )
        await worker.deliver({"source_id": "retail"})

    assert sources == {"snapshot": "bank", "send-part": "retail"}
    store.freeze.assert_awaited_once()
    store.finish_delivery.assert_awaited_once_with(
        {"source_id": "retail"},
        "sent",
        None,
        None,
    )


def test_platform_request_normalizes_recipient_set():
    request = ReportRequest(
        source_id="S",
        recipient_ids=["00000002", "00000001", "00000002"],
    )
    assert request.recipient_ids == ["00000001", "00000002"]
    with pytest.raises(ValidationError):
        ReportRequest(source_id="S", recipient_ids=["employee"])


@pytest.mark.asyncio
async def test_new_report_uses_beijing_acceptance_date_across_midnight(
    monkeypatch,
):
    request = ReportRequest(source_id="S", recipient_ids=["00000001"])
    db = AsyncMock()
    store = report_store.ReportStore(db)
    monkeypatch.setattr(
        report_store,
        "now_utc",
        lambda: datetime(2026, 11, 24, 15, 59),
    )
    first = await store.submit(request)
    monkeypatch.setattr(
        report_store,
        "now_utc",
        lambda: datetime(2026, 11, 24, 16),
    )
    second = await store.submit(request)

    assert first["report_id"] != second["report_id"]
    assert first["business_date"] == "2026-11-24"
    assert second["business_date"] == "2026-11-25"
    assert second["accepted_at"] == "2026-11-24T16:00:00+00:00"
    assert db.execute.await_count == 2


def test_render_empty_report_keeps_current_worker_capacity():
    snapshot = {
        "snapshot_at": "2026-11-24T04:00:00+00:00",
        "batches": [],
        "pools": [
            {
                "provider_id": "A",
                "model_id": "B",
                "occupied": 7,
                "capacity": 10,
            },
        ],
    }
    parts = render_parts("S", "2026-11-24", snapshot, 1000)
    assert len(parts) == 1
    assert "暂无可报告的批次" in parts[0]
    assert "7 / 10" in parts[0]


def test_recipient_projection_preserves_failure_and_unknown():
    assert (
        recipient_status(["sent", "failed"], "completed_with_errors")
        == "partial"
    )
    assert (
        recipient_status(["outcome_unknown"], "completed_with_errors")
        == "outcome_unknown"
    )
    assert recipient_status(["failed"], "completed_with_errors") == "failed"
    assert recipient_status([], "failed") == "not_started"


def test_render_splits_all_batches_without_losing_failure_counts():
    snapshot = {
        "snapshot_at": "2026-11-24T04:00:00+00:00",
        "pools": [],
        "batches": [],
    }
    for index in range(20):
        snapshot["batches"].append(
            {
                "batch_id": f"batch-{index:02}",
                "name": "客户计划" * 4,
                "logical_fire_at": "2026-11-24T03:00:00+00:00",
                "status": "failed",
                "done_count": 1,
                "member_count": 1,
                "success_count": 0,
                "failed_count": 1,
                "skipped_count": 0,
                "active_count": 0,
                "waiting_count": 0,
                "subtasks": {"failures": 2},
                "workers": [],
            },
        )
    parts = render_parts("S", "2026-11-24", snapshot, 1024)
    assert len(parts) > 1
    assert all(len(part.encode()) <= 1024 for part in parts)
    for position, part in enumerate(parts, 1):
        assert f"第 {position}/{len(parts)} 段" in part
    text = "\n".join(parts)
    assert text.count("异步子任务异常 2") == 20
    assert all(text.count(f"batch-{index:02}") == 1 for index in range(20))
