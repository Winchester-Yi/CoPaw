# -*- coding: utf-8 -*-
"""Cron planned firing distribution service and API contract tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from monitor.app.models.cron import TaskType
from monitor.app.services.cron.query_service import (
    QueryService,
    ScheduleCalculationLimitExceededError,
    ScheduleDefinitionRevisionConflictError,
    ScheduleDistributionValidationError,
)


UTC = timezone.utc


class FakeDb:
    """Return source-scoped cron definitions and record the SQL boundary."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch_all(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        source_id = str(params[0])
        return [
            row
            for row in self.rows
            if row.get("source_id") == source_id
            and row.get("enabled") is True
            and row.get("status") == "active"
            and row.get("deleted_at") is None
        ]


def job_row(
    job_id: str,
    *,
    source_id: str = "source-a",
    name: str | None = None,
    tenant_name: str = "Test User",
    tenant_id: str = "test-account",
    task_type: str = "text",
    cron_expr: str = "*/5 * * * *",
    timezone_name: str = "UTC",
    enabled: bool = True,
    status: str = "active",
    deleted_at: datetime | None = None,
    meta: Any = "",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name or job_id,
        "tenant_name": tenant_name,
        "tenant_id": tenant_id,
        "source_id": source_id,
        "enabled": enabled,
        "status": status,
        "deleted_at": deleted_at,
        "task_type": task_type,
        "cron_expr": cron_expr,
        "timezone": timezone_name,
        "meta": meta,
        "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
    }


@pytest.fixture
def patch_database(monkeypatch: pytest.MonkeyPatch):
    def apply(rows: list[dict[str, Any]]) -> FakeDb:
        fake_db = FakeDb(rows)
        monkeypatch.setattr(
            "monitor.app.services.cron.query_service.get_db_connection",
            lambda: fake_db,
        )
        return fake_db

    return apply


@pytest.mark.asyncio
async def test_distribution_counts_occurrences_and_half_open_boundaries(
    patch_database,
) -> None:
    fake_db = patch_database(
        [
            job_row("text-job", task_type="text", cron_expr="*/5 * * * *"),
            job_row("agent-job", task_type="agent", cron_expr="10 * * * *"),
            job_row("other-source", source_id="source-b"),
            job_row("disabled", enabled=False),
            job_row("paused", status="paused"),
            job_row("deleted", deleted_at=datetime(2026, 7, 1, tzinfo=UTC)),
        ],
    )
    service = QueryService()

    result = await service.get_schedule_distribution(
        source_id="source-a",
        start_time=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 27, 10, 15, tzinfo=UTC),
        bucket_minutes=5,
    )

    assert [(item.text_count, item.agent_count) for item in result.buckets] == [
        (1, 0),
        (1, 0),
        (1, 1),
    ]
    assert result.total_count == 4
    assert result.text_count == 3
    assert result.agent_count == 1
    assert result.eligible_job_count == 2
    assert result.buckets[0].start_time == datetime(
        2026,
        7,
        27,
        10,
        0,
        tzinfo=UTC,
    )
    assert result.buckets[-1].end_time == datetime(
        2026,
        7,
        27,
        10,
        15,
        tzinfo=UTC,
    )
    sql, params = fake_db.calls[0]
    assert "source_id = %s" in sql
    assert "enabled = 1" in sql
    assert "status = 'active'" in sql
    assert "deleted_at IS NULL" in sql
    assert params == ("source-a",)


@pytest.mark.asyncio
async def test_workflow_has_its_own_planned_firing_count(patch_database):
    patch_database(
        [job_row("workflow-job", task_type="workflow", cron_expr="0 10 * * *")],
    )

    result = await QueryService().get_schedule_distribution(
        source_id="source-a",
        start_time=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 27, 10, 5, tzinfo=UTC),
        bucket_minutes=5,
    )

    assert result.workflow_count == 1
    assert result.total_count == 1
    assert result.buckets[0].workflow_count == 1
    assert result.diagnostics.unsupported_task_type_jobs == 0


