# -*- coding: utf-8 -*-
from datetime import datetime

import pytest

from monitor.app.models.cron import (
    CronDispatchDetailQueryParams,
    CronDispatchPolicyItem,
)
from monitor.app.services.cron import query_service as query_service_module
from monitor.app.services.cron.query_service import QueryService
from monitor.app.routers.cron import (
    get_dispatch_batch_detail,
    list_dispatch_batches,
)


class FakeDb:
    def __init__(self, *, one_results=None, all_results=None):
        self.one_results = list(one_results or [])
        self.all_results = list(all_results or [])
        self.fetch_one_calls = []
        self.fetch_all_calls = []

    async def fetch_one(self, sql, params=None):
        self.fetch_one_calls.append((sql, params))
        return self.one_results.pop(0) if self.one_results else None

    async def fetch_all(self, sql, params=None):
        self.fetch_all_calls.append((sql, params))
        return self.all_results.pop(0) if self.all_results else []


@pytest.mark.asyncio
async def test_get_dispatch_batches_filters_by_current_source(monkeypatch):
    fake_db = FakeDb(
        one_results=[
            {
                "total_batches": 2,
                "running_batches": 1,
                "completed_batches": 1,
                "failed_batches": 0,
                "total_intents": 12,
                "completed_intents": 8,
                "failed_intents": 1,
            },
        ],
        all_results=[
            [
                {
                    "batch_id": "cron:batch-a",
                    "parent_job_id": "parent-a",
                    "parent_job_name": "展示定时任务名",
                    "parent_external_job_id": "external-a",
                    "tenant_id": "tenant-a",
                    "source_id": "RMASSIST",
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "agent_id": "agent-a",
                    "scheduled_fire_at": datetime(2026, 7, 8, 9, 0, 0),
                    "callback_received_at": datetime(2026, 7, 8, 5, 0, 0),
                    "status": "running",
                    "lock_owner": "worker-a",
                    "locked_at": datetime(2026, 7, 8, 5, 0, 20),
                    "total_count": 12,
                    "completed_count": 8,
                    "failed_count": 1,
                    "error_message": "",
                    "completed_at": None,
                    "created_at": datetime(2026, 7, 8, 5, 0, 0),
                    "updated_at": datetime(2026, 7, 8, 5, 1, 0),
                },
            ],
        ],
    )
    monkeypatch.setattr(
        query_service_module,
        "get_db_connection",
        lambda: fake_db,
    )

    result = await QueryService().get_dispatch_batches(
        source_id="RMASSIST",
        start_time=datetime(2026, 7, 8, 0, 0, 0),
        end_time=datetime(2026, 7, 8, 23, 59, 59),
        status="running",
        page=1,
        page_size=20,
    )

    assert result.source_id == "RMASSIST"
    assert result.stats.pending_intents == 3
    assert result.items[0].batch_id == "cron:batch-a"
    assert result.items[0].parent_job_name == "展示定时任务名"
    normalized_stats_sql = " ".join(fake_db.fetch_one_calls[0][0].split())
    normalized_page_sql = " ".join(fake_db.fetch_all_calls[0][0].split())
    source_scoped_job_join = (
        "LEFT JOIN swe_cron_jobs j ON b.parent_job_id = j.id "
        "AND b.source_id = j.source_id"
    )
    assert source_scoped_job_join in normalized_stats_sql
    assert source_scoped_job_join in normalized_page_sql
    assert "j.deleted_at" not in normalized_stats_sql
    assert "j.deleted_at" not in normalized_page_sql
    assert "COALESCE(j.name, '') AS parent_job_name" in normalized_page_sql
    assert fake_db.fetch_one_calls[0][1] == (
        "RMASSIST",
        datetime(2026, 7, 8, 0, 0, 0),
        datetime(2026, 7, 8, 23, 59, 59),
        "running",
    )


