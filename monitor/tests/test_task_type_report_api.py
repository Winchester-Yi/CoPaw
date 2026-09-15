# -*- coding: utf-8 -*-
"""在正式 app 上验证新接口契约，避免触发数据库启动生命周期。"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from monitor.app._app import app
from monitor.app.models.task_type_report import TaskTypeReportResponse
from monitor.app.routers import task_type_report as report_router
from monitor.app.services.cron.task_type_report import (
    ReportError,
    get_task_type_report_service,
)

URL = "/api/monitor/cron/task-type-report"
DATES = {"start_date": "2026-09-01", "end_date": "2026-09-13"}


@pytest.fixture
def service(monkeypatch):
    mock = AsyncMock()
    mock.get_report.return_value = TaskTypeReportResponse(
        **DATES,
        source_id="S",
        sync_date="2026-09-13",
        group_by="overall",
        resolved_filters={},
        warnings=["period_ratios_not_cohort_conversion"],
        items=[],
    )
    monkeypatch.setitem(
        app.dependency_overrides, get_task_type_report_service, lambda: mock
    )
    return mock


async def request(params=None, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(
            URL,
            params=DATES if params is None else params,
            headers=(
                {"X-Source-Id": "S", "X-Bbk-Id": "100"}
                if headers is None
                else headers
            ),
        )


@pytest.mark.asyncio
async def test_contract_and_normalized_parameters(service):
    response = await request(
        {
            **DATES,
            "first_bbk_id": " 001 ",
            "org_name": " 同名支行 ",
            "group_by": "org",
        },
        {"X-Source-Id": " S ", "X-Bbk-Id": "100"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["metric_version"] == "jkh_task_report_v1"
    assert body["ratio_unit"] == "percent"
    assert body["read_evidence"]["ask"] == "assumed_from_success"
    assert body["resolved_filters"] == {"first_bbk_id": None, "org_id": None}
    params, source, bbk = service.get_report.call_args.args
    assert source == "S" and params.first_bbk_id == "001"
    assert params.org_name == "同名支行" and params.group_by == "org"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"start_date": "2026-09-01"},
        {**DATES, "start_date": "2026-9-01"},
        {**DATES, "start_date": "2026-02-30"},
        {**DATES, "start_date": "1756684800"},
        {**DATES, "end_date": "2026-09-13T00:00:00"},
        {**DATES, "end_date": "2026-08-31"},
        {**DATES, "end_date": "2026-12-03"},
        {"start_date": "9999-12-31", "end_date": "9999-12-31"},
        {**DATES, "group_by": "org_id; DROP TABLE x"},
        {**DATES, "first_bbk_id": " "},
        {**DATES, "first_bbk_name": ""},
        {**DATES, "org_id": " "},
        {**DATES, "org_name": " "},
        {**DATES, "org_id": "x" * 65},
        {**DATES, "org_name": "x" * 257},
    ],
)
async def test_invalid_queries_are_422_without_database_work(service, params):
    response = await request(params)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "report_validation_error"
    service.get_report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "", " ", "x" * 65])
async def test_invalid_source(service, source):
    response = await request(
        headers={} if source is None else {"X-Source-Id": source}
    )
    assert response.status_code == 422
    service.get_report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "2026-09-01", "end_date": "2026-09-01"},
        {"start_date": "2026-09-01", "end_date": "2026-12-02"},
        {"start_date": "2024-02-29", "end_date": "2024-03-01"},
    ],
)
async def test_valid_date_boundaries(service, dates):
    assert (await request(dates)).status_code == 200
    service.get_report.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (422, "organization_name_ambiguous"),
        (503, "jkh_roster_ambiguous"),
        (503, "report_database_unavailable"),
    ],
)
async def test_structured_service_errors(service, status, code):
    service.get_report.side_effect = ReportError(status, code, "测试错误")
    response = await request()
    assert response.status_code == status
    assert response.json()["detail"] == {"code": code, "message": "测试错误"}


@pytest.mark.asyncio
async def test_failed_query_does_not_expose_database_or_return_partial_data(
    service,
):
    service.get_report.side_effect = RuntimeError("private SQL/password")
    response = await request()
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "report_query_failed"
    assert "private" not in response.text and "items" not in response.json()


@pytest.mark.asyncio
async def test_timeout_cancels_request(service, monkeypatch):
    cancelled = asyncio.Event()

    async def blocked(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(report_router, "REPORT_TIMEOUT_SECONDS", 0.01)
    service.get_report.side_effect = blocked
    response = await request()
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "report_query_timeout"
    assert cancelled.is_set()


def test_openapi_registers_new_route_and_keeps_old_routes():
    schema = app.openapi()
    assert URL in schema["paths"]
    assert "/api/monitor/cron/branch-behavior" in schema["paths"]
    assert "/api/monitor/cron/export-branch-dimension" in schema["paths"]
    operation = schema["paths"][URL]["get"]
    assert {p["name"] for p in operation["parameters"]} == {
        "start_date",
        "end_date",
        "group_by",
        "skill_detail",
        "first_bbk_id",
        "first_bbk_name",
        "org_id",
        "org_name",
        "X-Source-Id",
        "X-Bbk-Id",
        "task_type",
        "user_id",
        "keyword",
        "page",
        "page_size",
    }
    assert next(
        p for p in operation["parameters"] if p["name"] == "X-Source-Id"
    )["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", ["branch", "org", "manager"])
@pytest.mark.parametrize("skill_detail", [False, True])
async def test_six_dimension_combinations(service, group_by, skill_detail):
    response = await request(
        {
            **DATES,
            "group_by": group_by,
            "skill_detail": str(skill_detail).lower(),
        }
    )
    assert response.status_code == 200
    params, _, _ = service.get_report.call_args.args
    assert params.group_by == group_by
    assert params.skill_detail is skill_detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {**DATES, "skill_detail": "true"},
        {**DATES, "group_by": "manager", "skill_detail": "not-a-bool"},
    ],
)
async def test_invalid_skill_dimension(service, params):
    assert (await request(params)).status_code == 422
    service.get_report.assert_not_awaited()
