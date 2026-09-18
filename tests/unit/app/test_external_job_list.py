# -*- coding: utf-8 -*-
"""External jobs must preserve the list contract without creating runtime/data."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.app.auth import AuthMiddleware
from swe.app.crons.manager import CronManager
from swe.app.crons.models import CronJobSpec, CronJobState, JobsFile
from swe.app.middleware.tenant_identity import TenantIdentityMiddleware
from swe.app.middleware.tenant_workspace import TenantWorkspaceMiddleware
from swe.app.multi_agent_manager import MultiAgentManager
from swe.app.routers.agent_scoped import AgentContextMiddleware
from swe.app.routers.external_jobs import router
from swe.app.source_system_config.middleware import (
    SourceSystemConfigMiddleware,
)
from swe.config import utils as config_utils
from swe.config.context import resolve_storage_tenant_id

URL = "/api/external/cron/jobs"
HEADERS = {"X-Tenant-Id": "alice", "X-Source-Id": "portal"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config_utils, "WORKING_DIR", tmp_path)
    monkeypatch.setenv("SWE_AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.get("/api/cron/jobs")
    async def original_list():
        return []

    app.add_middleware(AuthMiddleware)
    app.add_middleware(AgentContextMiddleware)
    app.add_middleware(TenantWorkspaceMiddleware)
    app.add_middleware(SourceSystemConfigMiddleware)
    app.add_middleware(TenantIdentityMiddleware)
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    manager = SimpleNamespace(
        get_agent=AsyncMock(),
        agents={},
        _cache_key=MultiAgentManager._cache_key,
    )
    app.state.tenant_workspace_pool = pool
    app.state.multi_agent_manager = manager
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, root=tmp_path, pool=pool, manager=manager
        )


def _tenant(root, tenant="alice", source="portal"):
    return root / resolve_storage_tenant_id(tenant, source)


def _job(job_id="job-1", *, owner="alice", bound=True, enabled=True):
    meta = {}
    if bound:
        meta = {
            "creator_user_id": owner,
            "task_chat_id": f"chat-{job_id}",
            "task_session_id": f"cron-task:{job_id}",
            "task_has_scheduled_result": True,
            "task_unread_execution_count": 2,
            "task_last_scheduled_preview": "Existing result",
        }
    return CronJobSpec.model_validate(
        {
            "id": job_id,
            "name": job_id,
            "enabled": enabled,
            "tenant_id": "alice",
            "source_id": "portal",
            "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            "task_type": "agent",
            "request": {"input": "run report"},
            "dispatch": {
                "target": {"user_id": owner, "session_id": f"s-{job_id}"}
            },
            "meta": meta,
        }
    )


def _seed(root, jobs, *, tenant="alice", source="portal", agent="default"):
    tenant_dir = _tenant(root, tenant, source)
    workspace = tenant_dir / "workspaces" / agent
    workspace.mkdir(parents=True)
    (tenant_dir / "config.json").write_text(
        json.dumps(
            {
                "user_timezone": "Asia/Shanghai",
                "agents": {
                    "active_agent": agent,
                    "profiles": {
                        agent: {"id": agent, "workspace_dir": str(workspace)},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "jobs.json").write_text(
        JobsFile(jobs=jobs).model_dump_json(),
        encoding="utf-8",
    )
    return workspace


def _snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
        for p in root.rglob("*")
    }


@pytest.mark.parametrize("suffix", ["", "/", "?page=1&user_id=bob"])
def test_absent_tenant_is_empty_without_initialization(api, suffix):
    response = api.client.get(URL + suffix, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == []
    assert list(api.root.iterdir()) == []
    api.pool.ensure_bootstrap.assert_not_awaited()
    api.manager.get_agent.assert_not_awaited()


@pytest.mark.parametrize("user_id", [None, "alice", "bob"])
@pytest.mark.parametrize("loaded", [False, True])
def test_bound_jobs_match_original_list_response(
    api, monkeypatch, user_id, loaded
):
    from swe.app.crons import api as cron_api
    from swe.app.crons import manager as manager_module
    from swe.app.routers import external_jobs

    jobs = [_job(), _job("job-2", owner="bob", enabled=False)]
    _seed(api.root, jobs)
    next_runs = [
        datetime(2030, 1, day, 1, tzinfo=timezone.utc) for day in (1, 2, 3)
    ]
    monkeypatch.setattr(
        manager_module, "compute_next_run_times", lambda *a, **kw: next_runs
    )
    monkeypatch.setattr(
        external_jobs, "compute_next_run_times", lambda *a, **kw: next_runs
    )
    # Real legacy read methods with no runtime startup in the comparison harness.
    legacy_manager = object.__new__(CronManager)
    legacy_manager._states = (
        {"job-1": CronJobState(last_status="running")} if loaded else {}
    )
    legacy_manager._timezone = "Asia/Shanghai"
    legacy_manager.list_jobs = AsyncMock(return_value=jobs)
    legacy_manager.create_or_replace_job = AsyncMock(
        side_effect=AssertionError("must not write")
    )
    if loaded:
        scope = resolve_storage_tenant_id("alice", "portal")
        api.manager.agents[api.manager._cache_key("default", scope)] = (
            SimpleNamespace(cron_manager=legacy_manager)
        )
    legacy_app = FastAPI()
    legacy_app.include_router(cron_api.router, prefix="/api")
    legacy_app.add_middleware(TenantIdentityMiddleware)
    legacy_app.dependency_overrides[cron_api.get_cron_manager] = (
        lambda: legacy_manager
    )
    headers = dict(HEADERS)
    if user_id is not None:
        headers["X-User-Id"] = user_id
    before = _snapshot(api.root)
    response = api.client.get(URL + "?user_id=ignored", headers=headers)
    state_before_legacy = {
        key: value.model_dump()
        for key, value in legacy_manager._states.items()
    }
    assert all(
        value["next_run_at"] is None for value in state_before_legacy.values()
    )
    with TestClient(legacy_app) as client:
        expected = client.get(
            "/api/cron/jobs?user_id=ignored", headers=headers
        )
    assert response.status_code == expected.status_code == 200
    assert response.json() == expected.json()
    assert (
        len(response.json()) == 2
    )  # Header affects task visibility, not filtering.
    assert _snapshot(api.root) == before
    legacy_manager.create_or_replace_job.assert_not_awaited()
    api.pool.ensure_bootstrap.assert_not_awaited()
    api.manager.get_agent.assert_not_awaited()


def test_unbound_jobs_do_not_create_chats_or_bindings(api, monkeypatch):
    _seed(api.root, [_job(bound=False)])
    before = _snapshot(api.root)

    def forbid_constructor(*args, **kwargs):
        raise AssertionError("must not create a CronManager")

    monkeypatch.setattr(CronManager, "__init__", forbid_constructor)
    response = api.client.get(URL, headers={**HEADERS, "X-User-Id": "alice"})
    assert response.status_code == 200
    item = response.json()[0]
    assert item["task"]["chat_id"] is None
    assert item["task"]["session_id"] is None
    assert item["meta"] == {}
    assert item["state"]["last_status"] is None
    assert len(item["state"]["next_run_times"]) == 3
    assert _snapshot(api.root) == before


@pytest.mark.parametrize(
    "tenant,source",
    [("bob", "portal"), ("alice", "other"), ("default", "portal")],
)
def test_tenant_and_source_storage_isolation(api, tenant, source):
    _seed(api.root, [_job("own")])
    _seed(api.root, [_job("separate")], tenant=tenant, source=source)
    response = api.client.get(
        URL, headers={**HEADERS, "X-Tenant-Id": tenant, "X-Source-Id": source}
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["separate"]


def test_existing_default_jobs_survive_missing_config(api):
    workspace = _seed(api.root, [_job()])
    (workspace.parent.parent / "config.json").unlink()
    before = _snapshot(api.root)
    assert api.client.get(URL, headers=HEADERS).json()[0]["id"] == "job-1"
    assert _snapshot(api.root) == before


def test_missing_jobs_file_stays_absent(api):
    workspace = _seed(api.root, [])
    (workspace / "jobs.json").unlink()
    before = _snapshot(api.root)
    assert api.client.get(URL, headers=HEADERS).json() == []
    assert _snapshot(api.root) == before


def test_active_and_explicit_agent_selection(api):
    active = _seed(api.root, [_job("research")], agent="research")
    other = active.parent / "other"
    other.mkdir()
    (other / "jobs.json").write_text(
        JobsFile(jobs=[_job("other")]).model_dump_json()
    )
    path = active.parent.parent / "config.json"
    data = json.loads(path.read_text())
    data["agents"]["profiles"]["other"] = {
        "id": "other",
        "workspace_dir": str(other),
    }
    path.write_text(json.dumps(data))
    assert api.client.get(URL, headers=HEADERS).json()[0]["id"] == "research"
    assert (
        api.client.get(URL, headers={**HEADERS, "X-Agent-Id": "other"}).json()[
            0
        ]["id"]
        == "other"
    )


@pytest.mark.parametrize("missing", ["X-Tenant-Id", "X-Source-Id"])
def test_identity_headers_remain_required(api, missing):
    assert (
        api.client.get(
            URL, headers={k: v for k, v in HEADERS.items() if k != missing}
        ).status_code
        == 400
    )
    assert list(api.root.iterdir()) == []


def test_auth_is_not_exempt(api, monkeypatch):
    from swe.app import auth

    monkeypatch.setenv("SWE_AUTH_ENABLED", "true")
    monkeypatch.setattr(auth, "has_registered_users", lambda: True)
    assert api.client.get(URL, headers=HEADERS).status_code == 401
    api.pool.ensure_bootstrap.assert_not_awaited()


@pytest.mark.parametrize(
    "filename", ["config.json", "workspaces/default/jobs.json"]
)
def test_corrupt_storage_is_not_repaired(api, filename):
    _seed(api.root, [_job()])
    (_tenant(api.root) / filename).write_text("{broken")
    before = _snapshot(api.root)
    response = api.client.get(URL, headers=HEADERS)
    assert response.status_code == 503
    assert str(api.root) not in response.text
    assert _snapshot(api.root) == before


def test_unknown_disabled_and_foreign_agent_paths(api):
    assert (
        api.client.get(
            URL, headers={**HEADERS, "X-Agent-Id": "unknown"}
        ).status_code
        == 404
    )
    workspace = _seed(api.root, [_job()])
    path = workspace.parent.parent / "config.json"
    data = json.loads(path.read_text())
    data["agents"]["profiles"]["default"]["enabled"] = False
    path.write_text(json.dumps(data))
    assert api.client.get(URL, headers=HEADERS).status_code == 403
    data["agents"]["profiles"]["default"] = {
        "id": "default",
        "workspace_dir": str(api.root / "other"),
    }
    path.write_text(json.dumps(data))
    assert api.client.get(URL, headers=HEADERS).status_code == 403


def test_jobs_symlink_cannot_read_another_tenant(api):
    own = _seed(api.root, [])
    foreign = _seed(api.root, [_job("secret")], tenant="bob")
    (own / "jobs.json").unlink()
    (own / "jobs.json").symlink_to(foreign / "jobs.json")
    assert api.client.get(URL, headers=HEADERS).status_code == 403


def test_original_jobs_still_bootstraps(api, monkeypatch):
    from swe.app.middleware import tenant_workspace
    from swe.app.workspace.bootstrap_state import TenantBootstrapUnavailable

    monkeypatch.setattr(
        tenant_workspace,
        "resolve_user_identity",
        AsyncMock(
            return_value=SimpleNamespace(user_name="Alice", bbk_id="id")
        ),
    )
    api.pool.ensure_bootstrap.side_effect = TenantBootstrapUnavailable("test")
    assert api.client.get("/api/cron/jobs", headers=HEADERS).status_code == 503
    api.pool.ensure_bootstrap.assert_awaited_once()