@pytest.mark.asyncio
async def test_get_dispatch_batches_applies_global_query_to_stats_and_page(monkeypatch):
    fake_db = FakeDb(
        one_results=[
            {
                "total_batches": 0,
                "running_batches": 0,
                "completed_batches": 0,
                "failed_batches": 0,
                "total_intents": 0,
                "completed_intents": 0,
                "failed_intents": 0,
            },
        ],
        all_results=[[]],
    )
    monkeypatch.setattr(query_service_module, "get_db_connection", lambda: fake_db)

    await QueryService().get_dispatch_batches(
        source_id="RMASSIST",
        status="failed",
        query="  Agent\\_%  ",
        page=2,
        page_size=4,
    )

    stats_sql, stats_params = fake_db.fetch_one_calls[0]
    page_sql, page_params = fake_db.fetch_all_calls[0]
    normalized_stats_sql = " ".join(stats_sql.split())
    normalized_page_sql = " ".join(page_sql.split())
    source_scoped_job_join = (
        "LEFT JOIN swe_cron_jobs j ON b.parent_job_id = j.id "
        "AND b.source_id = j.source_id"
    )
    searchable_fields = (
        "b.batch_id",
        "b.parent_job_id",
        "b.parent_external_job_id",
        "b.tenant_id",
        "b.provider_id",
        "b.model_id",
        "b.agent_id",
        "j.name",
    )

    assert stats_params == (
        "RMASSIST",
        "failed",
        "%agent\\\\\\_\\%%",
        "%agent\\\\\\_\\%%",
        "%agent\\\\\\_\\%%",
        "%agent\\\\\\_\\%%",
        "%agent\\\\\\_\\%%",
        "%agent\\\\\\_\\%%",
        "%agent\\\\\\_\\%%",
        "%agent\\\\\\_\\%%",
    )
    assert page_params == stats_params + (4, 4)
    assert source_scoped_job_join in normalized_stats_sql
    assert source_scoped_job_join in normalized_page_sql
    assert "j.deleted_at" not in normalized_stats_sql
    assert "j.deleted_at" not in normalized_page_sql
    for field in searchable_fields:
        predicate = f"LOWER(COALESCE({field}, '')) LIKE %s ESCAPE '\\\\'"
        assert predicate in normalized_stats_sql
        assert predicate in normalized_page_sql


def test_build_dispatch_batch_where_clause_ignores_blank_query():
    where_clause, params = QueryService()._build_dispatch_batch_where_clause(
        source_id="RMASSIST",
        query="   ",
    )

    assert where_clause == "b.source_id = %s"
    assert params == ["RMASSIST"]


@pytest.mark.asyncio
async def test_list_dispatch_batches_forwards_query():
    class FakeRequest:
        headers = {"X-Source-Id": "RMASSIST"}

    class FakeService:
        def __init__(self):
            self.kwargs = None

        async def get_dispatch_batches(self, **kwargs):
            self.kwargs = kwargs
            return object()

    service = FakeService()
    await list_dispatch_batches(
        request=FakeRequest(),
        start_time=None,
        end_time=None,
        status=None,
        query="tenant-a",
        page=1,
        page_size=4,
        service=service,
    )

    assert service.kwargs["query"] == "tenant-a"