@pytest.mark.asyncio
async def test_distribution_does_not_materialize_detail_occurrences(
    patch_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_database(
        [
            job_row("text-job", task_type="text", cron_expr="*/5 * * * *"),
            job_row("agent-job", task_type="agent", cron_expr="10 * * * *"),
        ],
    )

    def fail_if_materialized(*args, **kwargs):
        raise AssertionError("aggregate must not materialize detail occurrences")

    monkeypatch.setattr(
        QueryService,
        "_generate_schedule_occurrences",
        fail_if_materialized,
    )

    result = await QueryService().get_schedule_distribution(
        source_id="source-a",
        start_time=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 27, 10, 15, tzinfo=UTC),
        bucket_minutes=5,
    )

    assert result.total_count == 4
    assert result.text_count == 3
    assert result.agent_count == 1


@pytest.mark.asyncio
async def test_distribution_discards_partial_counts_from_invalid_definition(
    patch_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_database(
        [
            job_row("broken-job", task_type="text"),
            job_row("valid-job", task_type="agent"),
        ],
    )
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    def iter_schedule_times(definition, **kwargs):
        yield start
        if definition.job_id == "broken-job":
            raise ValueError("invalid during iteration")

    monkeypatch.setattr(
        QueryService,
        "_iter_schedule_times",
        staticmethod(iter_schedule_times),
    )

    result = await QueryService().get_schedule_distribution(
        source_id="source-a",
        start_time=start,
        end_time=start + timedelta(minutes=5),
        bucket_minutes=5,
    )

    assert result.total_count == 1
    assert result.text_count == 0
    assert result.agent_count == 1
    assert result.eligible_job_count == 1
    assert result.diagnostics.invalid_cron_jobs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_minutes", [5, 10, 15, 30, 60])
async def test_distribution_emits_supported_buckets_and_partial_final_bucket(
    patch_database,
    bucket_minutes: int,
) -> None:
    patch_database([])
    service = QueryService()
    start = datetime(2026, 7, 27, 10, 2, tzinfo=UTC)
    end = start + timedelta(minutes=bucket_minutes + 2)

    result = await service.get_schedule_distribution(
        source_id="source-a",
        start_time=start,
        end_time=end,
        bucket_minutes=bucket_minutes,
    )

    assert len(result.buckets) == 2
    assert result.buckets[0].start_time == start
    assert result.buckets[0].end_time == start + timedelta(
        minutes=bucket_minutes,
    )
    assert result.buckets[1].end_time == end


@pytest.mark.asyncio
async def test_distribution_uses_job_timezone_and_falls_back_for_invalid_zone(
    patch_database,
) -> None:
    patch_database(
        [
            job_row(
                "shanghai",
                cron_expr="0 9 * * *",
                timezone_name="Asia/Shanghai",
            ),
            job_row(
                "invalid-zone",
                cron_expr="0 1 * * *",
                timezone_name="Mars/Olympus",
                task_type="agent",
            ),
        ],
    )
    service = QueryService()

    result = await service.get_schedule_distribution(
        source_id="source-a",
        start_time=datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 27, 2, 0, tzinfo=UTC),
        bucket_minutes=60,
    )

    assert [(bucket.text_count, bucket.agent_count) for bucket in result.buckets] == [
        (0, 0),
        (1, 1),
    ]
    assert result.diagnostics.invalid_timezone_jobs == 1


