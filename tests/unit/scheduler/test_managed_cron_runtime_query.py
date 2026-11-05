# -*- coding: utf-8 -*-
"""Runtime batches execute real relational SQL without loading task definitions."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_queries import (
    HEADERS,
    PATH,
    QueryDatabase,
)


class RuntimeDatabase(QueryDatabase):
    def __init__(self):
        super().__init__()
        self.queries = []

    async def fetch_all(self, query, params=None):
        self.queries.append(query)
        return await super().fetch_all(query, params)


@pytest.fixture
def database():
    db = RuntimeDatabase()
    yield db
    db.conn.close()


@pytest.fixture
async def client(database):
    from scheduler.app.routers.managed_cron import (
        get_managed_repository,
        router,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_managed_repository] = (
        lambda: ManagedCronRepository(database)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        yield http


async def runtime(client, ids, headers=None):
    return await client.post(
        f"{PATH}/-/runtime-statuses",
        headers=headers or HEADERS,
        json={"job_ids": ids},
    )


@pytest.mark.asyncio
async def test_batch_returns_ordered_complete_scalar_projection(
    client,
    database,
):
    database.add(
        "ready",
        last_status="running",
        last_error="错" * 1025,
        latest_scheduled_preview="结" * 257,
        has_scheduled_result=1,
    )
    database.add("missing", with_state=False)
    database.conn.execute("UPDATE swe_cron_jobs_v2 SET spec_json='corrupt'")
    before = datetime.now(timezone.utc)
    response = await runtime(client, ["missing", "ready", "absent", "ready"])
    assert response.status_code == 200
    missing, ready, absent = response.json()["items"]
    assert missing == {
        "id": "missing",
        "status": "state_missing",
        "definition_version": 1,
    }
    assert absent == {"id": "absent", "status": "unavailable"}
    assert ready["id"] == "ready" and ready["status"] == "ok"
    assert (
        ready["definition_version"]
        == ready["management_version"]
        == ready["state_version"]
        == 1
    )
    assert (
        ready["management_status"] == "ready"
        and ready["dispatch_state"] == "ordinary"
    )
    assert len(ready["state"]["last_error"]) == 1024
    assert ready["state"]["last_error_truncated"] is True
    assert len(ready["task"]["latest_scheduled_preview"]) == 256
    assert ready["task"]["latest_scheduled_preview_truncated"] is True
    assert ready["task"]["is_running"] is True
    assert ready["task"]["unread_execution_count"] == 2
    assert ready["task"]["has_scheduled_result"] is True
    assert len(ready["state"]["next_run_times"]) == 3
    assert all(
        datetime.fromisoformat(t) > before
        for t in ready["state"]["next_run_times"]
    )
    assert ready["state"]["next_run_at"] == ready["state"]["next_run_times"][0]
    assert not {
        "request",
        "text",
        "meta",
        "spec",
        "model_slot",
        "dispatch",
    }.intersection(ready)
    assert len(database.queries) == 1
    sql = database.queries[0].lower()
    assert "left join" in sql
    assert all(
        term not in sql
        for term in ("spec_json", "d.*", "count(", "for update")
    )
    assert database.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, "manager", "admin"])
async def test_batch_uses_page_scope_and_exact_identity(
    client,
    database,
    role,
):
    database.add("owned")
    database.add("creator-case", actor="ALICE")
    database.add("source-case", source="BANK")
    database.add("agent-case", agent="DEFAULT")
    database.add("tenant-case", tenant="ALICE")
    database.add(
        "foreign",
        tenant="bob",
        parent=("alice", "bank", "default", "owned"),
    )
    database.add("deleted", deleted=True)
    database.add("legacy", owner="legacy")
    database.add("Case")
    ids = [
        "owned",
        "creator-case",
        "source-case",
        "agent-case",
        "tenant-case",
        "foreign",
        "deleted",
        "legacy",
        "case",
        "Case",
    ]
    headers = {**HEADERS, **({"X-User-Role": role} if role else {})}
    response = await runtime(client, ids, headers)
    assert response.status_code == 200
    assert [i["id"] for i in response.json()["items"]] == ids
    assert [
        i["id"] for i in response.json()["items"] if i["status"] == "ok"
    ] == (["owned", "creator-case", "Case"] if role else ["owned", "Case"])
    assert len(database.queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "component,value",
    [
        ("source_id", "BANK"),
        ("tenant_id", "ALICE"),
        ("agent_id", "DEFAULT"),
        ("job_id", "JOB"),
    ],
)
async def test_case_distinct_state_is_missing(
    client,
    database,
    component,
    value,
):
    database.add("job")
    database.conn.execute(
        f"UPDATE swe_cron_job_states SET {component}=?",
        (value,),
    )
    response = await runtime(client, ["job"])
    assert response.json()["items"] == [
        {"id": "job", "status": "state_missing", "definition_version": 1},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids",
    [[], ["same"] * 201, [""], ["x" * 129], ["../x"], [" x"], [1], ["x\x00"]],
)
async def test_invalid_batch_rejected_before_query(client, database, ids):
    assert (await runtime(client, ids)).status_code == 422
    assert database.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 199, 200])
async def test_valid_batch_capacity_and_deduplication(client, database, count):
    response = await runtime(client, ["same"] * count)
    assert response.status_code == 200
    assert response.json()["items"] == [
        {"id": "same", "status": "unavailable"},
    ]
    assert len(database.queries) == 1


@pytest.mark.asyncio
async def test_identity_conflicts_and_database_failure_are_errors(
    client,
    database,
):
    assert (
        await runtime(client, ["a"], {**HEADERS, "X-Scope-Id": "wrong"})
    ).status_code == 400
    database.broken = True
    response = await runtime(client, ["a"])
    assert response.status_code == 503
    assert "private credentials" not in response.text


@pytest.mark.asyncio
async def test_dates_and_pause_recomputed_without_state_version_change(
    client,
    database,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import date_policy

    database.add("job", last_error=None, latest_scheduled_preview="short")
    database.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET run_start_date='2026-11-06', run_end_date='2026-11-07'",
    )
    clock = [datetime(2026, 11, 5, tzinfo=date_policy.BEIJING)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz)

    monkeypatch.setattr(date_policy, "datetime", Clock)
    monkeypatch.setattr(
        "scheduler.app.services.cron.managed.views.datetime",
        Clock,
    )
    first = (await runtime(client, ["job"])).json()["items"][0]
    assert first["task"]["pause_reason"] == "date_before_start"
    assert first["state"]["next_run_times"] == [
        "2026-11-06T09:00:00+08:00",
        "2026-11-07T09:00:00+08:00",
    ]
    clock[0] = datetime(2026, 11, 8, tzinfo=date_policy.BEIJING)
    second = (await runtime(client, ["job"])).json()["items"][0]
    assert second["task"]["pause_reason"] == "date_expired"
    assert second["state"]["next_run_times"] == []
    assert second["state_version"] == first["state_version"]
    assert second["state"]["last_error"] is None
    assert second["state"]["last_error_truncated"] is False
    assert second["task"]["latest_scheduled_preview_truncated"] is False
    assert database.writes == 0


@pytest.mark.asyncio
async def test_old_legal_ids_remain_available(client, database):
    for job_id in ("-", "runtime-statuses"):
        database.add(job_id)
        response = await client.get(f"{PATH}/{job_id}", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["spec"]["id"] == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,value",
    [("state_version", None), ("management_version", None)],
)
async def test_invalid_required_state_returns_unavailable_error(
    client,
    database,
    column,
    value,
):
    database.add("job")
    database.conn.execute(
        f"UPDATE swe_cron_job_states SET {column}=?",
        (value,),
    )
    response = await runtime(client, ["job"])
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_exact_limits_disabled_task_and_null_fields(client, database):
    database.add(
        "job",
        enabled=False,
        last_error="错" * 1024,
        latest_scheduled_preview="结" * 256,
    )
    response = await runtime(client, ["job"])
    item = response.json()["items"][0]
    assert item["enabled"] is False
    assert item["task"]["pause_reason"] == "manual"
    assert item["task"]["is_paused"] is True
    assert item["task"]["latest_scheduled_preview_truncated"] is False
    assert item["state"]["last_error_truncated"] is False
    assert item["state"]["last_run_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,value",
    [
        ("timezone", "invalid/zone"),
        ("cron_expr", "invalid"),
        ("run_start_date", "not-a-date"),
    ],
)
async def test_invalid_display_data_is_not_success(
    client,
    database,
    column,
    value,
):
    database.add("job")
    database.conn.execute(f"UPDATE swe_cron_jobs_v2 SET {column}=?", (value,))
    assert (await runtime(client, ["job"])).status_code == 503


@pytest.mark.asyncio
async def test_disconnected_database_is_not_empty_success(client, database):
    database.is_connected = False
    assert (await runtime(client, ["job"])).status_code == 503
    assert database.queries == []


@pytest.mark.asyncio
async def test_body_cannot_override_scope(client, database):
    response = await client.post(
        f"{PATH}/-/runtime-statuses",
        headers=HEADERS,
        json={"job_ids": ["job"], "tenant_id": "bob"},
    )
    assert response.status_code == 422
    assert database.queries == []