@pytest.mark.asyncio
async def test_get_dispatch_batch_detail_parses_events(monkeypatch):
    fake_db = FakeDb(
        one_results=[
            {
                "batch_id": "cron:batch-a",
                "parent_job_id": "parent-a",
                "parent_job_name": "展示定时任务名",
                "parent_external_job_id": "",
                "tenant_id": "tenant-a",
                "source_id": "RMASSIST",
                "provider_id": "provider-a",
                "model_id": "model-a",
                "agent_id": "agent-a",
                "scheduled_fire_at": datetime(2026, 7, 8, 9, 0, 0),
                "callback_received_at": datetime(2026, 7, 8, 5, 0, 0),
                "status": "running",
                "lock_owner": "worker-a",
                "locked_at": None,
                "total_count": 1,
                "completed_count": 0,
                "failed_count": 0,
                "error_message": "",
                "completed_at": None,
                "created_at": datetime(2026, 7, 8, 5, 0, 0),
                "updated_at": datetime(2026, 7, 8, 5, 1, 0),
            },
            {"count": 1},
        ],
        all_results=[
            [
                {
                    "id": 1001,
                    "batch_id": "cron:batch-a",
                    "intent_role": "child",
                    "status": "pending",
                    "source_id": "RMASSIST",
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "tenant_id": "tenant-a",
                    "agent_id": "agent-a",
                    "job_id": "job-a",
                    "parent_job_id": "parent-a",
                    "scheduled_fire_at": datetime(2026, 7, 8, 9, 0, 0),
                    "due_at": datetime(2026, 7, 8, 5, 5, 0),
                    "dispatch_order": 1,
                    "viewer_heat_score": "1.25",
                    "attempt_count": 0,
                    "max_attempts": 3,
                    "lock_owner": "",
                    "locked_at": None,
                    "acked_at": None,
                    "completed_at": None,
                    "error_message": "",
                    "created_at": datetime(2026, 7, 8, 5, 0, 0),
                    "updated_at": datetime(2026, 7, 8, 5, 0, 0),
                },
            ],
            [
                {
                    "id": 1,
                    "batch_id": "cron:batch-a",
                    "intent_id": 1001,
                    "event_type": "retry_scheduled",
                    "worker_id": "worker-a",
                    "job_id": "job-a",
                    "tenant_id": "tenant-a",
                    "source_id": "RMASSIST",
                    "details": '{"error": "timeout"}',
                    "created_at": datetime(2026, 7, 8, 5, 6, 0),
                },
            ],
        ],
    )
    monkeypatch.setattr(
        query_service_module,
        "get_db_connection",
        lambda: fake_db,
    )

    result = await QueryService().get_dispatch_batch_detail(
        source_id="RMASSIST",
        batch_id="cron:batch-a",
    )

    assert result is not None
    assert result.batch.parent_job_name == "展示定时任务名"
    assert result.intent_total == 1
    assert result.intents[0].viewer_heat_score == 1.25
    assert result.events[0].details == {"error": "timeout"}
    normalized_batch_sql = " ".join(fake_db.fetch_one_calls[0][0].split())
    assert (
        "LEFT JOIN swe_cron_jobs j ON b.parent_job_id = j.id "
        "AND b.source_id = j.source_id"
        in normalized_batch_sql
    )
    assert "j.deleted_at" not in normalized_batch_sql
    assert "COALESCE(j.name, '') AS parent_job_name" in normalized_batch_sql


@pytest.mark.asyncio
async def test_get_dispatch_batch_detail_paginates_and_filters_all_rows(
    monkeypatch,
):
    fake_db = FakeDb(
        one_results=[
            {"batch_id": "cron:batch-a"},
            {"count": 650},
            {"count": 120},
            {"count": 725},
        ],
        all_results=[
            [
                {
                    "id": 151,
                    "batch_id": "cron:batch-a",
                    "intent_role": "child",
                    "status": "pending",
                    "job_id": "Job_%",
                },
            ],
            [
                {
                    "id": 51,
                    "batch_id": "cron:batch-a",
                    "event_type": "retry_scheduled",
                },
            ],
        ],
    )
    monkeypatch.setattr(
        query_service_module,
        "get_db_connection",
        lambda: fake_db,
    )

    result = await QueryService().get_dispatch_batch_detail(
        source_id="RMASSIST",
        batch_id="cron:batch-a",
        params=CronDispatchDetailQueryParams(
            intent_page=2,
            intent_limit=50,
            intent_query=" Job_% ",
            intent_role="child",
            intent_status="pending",
            event_page=3,
            event_limit=25,
        ),
    )

    assert result is not None
    assert result.intent_total == 650
    assert result.intent_filtered_total == 120
    assert result.intent_page == 2
    assert result.intent_page_size == 50
    assert result.event_total == 725
    assert result.event_page == 3
    assert result.event_page_size == 25

    filtered_count_sql, filtered_count_params = fake_db.fetch_one_calls[2]
    intent_page_sql, intent_page_params = fake_db.fetch_all_calls[0]
    event_page_sql, event_page_params = fake_db.fetch_all_calls[1]
    normalized_filtered_sql = " ".join(filtered_count_sql.split())
    normalized_intent_sql = " ".join(intent_page_sql.split())

    for sql in (normalized_filtered_sql, normalized_intent_sql):
        assert "intent_role = %s" in sql
        assert "status = %s" in sql
        assert "LOWER(COALESCE(job_id, '')) LIKE %s ESCAPE '\\\\'" in sql
    assert filtered_count_params[:4] == (
        "RMASSIST",
        "cron:batch-a",
        "child",
        "pending",
    )
    assert set(filtered_count_params[4:]) == {"%job\\_\\%%"}
    assert intent_page_params[-2:] == (50, 50)
    assert event_page_params == ("RMASSIST", "cron:batch-a", 25, 50)


