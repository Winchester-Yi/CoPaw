# -*- coding: utf-8 -*-
"""新旧对外查询独立，新入口只按Header定位数据库中的新版用户任务。"""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from scheduler.app.database import connection
from scheduler.app.routers import api_router
from scheduler.app.services.cron import workday_calendar
from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase

URL = "/api/scheduler/external/cron/jobs"
HEADERS = {"X-Tenant-Id": "alice", "X-Source-Id": "bank"}


class Calendar:
    async def get_day(self, day):
        return {
            "date": day.isoformat(),
            "is_workday": day.weekday() < 5,
            "source": "api",
        }


@pytest.fixture
async def query_api(monkeypatch):
    db = QueryDatabase()
    monkeypatch.setattr(connection, "get_db_connection", lambda: db)
    monkeypatch.setattr(workday_calendar, "_calendar_service", Calendar())
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://scheduler",
    ) as client:
        yield client, db
    db.conn.close()


@pytest.mark.asyncio
async def test_default_agent_is_selected_without_swe_or_viewer(query_api):
    client, db = query_api
    db.add(
        "new-default",
        has_scheduled_result=1,
        latest_scheduled_preview="DB result",
    )
    db.add("other-agent", agent="research")
    db.add("other-tenant", tenant="bob")
    db.add("other-source", source="portal")
    db.add("case-distinct", source="BANK")
    db.add("deleted", deleted=True)
    db.add("legacy-owner", owner="swe")
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200
    items = response.json()
    assert [item["id"] for item in items] == ["new-default"]
    item = items[0]
    assert item["task"]["visible_in_my_tasks"] is False
    assert item["task"]["latest_scheduled_preview"] == "DB result"
    assert item["meta"]["task_last_scheduled_preview"] == "DB result"
    assert (
        item["task"]["unread_execution_count"]
        == item["meta"]["task_unread_execution_count"]
        == 2
    )
    assert item["schedule"]["run_on_non_workday"] is False
    assert "Authorization" not in item["meta"]
    assert len(item["state"]["next_run_times"]) == 3
    assert db.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dates,enabled,stored_reason,expected",
    [
        (("2000-01-01", "2000-01-02"), True, None, "date_expired"),
        (("2099-01-01", "2099-01-02"), True, None, "date_before_start"),
        ((None, None), True, None, None),
        ((None, None), False, None, "manual"),
        ((None, None), True, "manual", "manual"),
        (("2000-01-01", "2000-01-02"), False, None, "manual"),
        (
            ("2000-01-01", "2000-01-02"),
            True,
            "auto_unread_threshold",
            "auto_unread_threshold",
        ),
        (
            ("2000-01-01", "2000-01-02"),
            False,
            "auto_unread_threshold",
            "auto_unread_threshold",
        ),
    ],
)
async def test_compat_pause_reason_matches_effective_task_state_without_writes(
    query_api,
    dates,
    enabled,
    stored_reason,
    expected,
):
    client, db = query_api
    db.add("pause-case", enabled=enabled, pause_reason=stored_reason)
    spec = json.loads(
        db.conn.execute("SELECT spec_json FROM swe_cron_jobs_v2").fetchone()[
            0
        ],
    )
    spec["run_date_range"] = (
        {"start_date": dates[0], "end_date": dates[1]} if dates[0] else None
    )
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET run_start_date=?,run_end_date=?,spec_json=?",
        (*dates, json.dumps(spec)),
    )
    before = list(db.conn.iterdump())
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200
    item = response.json()[0]
    assert item["task"]["pause_reason"] == expected
    assert item["meta"]["pause_reason"] == expected
    assert item["task"]["is_paused"] is (expected is not None)
    assert item["enabled"] is enabled
    assert list(db.conn.iterdump()) == before