@pytest.mark.asyncio
async def test_distribution_applies_dst_observing_timezone_offset(
    patch_database,
) -> None:
    patch_database(
        [
            job_row(
                "new-york",
                cron_expr="0 9 * * *",
                timezone_name="America/New_York",
            ),
        ],
    )

    result = await QueryService().get_schedule_distribution(
        source_id="source-a",
        start_time=datetime(2026, 7, 27, 13, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 27, 14, 0, tzinfo=UTC),
        bucket_minutes=60,
    )

    # New York observes UTC-4 in July, so 09:00 local is 13:00 UTC.
    assert result.total_count == 1
    assert result.buckets[0].start_time == datetime(
        2026,
        7,
        27,
        13,
        0,
        tzinfo=UTC,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cron_expr", "start", "end", "expected_times"),
    [
        (
            "30 2 * * *",
            datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
            datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
            [datetime(2026, 3, 8, 7, 0, tzinfo=UTC)],
        ),
        (
            "30 1 * * *",
            datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
            [
                datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
            ],
        ),
    ],
)
async def test_distribution_matches_croniter_across_dst_transitions(
    patch_database,
    cron_expr: str,
    start: datetime,
    end: datetime,
    expected_times: list[datetime],
) -> None:
    patch_database(
        [
            job_row(
                "new-york",
                cron_expr=cron_expr,
                timezone_name="America/New_York",
            ),
        ],
    )

    detail = await QueryService().get_schedule_distribution_details(
        source_id="source-a",
        start_time=start,
        end_time=end,
        page=1,
        page_size=10,
    )

    assert [item.scheduled_at for item in detail.items] == expected_times


@pytest.mark.asyncio
async def test_distribution_excludes_bad_definitions_but_keeps_valid_jobs(
    patch_database,
) -> None:
    patch_database(
        [
            job_row("valid"),
            job_row("invalid-cron", cron_expr="not cron"),
            job_row("invalid-type", task_type="cleanup"),
            job_row("invalid-meta", meta="{"),
        ],
    )
    service = QueryService()

    result = await service.get_schedule_distribution(
        source_id="source-a",
        start_time=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 27, 10, 5, tzinfo=UTC),
        bucket_minutes=5,
    )

    assert result.total_count == 2
    assert result.eligible_job_count == 2
    assert result.diagnostics.invalid_cron_jobs == 1
    assert result.diagnostics.unsupported_task_type_jobs == 1
    assert result.diagnostics.invalid_metadata_jobs == 1


@pytest.mark.asyncio
async def test_managed_broadcast_child_exclusion_respects_runtime_gate(
    patch_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_meta = json.dumps(
        {
            "broadcast_source_job_id": "parent",
            "broadcast_dispatch_intents_enabled": True,
        },
    )
    patch_database(
        [
            job_row(
                "parent",
                meta=json.dumps({"broadcast_dispatch_intents_enabled": True}),
            ),
            job_row("managed-child", task_type="agent", meta=managed_meta),
            job_row(
                "legacy-child",
                task_type="agent",
                meta=json.dumps({"broadcast_source_job_id": "parent"}),
            ),
        ],
    )
    service = QueryService()
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5)

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "true")
    enabled = await service.get_schedule_distribution(
        source_id="source-a",
        start_time=start,
        end_time=end,
        bucket_minutes=5,
    )
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "false")
    disabled = await service.get_schedule_distribution(
        source_id="source-a",
        start_time=start,
        end_time=end,
        bucket_minutes=5,
    )

    assert enabled.total_count == 2
    assert enabled.diagnostics.managed_child_jobs == 1
    assert disabled.total_count == 3
    assert disabled.diagnostics.managed_child_jobs == 0


