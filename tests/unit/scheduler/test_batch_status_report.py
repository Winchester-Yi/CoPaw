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
from scheduler.app.services.cron.batch_status_report.render import (
    batch_lines,
    render_parts,
)
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
@pytest.mark.parametrize("followed_scope", [True, False])
async def test_report_snapshot_and_delivery_use_internal_swe_address(
    monkeypatch,
    followed_scope,
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
            snapshot = {
                "snapshot_at": "2026-11-24T04:00:00+00:00",
                "batches": [],
                "pools": [],
            }
            if followed_scope:
                snapshot["batch_scope"] = "followed_batch"
            return httpx.Response(
                200,
                json=snapshot,
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
    if followed_scope:
        store.freeze.assert_awaited_once()
    else:
        store.freeze.assert_not_awaited()
        store.collection_failed.assert_awaited_once_with(
            {"source_id": "bank", "business_date": "2026-11-24"},
            "cron_report_snapshot_scope_unavailable",
            True,
        )
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


def test_render_marks_follow_level_only_when_followed():
    from scheduler.app.services.cron.batch_status_report.render import (
        render_parts,
    )

    base = {
        "batch_id": "b1",
        "name": "客户计划",
        "logical_fire_at": "2026-11-24T01:00:00+00:00",
        "status": "completed",
        "member_count": 1,
        "done_count": 1,
        "success_count": 1,
        "failed_count": 0,
        "skipped_count": 0,
        "active_count": 0,
        "waiting_count": 0,
        "subtasks": {},
        "failure_details": None,
        "workers": [],
    }
    pools = [
        {
            "provider_id": "p",
            "model_id": "m",
            "occupied": 0,
            "capacity": 10,
        }
    ]
    followed = render_parts(
        "S",
        "2026-11-24",
        {
            "snapshot_at": "2026-11-24T04:00:00+00:00",
            "batches": [{**base, "follow_level": 3}],
            "pools": pools,
        },
        1000,
    )
    assert "关注 L3" in "\n".join(followed)
    plain = render_parts(
        "S",
        "2026-11-24",
        {
            "snapshot_at": "2026-11-24T04:00:00+00:00",
            "batches": [{**base, "follow_level": 0}],
            "pools": pools,
        },
        1000,
    )
    assert "关注 L" not in "\n".join(plain)


@pytest.mark.parametrize(
    ("status", "finished_at", "expected"),
    [
        ("completed", "2026-11-24T03:25:00+00:00", "结束 11:25"),
        ("failed", "2026-11-24T16:05:00+00:00", "结束 2026-11-25 00:05"),
        ("running", "2026-11-24T03:25:00+00:00", ""),
        ("completed", None, ""),
    ],
)
def test_batch_lines_show_finished_time_for_terminal_batches(
    status,
    finished_at,
    expected,
):
    batch = {
        "name": "客户名单生成",
        "logical_fire_at": "2026-11-24T03:00:00+00:00",
        "status": status,
        "finished_at": finished_at,
        "done_count": 1,
        "member_count": 1,
        "success_count": 1,
        "failed_count": 0,
        "skipped_count": 0,
        "active_count": 0,
        "waiting_count": 0,
        "workers": [],
    }
    first = batch_lines(batch)[0]
    if expected:
        assert first.endswith(f" · {expected}")
    else:
        assert " · 结束 " not in first


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
    assert "暂无可报告的关注批次" in parts[0]
    assert "7 / 10" in parts[0]
    assert "关注批次共 0 个" in parts[0]
    assert "第 1/1 段" not in parts[0]


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
                "name": f"客户计划 {index:02}",
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
                "failure_details": {
                    "main_count": 0,
                    "subtask_types": {"API + Prompt": 1},
                },
                "workers": [],
            },
        )
    parts = render_parts("S", "2026-11-24", snapshot, 1024)
    assert len(parts) > 1
    assert all(len(part.encode()) <= 1024 for part in parts)
    summary, *details = parts
    assert "关注批次共 20 个 · 已失败 20" in summary
    assert "全来源共享模型池" in summary
    assert "客户计划" not in summary
    for position, part in enumerate(details, 1):
        assert f"第 {position}/{len(details)} 段" in part
        assert "批次共" not in part
    text = "\n".join(details)
    assert text.count("主任务失败 0 · 子任务失败 1（API + Prompt 1）") == 20
    assert all(text.count(f"客户计划 {index:02}") == 1 for index in range(20))
    assert "batch-" not in text


def test_render_oversized_batch_continuation_uses_name_and_time():
    workers = [
        {
            "provider_id": "provider",
            "model_id": f"model-{index:02}",
            "occupied": 1,
        }
        for index in range(20)
    ]
    snapshot = {
        "snapshot_at": "2026-11-24T04:00:00+00:00",
        "pools": [],
        "batches": [
            {
                "batch_id": "private-batch-id",
                "name": "客户计划",
                "logical_fire_at": "2026-11-24T03:00:00+00:00",
                "status": "running",
                "done_count": 0,
                "member_count": 20,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "active_count": 20,
                "waiting_count": 0,
                "subtasks": {},
                "workers": workers,
            },
        ],
    }
    summary, *details = render_parts("S", "2026-11-24", snapshot, 650)
    assert "客户计划" not in summary
    assert len(details) > 1
    assert all(len(part.encode()) <= 650 for part in details)
    text = "\n".join(details)
    assert "11:00 · 客户计划 · 运行中（续）" in text
    assert "private-batch-id" not in text
    assert "失败成员" not in text
    assert "异步子任务" not in text
    assert "主任务失败" not in text
    assert all(text.count(worker["model_id"]) == 1 for worker in workers)


@pytest.mark.parametrize(
    ("failure_details", "expected"),
    [
        (
            {"main_count": 1, "subtask_types": {"API + Prompt": 2, "超时": 1}},
            ["主任务失败 1 · 子任务失败 3（API + Prompt 2、超时 1）"],
        ),
        (
            {
                "main_count": 1,
                "subtask_types": {kind * 80: 1 for kind in "ABC"},
            },
            [
                "主任务失败 1 · 子任务失败 3",
                *(f"子任务失败类型 · {kind * 80} 1" for kind in "ABC"),
            ],
        ),
        (None, ["失败分类暂不可用"]),
    ],
)
def test_batch_lines_render_failed_members_by_stage(failure_details, expected):
    batch = {
        "name": "客户计划",
        "logical_fire_at": "2026-11-24T03:00:00+00:00",
        "status": "failed",
        "done_count": 4,
        "member_count": 4,
        "success_count": 0,
        "failed_count": 4,
        "skipped_count": 0,
        "active_count": 0,
        "waiting_count": 0,
        "subtasks": {"failures": 8},
        "workers": [],
    }
    if failure_details is not None:
        batch["failure_details"] = failure_details
    assert batch_lines(batch)[3:] == expected


def test_render_overflow_model_pools_keep_one_summary_and_all_pools():
    pools = [
        {
            "provider_id": "provider",
            "model_id": f"model-{index:02}",
            "occupied": index,
            "capacity": 20,
        }
        for index in range(20)
    ]
    snapshot = {
        "snapshot_at": "2026-11-24T04:00:00+00:00",
        "batches": [],
        "pools": pools,
    }
    summary, *details = render_parts("S", "2026-11-24", snapshot, 500)
    assert "关注批次共 0 个" in summary
    assert "共享模型池见后续明细" in summary
    assert all(len(part.encode()) <= 500 for part in (summary, *details))
    text = "\n".join(details)
    assert "共享模型池" in text
    assert all(text.count(pool["model_id"]) == 1 for pool in pools)