@pytest.mark.asyncio
async def test_explicit_agent_and_optional_viewer_do_not_replace_tenant(
    query_api,
):
    client, db = query_api
    db.add("default")
    db.add("research", agent="research", actor="viewer")
    response = await client.get(
        URL,
        headers={**HEADERS, "X-Agent-Id": "research", "X-User-Id": "viewer"},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["research"]
    assert response.json()[0]["task"]["visible_in_my_tasks"] is True


@pytest.mark.asyncio
async def test_complete_array_is_not_limited_to_one_database_page(query_api):
    client, db = query_api
    for index in range(205):
        db.add(f"job-{index:03}")
    before = list(db.conn.iterdump())
    response = await client.get(
        URL + "/",
        headers=HEADERS,
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert len(response.json()) == 205
    assert response.json()[0]["id"] == "job-000"
    assert response.json()[-1]["id"] == "job-204"
    assert list(db.conn.iterdump()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["X-Tenant-Id", "X-Source-Id"])
async def test_tenant_and_source_remain_required_without_actor_substitution(
    query_api,
    missing,
):
    client, _ = query_api
    response = await client.get(
        URL,
        headers={
            **{k: v for k, v in HEADERS.items() if k != missing},
            "X-User-Id": "alice",
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{"X-Agent-Id": "../other"}, {"X-Scope-Id": "wrong-scope"}],
)
async def test_invalid_identity_is_rejected(query_api, headers):
    client, _ = query_api
    assert (
        await client.get(URL, headers={**HEADERS, **headers})
    ).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    ["YWxpY2U.YmFuaw", "scope.v1.YWxpY2U.YmFuaw", "alice-bank"],
)
async def test_matching_optional_scope_is_accepted(query_api, scope):
    client, db = query_api
    db.add("own")
    response = await client.get(URL, headers={**HEADERS, "X-Scope-Id": scope})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["own"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["database", "missing-state", "case-distinct-state"],
)
async def test_unavailable_is_not_disguised_as_empty_result(
    query_api,
    failure,
):
    client, db = query_api
    db.add("new", with_state=failure != "missing-state")
    if failure == "database":
        db.broken = True
    elif failure == "case-distinct-state":
        db.conn.execute("UPDATE swe_cron_job_states SET source_id='BANK'")
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 503
    assert "credential" not in response.text


@pytest.mark.asyncio
async def test_empty_database_has_no_file_or_runtime_fallback(query_api):
    client, _ = query_api
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200 and response.json() == []


@pytest.mark.asyncio
async def test_workday_rules_and_binding_fields_are_returned_from_database(
    query_api,
):
    client, db = query_api
    db.add(
        "new",
        ordinary_external_job_id="ordinary-1",
        batch_external_job_id="batch-2",
        dispatch_state="batch_paused",
    )
    spec = json.loads(
        db.conn.execute("SELECT spec_json FROM swe_cron_jobs_v2").fetchone()[
            0
        ],
    )
    spec["schedule"]["run_on_non_workday"] = True
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET cron_expr='0 10 LW * *',spec_json=?",
        (json.dumps(spec),),
    )
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200
    item = response.json()[0]
    assert item["schedule"] == {
        "type": "cron",
        "cron": "0 10 LW * *",
        "timezone": "Asia/Shanghai",
        "run_on_non_workday": True,
    }
    assert (
        item["state"]["external_job_id"]
        == item["meta"]["external_job_id"]
        == "ordinary-1"
    )
    assert item["meta"]["batch_dispatch_external_job_id"] == "batch-2"
    assert item["meta"]["broadcast_dispatch_intents_enabled"] is True
    assert all(
        datetime.fromisoformat(at.replace("Z", "+00:00")).hour == 10
        for at in item["state"]["next_run_times"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Basic privatevalue",
        "Bearer privatevalue",
        "Cookie: session=privatevalue",
        "refresh_token='privatevalue'",
    ],
)
async def test_embedded_credentials_are_redacted_without_exposing_private_keys(
    query_api,
    secret,
):
    client, db = query_api
    db.add("new")
    spec = json.loads(
        db.conn.execute("SELECT spec_json FROM swe_cron_jobs_v2").fetchone()[
            0
        ],
    )
    spec["meta"].update(
        note=secret,
        nested={"_scheduler_token": "privatevalue"},
    )
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=?",
        (json.dumps(spec),),
    )
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200
    assert "privatevalue" not in response.text
    assert "[REDACTED]" in response.json()[0]["meta"]["note"]


def test_new_router_import_does_not_load_swe_runtime():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from scheduler.app.routers import api_router; assert not any(key == 'swe' or key.startswith('swe.') for key in sys.modules)",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "scheduler/src")},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_child_calendar_preview_applies_offset_to_parent_rule(query_api):
    from scheduler.app.services.cron.managed.external_query import (
        ExternalManagedJobs,
    )

    _, db = query_api
    db.add("child")
    raw = json.loads(
        db.conn.execute("SELECT spec_json FROM swe_cron_jobs_v2").fetchone()[
            0
        ],
    )
    raw["meta"].update(
        broadcast_original_cron="0 10 LW * *",
        broadcast_original_timezone="Asia/Shanghai",
        broadcast_offset_minutes=120,
    )
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET cron_expr='0 8 LW * *',spec_json=?",
        (json.dumps(raw),),
    )
    items = await ExternalManagedJobs(
        db,
        clock=lambda: datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
    ).list("alice", "bank", "default")
    assert (
        items[0]["state"]["next_run_at"].isoformat()
        == "2026-09-30T08:00:00+08:00"
    )
    assert db.writes == 0


@pytest.mark.asyncio
async def test_preview_reports_fallback_and_reuses_page_date_observations(
    query_api,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.external_query import (
        ExternalManagedJobs,
    )

    _, db = query_api
    db.add("first")
    db.add("second")
    seen = []

    class FallbackCalendar:
        async def get_day(self, day):
            seen.append(day)
            return {"is_workday": day.weekday() < 5, "source": "fallback"}

    rows = await ExternalManagedJobs(
        db,
        calendar=FallbackCalendar(),
        clock=lambda: datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
    ).list("alice", "bank", "default")
    assert len(seen) == 3
    assert all(item["meta"]["calendar_source"] == "fallback" for item in rows)


@pytest.mark.asyncio
async def test_viewer_only_changes_visibility_not_the_returned_task_set(
    query_api,
):
    client, db = query_api
    db.add("alice", actor="alice")
    db.add("bob", actor="bob")
    response = await client.get(URL, headers={**HEADERS, "X-User-Id": "bob"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["alice", "bob"]
    assert [
        item["task"]["visible_in_my_tasks"] for item in response.json()
    ] == [False, True]