@pytest.mark.asyncio
async def test_detail_reconciles_with_bucket_and_returns_only_whitelisted_fields(
    patch_database,
) -> None:
    fake_db = patch_database(
        [
            job_row(
                "job-b",
                tenant_name="Bob",
                tenant_id="account-b",
                task_type="agent",
                cron_expr="0,5 * * * *",
            ),
            job_row(
                "job-a",
                tenant_name="Alice",
                tenant_id="account-a",
                task_type="text",
                cron_expr="0,5 * * * *",
            ),
            job_row("disabled", enabled=False),
            job_row("paused", status="paused"),
            job_row("deleted", deleted_at=datetime(2026, 7, 1, tzinfo=UTC)),
        ],
    )
    service = QueryService()
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    end = datetime(2026, 7, 27, 10, 10, tzinfo=UTC)
    aggregate = await service.get_schedule_distribution(
        source_id="source-a",
        start_time=start,
        end_time=end,
        bucket_minutes=10,
    )

    first_page = await service.get_schedule_distribution_details(
        source_id="source-a",
        start_time=start,
        end_time=end,
        task_type=None,
        page=1,
        page_size=3,
    )
    second_page = await service.get_schedule_distribution_details(
        source_id="source-a",
        start_time=start,
        end_time=end,
        task_type=None,
        page=2,
        page_size=3,
        expected_revision=first_page.definition_revision,
    )

    assert first_page.total == aggregate.total_count == 4
    assert first_page.definition_revision == aggregate.definition_revision
    all_items = [*first_page.items, *second_page.items]
    assert [(item.scheduled_at, item.job_id) for item in all_items] == sorted(
        (item.scheduled_at, item.job_id) for item in all_items
    )
    assert set(all_items[0].model_dump()) == {
        "scheduled_at",
        "job_id",
        "job_name",
        "user_name",
        "user_id",
        "task_type",
        "cron_expr",
        "timezone",
    }
    assert {
        (item.job_id, item.user_name, item.user_id) for item in all_items
    } == {
        ("job-a", "Alice", "account-a"),
        ("job-b", "Bob", "account-b"),
    }
    assert first_page.calculated_at.tzinfo is not None
    sql, _ = fake_db.calls[0]
    assert "tenant_name" in sql
    assert "tenant_id" in sql

    agent_page = await service.get_schedule_distribution_details(
        source_id="source-a",
        start_time=start,
        end_time=end,
        task_type=TaskType.AGENT,
        page=1,
        page_size=10,
    )
    assert agent_page.total == 2
    assert {item.task_type for item in agent_page.items} == {"agent"}


@pytest.mark.asyncio
async def test_detail_rejects_changed_definition_revision(
    patch_database,
) -> None:
    fake_db = patch_database([job_row("job-a")])
    service = QueryService()
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    first = await service.get_schedule_distribution_details(
        source_id="source-a",
        start_time=start,
        end_time=end,
        page=1,
        page_size=10,
    )
    fake_db.rows[0]["cron_expr"] = "*/10 * * * *"

    with pytest.raises(ScheduleDefinitionRevisionConflictError):
        await service.get_schedule_distribution_details(
            source_id="source-a",
            start_time=start,
            end_time=end,
            page=2,
            page_size=10,
            expected_revision=first.definition_revision,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "updated_value"),
    [
        ("tenant_name", "Alice Updated"),
        ("tenant_id", "account-a-updated"),
    ],
)
async def test_detail_rejects_changed_account_identity_revision(
    patch_database,
    field_name: str,
    updated_value: str,
) -> None:
    fake_db = patch_database(
        [
            job_row(
                "job-a",
                tenant_name="Alice",
                tenant_id="account-a",
            ),
        ],
    )
    service = QueryService()
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    first = await service.get_schedule_distribution_details(
        source_id="source-a",
        start_time=start,
        end_time=end,
        page=1,
        page_size=10,
    )
    fake_db.rows[0][field_name] = updated_value

    with pytest.raises(ScheduleDefinitionRevisionConflictError):
        await service.get_schedule_distribution_details(
            source_id="source-a",
            start_time=start,
            end_time=end,
            page=2,
            page_size=10,
            expected_revision=first.definition_revision,
        )


@pytest.mark.asyncio
async def test_detail_requires_revision_after_first_page(patch_database) -> None:
    patch_database([job_row("job-a")])

    with pytest.raises(ScheduleDistributionValidationError):
        await QueryService().get_schedule_distribution_details(
            source_id="source-a",
            start_time=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            end_time=datetime(2026, 7, 27, 10, 5, tzinfo=UTC),
            page=2,
            page_size=10,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end", "bucket"),
    [
        (
            datetime(2026, 7, 27, 10, 0),
            datetime(2026, 7, 27, 11, 0, tzinfo=UTC),
            5,
        ),
        (
            datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            5,
        ),
        (
            datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 3, 10, 1, tzinfo=UTC),
            5,
        ),
        (
            datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 11, 0, tzinfo=UTC),
            7,
        ),
    ],
)
async def test_distribution_rejects_invalid_range_or_bucket(
    patch_database,
    start: datetime,
    end: datetime,
    bucket: int,
) -> None:
    patch_database([])
    with pytest.raises(ScheduleDistributionValidationError):
        await QueryService().get_schedule_distribution(
            source_id="source-a",
            start_time=start,
            end_time=end,
            bucket_minutes=bucket,
        )