@pytest.mark.asyncio
async def test_dispatch_batch_detail_route_forwards_pagination_and_filters():
    class FakeRequest:
        headers = {"X-Source-Id": "RMASSIST"}

    class FakeService:
        def __init__(self):
            self.kwargs = None

        async def get_dispatch_batch_detail(self, **kwargs):
            self.kwargs = kwargs
            return object()

    service = FakeService()
    await get_dispatch_batch_detail(
        request=FakeRequest(),
        batch_id="cron:batch-a",
        intent_limit=50,
        event_limit=25,
        intent_page=2,
        event_page=3,
        intent_query="job-a",
        intent_role="child",
        intent_status="pending",
        service=service,
    )

    assert service.kwargs["source_id"] == "RMASSIST"
    assert service.kwargs["batch_id"] == "cron:batch-a"
    assert service.kwargs["params"] == CronDispatchDetailQueryParams(
        intent_page=2,
        intent_limit=50,
        intent_query="job-a",
        intent_role="child",
        intent_status="pending",
        event_page=3,
        event_limit=25,
    )


@pytest.mark.asyncio
async def test_get_dispatch_workers_parses_policy_and_capacity(monkeypatch):
    fake_db = FakeDb(
        all_results=[
            [
                {
                    "source_id": "RMASSIST",
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "default_strategy_id": "strategy-a",
                    "strategy_schedule": (
                        '[{"start_time":"16:00","end_time":"21:00",'
                        '"strategy_id":"peak_1"}]'
                    ),
                    "enabled": 1,
                    "created_at": datetime(2026, 7, 8, 4, 0, 0),
                    "updated_at": datetime(2026, 7, 8, 4, 0, 0),
                    "strategy_id": "strategy-a",
                    "min_workers": 5,
                    "baseline_workers": 5,
                    "max_workers": 999,
                    "adjust_interval_seconds": 20,
                    "feedback_window_seconds": 20,
                    "stale_execution_seconds": 7800,
                    "error_rate_rules": '{"success_100": "double"}',
                    "strategy_enabled": 1,
                    "description": "test strategy",
                },
            ],
            [
                {
                    "id": 10,
                    "worker_id": "worker-a",
                    "source_id": "RMASSIST",
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "strategy_id": "strategy-a",
                    "previous_workers": 5,
                    "baseline_workers": 5,
                    "min_workers": 5,
                    "max_workers": 999,
                    "effective_workers": 10,
                    "pending_count": 3,
                    "claimed_count": 2,
                    "running_count": 1,
                    "success_count": 8,
                    "failure_count": 0,
                    "error_rate": "0.0",
                    "matched_rule": '{"reason": "success_100_double"}',
                    "avg_latency_ms": 1200,
                    "decision_reason": "success_100_double",
                    "created_at": datetime(2026, 7, 8, 5, 0, 0),
                },
            ],
            [],
        ],
    )
    monkeypatch.setattr(
        query_service_module,
        "get_db_connection",
        lambda: fake_db,
    )

    result = await QueryService().get_dispatch_workers(source_id="RMASSIST")

    assert result.policies[0].strategy_schedule == [
        {
            "start_time": "16:00",
            "end_time": "21:00",
            "strategy_id": "peak_1",
        },
    ]
    assert result.policies[0].strategy["error_rate_rules"] == {
        "success_100": "double",
    }
    assert result.current_capacity[0].matched_rule == {
        "reason": "success_100_double",
    }