@pytest.mark.asyncio
async def test_distribution_stops_at_occurrence_budget(
    patch_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_database([job_row("hot", cron_expr="* * * * *")])
    monkeypatch.setattr(
        "monitor.app.services.cron.query_service.SCHEDULE_OCCURRENCE_LIMIT",
        2,
    )

    with pytest.raises(ScheduleCalculationLimitExceededError):
        await QueryService().get_schedule_distribution(
            source_id="source-a",
            start_time=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            end_time=datetime(2026, 7, 27, 10, 5, tzinfo=UTC),
            bucket_minutes=5,
        )


@pytest.mark.asyncio
async def test_distribution_allows_exact_occurrence_budget(
    patch_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_database([job_row("hot", cron_expr="* * * * *")])
    monkeypatch.setattr(
        "monitor.app.services.cron.query_service.SCHEDULE_OCCURRENCE_LIMIT",
        2,
    )

    result = await QueryService().get_schedule_distribution(
        source_id="source-a",
        start_time=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 7, 27, 10, 2, tzinfo=UTC),
        bucket_minutes=5,
    )

    assert result.total_count == 2


class ApiServiceStub:
    """Capture validated endpoint arguments while using real response DTOs."""

    def __init__(self) -> None:
        self.service = QueryService()
        self.calls: list[dict[str, Any]] = []

    async def get_schedule_distribution(self, **kwargs):
        self.calls.append(kwargs)
        return await self.service.get_schedule_distribution(**kwargs)

    async def get_schedule_distribution_details(self, **kwargs):
        self.calls.append(kwargs)
        return await self.service.get_schedule_distribution_details(**kwargs)


@pytest.fixture
def api_client(patch_database):
    patch_database(
        [
            job_row(
                "trusted-job",
                source_id="trusted",
                tenant_name="Trusted User",
                tenant_id="trusted-account",
            ),
            job_row(
                "default-job",
                source_id="default",
                tenant_name="Default User",
                tenant_id="default-account",
            ),
        ],
    )
    from monitor.app.routers.cron import router
    from monitor.app.services.cron import get_query_service

    app = FastAPI()
    stub = ApiServiceStub()
    app.dependency_overrides[get_query_service] = lambda: stub
    app.include_router(router)
    try:
        yield TestClient(app), stub
    finally:
        app.dependency_overrides.clear()


def test_schedule_distribution_api_uses_trusted_source_header(api_client) -> None:
    client, stub = api_client
    response = client.get(
        "/monitor/cron/schedule-distribution",
        params={
            "start_time": "2026-07-27T10:00:00+00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "bucket_minutes": 5,
            "source_id": "attacker",
        },
        headers={"X-Source-Id": "trusted"},
    )

    assert response.status_code == 200
    assert stub.calls[0]["source_id"] == "trusted"
    payload = response.json()
    assert payload["total_count"] == 1
    assert payload["calculated_at"].endswith("Z")


def test_schedule_distribution_detail_api_supports_filter_and_pagination(
    api_client,
) -> None:
    client, stub = api_client
    response = client.get(
        "/monitor/cron/schedule-distribution/details",
        params={
            "start_time": "2026-07-27T10:00:00+00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "task_type": "text",
            "page": 1,
            "page_size": 20,
            "source_id": "attacker",
        },
        headers={"X-Source-Id": "trusted"},
    )

    assert response.status_code == 200
    assert stub.calls[0]["source_id"] == "trusted"
    assert stub.calls[0]["task_type"] == TaskType.TEXT
    payload = response.json()
    assert payload["definition_revision"]
    assert [item["job_id"] for item in payload["items"]] == ["trusted-job"]
    assert [
        (item["user_name"], item["user_id"]) for item in payload["items"]
    ] == [("Trusted User", "trusted-account")]
    serialized_payload = json.dumps(payload, ensure_ascii=False)
    assert "Default User" not in serialized_payload
    assert "default-account" not in serialized_payload


@pytest.mark.parametrize(
    "params",
    [
        {
            "start_time": "2026-07-27T10:00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "bucket_minutes": 5,
        },
        {
            "start_time": "2026-07-27T10:05:00+00:00",
            "end_time": "2026-07-27T10:00:00+00:00",
            "bucket_minutes": 5,
        },
        {
            "start_time": "2026-07-27T10:00:00+00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "bucket_minutes": 7,
        },
    ],
)
def test_schedule_distribution_api_rejects_invalid_queries(
    api_client,
    params: dict[str, Any],
) -> None:
    client, _ = api_client
    response = client.get(
        "/monitor/cron/schedule-distribution",
        params=params,
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "schedule_distribution_validation_error"
    assert isinstance(detail["message"], str)
    assert detail["message"]


@pytest.mark.parametrize(
    "params",
    [
        {
            "start_time": "2026-07-27T10:00:00+00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "task_type": "cleanup",
        },
        {
            "start_time": "2026-07-27T10:00:00+00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "page": 0,
        },
    ],
)
def test_schedule_distribution_detail_api_wraps_request_validation_errors(
    api_client,
    params: dict[str, Any],
) -> None:
    client, _ = api_client
    response = client.get(
        "/monitor/cron/schedule-distribution/details",
        params=params,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "schedule_distribution_validation_error",
        "message": "Invalid schedule distribution request.",
    }


def test_schedule_distribution_api_returns_typed_limit_error(
    api_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = api_client
    monkeypatch.setattr(
        "monitor.app.services.cron.query_service.SCHEDULE_OCCURRENCE_LIMIT",
        0,
    )
    response = client.get(
        "/monitor/cron/schedule-distribution",
        params={
            "start_time": "2026-07-27T10:00:00+00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "bucket_minutes": 5,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == (
        "schedule_calculation_limit_exceeded"
    )


def test_schedule_distribution_detail_api_returns_revision_conflict(
    api_client,
) -> None:
    client, _ = api_client
    response = client.get(
        "/monitor/cron/schedule-distribution/details",
        params={
            "start_time": "2026-07-27T10:00:00+00:00",
            "end_time": "2026-07-27T10:05:00+00:00",
            "page": 2,
            "page_size": 20,
            "expected_revision": "stale",
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == (
        "schedule_definition_revision_conflict"
    )
    assert detail["message"]
    assert detail["actual_revision"]


def test_schedule_distribution_openapi_declares_bucket_and_error_contracts(
    api_client,
) -> None:
    client, _ = api_client
    schema = client.app.openapi()
    aggregate = schema["paths"]["/monitor/cron/schedule-distribution"]["get"]
    detail = schema["paths"][
        "/monitor/cron/schedule-distribution/details"
    ]["get"]
    bucket_parameter = next(
        item
        for item in aggregate["parameters"]
        if item["name"] == "bucket_minutes"
    )

    assert bucket_parameter["schema"]["enum"] == [5, 10, 15, 30, 60]
    assert aggregate["responses"]["422"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/CronScheduleDistributionErrorResponse")
    assert detail["responses"]["422"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/CronScheduleDistributionErrorResponse")
    assert detail["responses"]["409"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/CronScheduleDistributionErrorResponse")


def test_existing_cron_routes_remain_registered() -> None:
    from monitor.app.routers.cron import router

    registered_paths = {
        route.path for route in router.routes if hasattr(route, "path")
    }

    assert {
        "/monitor/cron/overview",
        "/monitor/cron/jobs",
        "/monitor/cron/executions",
    }.issubset(registered_paths)


def test_existing_cron_route_keeps_default_validation_envelope(api_client) -> None:
    client, _ = api_client

    response = client.get("/monitor/cron/jobs", params={"page": 0})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