def test_map_dispatch_policy_ignores_non_list_strategy_schedule():
    policy = QueryService()._map_dispatch_policy(
        {
            "source_id": "RMASSIST",
            "provider_id": "provider-a",
            "model_id": "model-a",
            "default_strategy_id": "strategy-a",
            "strategy_schedule": (
                '{"start_time":"16:00","end_time":"21:00",'
                '"strategy_id":"peak_1"}'
            ),
            "enabled": 1,
        },
    )

    assert policy.strategy_schedule is None


def test_map_dispatch_policy_filters_invalid_schedule_items():
    policy = QueryService()._map_dispatch_policy(
        {
            "source_id": "RMASSIST",
            "provider_id": "provider-a",
            "model_id": "model-a",
            "default_strategy_id": "strategy-a",
            "strategy_schedule": '[{"strategy_id":"peak_1"}, "invalid"]',
            "enabled": 1,
        },
    )

    assert policy.strategy_schedule == [{"strategy_id": "peak_1"}]


def test_dispatch_policy_item_accepts_strategy_schedule_without_validation():
    strategy_schedule = {"strategy_id": "peak_1"}
    policy = CronDispatchPolicyItem(
        source_id="RMASSIST",
        provider_id="provider-a",
        model_id="model-a",
        default_strategy_id="strategy-a",
        strategy_schedule=strategy_schedule,
    )

    assert policy.strategy_schedule == strategy_schedule


@pytest.mark.asyncio
async def test_get_dispatch_workers_queries_one_current_row_per_model_scope(
    monkeypatch,
):
    fake_db = FakeDb(all_results=[[], [], []])
    monkeypatch.setattr(
        query_service_module,
        "get_db_connection",
        lambda: fake_db,
    )

    await QueryService().get_dispatch_workers(source_id="RMASSIST")

    current_sql, current_params = fake_db.fetch_all_calls[1]
    normalized_sql = " ".join(current_sql.split())
    assert current_params == ("RMASSIST",)
    assert "GROUP BY source_id, provider_id, model_id" in normalized_sql
    assert "model_id, strategy_id" not in normalized_sql
    assert "LEFT JOIN swe_cron_dispatch_scope_leases" in normalized_sql
    assert "lease_expires_at >= NOW()" in normalized_sql


@pytest.mark.asyncio
async def test_get_dispatch_workers_filters_explicit_capacity_event_query(
    monkeypatch,
):
    fake_db = FakeDb(all_results=[[], [], []])
    monkeypatch.setattr(
        query_service_module,
        "get_db_connection",
        lambda: fake_db,
    )
    start_time = datetime(2026, 7, 14, 0, 0, 0)
    end_time = datetime(2026, 7, 14, 23, 59, 59)

    await QueryService().get_dispatch_workers(
        source_id="RMASSIST",
        start_time=start_time,
        end_time=end_time,
    )

    event_sql, event_params = fake_db.fetch_all_calls[2]
    normalized_sql = " ".join(event_sql.split())
    assert event_params == ("RMASSIST", start_time, end_time)
    assert "SELECT *" not in normalized_sql.upper()
    assert "created_at >= %s" in normalized_sql
    assert "created_at <= %s" in normalized_sql
