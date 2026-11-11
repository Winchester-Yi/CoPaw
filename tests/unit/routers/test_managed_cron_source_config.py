# -*- coding: utf-8 -*-
"""真实内部 HTTP 中间件和执行写入证明配置不回退到默认 10。"""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from scheduler.app.services.cron.managed.dispatcher import ExecutionDispatcher
from scheduler.app.services.cron.managed.executions import (
    ExecutionRequest,
    ExecutionStore,
)
from scheduler.app.services.cron.managed.identity import CallerIdentity
from swe.app.crons import managed_execution_api as api
from swe.app.crons.managed_execution import ManagedExecutionService
from swe.app.crons.managed_receipts import SessionResultReceipts
from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter
from swe.app.middleware.tenant_identity import TenantIdentityMiddleware
from swe.app.source_system_config.middleware import (
    SourceSystemConfigMiddleware,
)
from swe.app.source_system_config.runtime import (
    bind_source_system_config,
    get_current_source_system_config,
)
from tests.fakes.managed_source_config import ManagedSourceConfigService
from tests.unit.app.test_managed_cron_execution import Engine, key
from tests.unit.app.test_managed_cron_result_recovery import Session
from tests.unit.scheduler.test_managed_cron_executions import execution_setup
from tests.unit.scheduler.test_managed_cron_management import create_payload


def configured_app(db, config):
    app = FastAPI()
    app.state.db_connection = db
    app.state.source_system_config_service = config
    app.include_router(api.router, prefix="/api")
    app.add_middleware(SourceSystemConfigMiddleware)
    app.add_middleware(TenantIdentityMiddleware, default_tenant_id=None)
    return app


def source_config(*, enabled=True):
    return ManagedSourceConfigService(
        {
            "bank": {
                "cron_unread_auto_pause": {
                    "enabled": enabled,
                    "threshold": 20,
                },
            },
            "other": {
                "cron_unread_auto_pause": {"enabled": True, "threshold": 30},
            },
        },
    )


async def queued(setup, unread, *, kind="ordinary", caller=None):
    db, original, management = setup
    caller = caller or original
    created = await management.create(caller, create_payload())
    db.conn.execute(
        "UPDATE swe_cron_job_states SET unread_execution_count=? WHERE job_id=?",
        (unread, created["job_id"]),
    )
    run, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest(
            kind,
            "period",
            scheduled_at=datetime(2026, 11, 3, 1, tzinfo=timezone.utc),
        ),
    )
    return await ExecutionDispatcher(db, None)._claim(run, True)


def headers(run):
    return {
        "X-User-Id": run["actor_id"],
        "X-Tenant-Id": run["tenant_id"],
        "X-Source-Id": run["source_id"],
        "X-Agent-Id": run["agent_id"],
    }


async def execute(http, run):
    return await http.post(
        "/api/internal/cron/managed/execute",
        headers=headers(run),
        json={
            "execution_id": run["execution_id"],
            "job_id": run["job_id"],
            "dispatch_token": run["dispatch_owner"],
        },
    )


def state(db, run):
    return dict(
        db.conn.execute(
            "SELECT unread_execution_count,pause_reason FROM swe_cron_job_states WHERE job_id=?",
            (run["job_id"],),
        ).fetchone(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,unread,enabled,paused",
    [
        ("ordinary", 9, True, False),
        ("batch", 19, True, True),
        ("ordinary", 19, False, False),
        ("manual", 19, True, False),
        ("manual", 20, True, False),
    ],
)
async def test_effective_policy_survives_http_response_until_result_commit(
    execution_setup,
    monkeypatch,
    kind,
    unread,
    enabled,
    paused,
):
    db = execution_setup[0]
    run = await queued(execution_setup, unread, kind=kind)
    config = source_config(enabled=enabled)
    app = configured_app(db, config)
    started, release = asyncio.Event(), asyncio.Event()
    engine = Engine(started=started, release=release)

    async def factory(_request):
        return ManagedExecutionService(ExecutionWriter(db), engine)

    monkeypatch.setattr(api, "get_execution_service", factory)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        try:
            response = await execute(http, run)
            assert response.status_code == 202
            await asyncio.wait_for(started.wait(), 2)
            assert get_current_source_system_config() is None
            # 配置在请求返回后被修改，已启动执行仍使用本次快照。
            config.overrides["bank"]["cron_unread_auto_pause"]["threshold"] = 1
        finally:
            tasks = tuple(getattr(app.state, "managed_cron_tasks", ()))
            release.set()
            await asyncio.gather(*tasks)
    actual = state(db, run)
    assert actual["unread_execution_count"] == unread + 1
    assert actual["pause_reason"] == (
        "auto_unread_threshold" if paused else None
    )
    assert config.calls == ["bank"]


@pytest.mark.asyncio
async def test_concurrent_sources_do_not_share_thresholds(
    execution_setup,
    monkeypatch,
):
    db = execution_setup[0]
    runs = [
        await queued(execution_setup, 19),
        await queued(
            execution_setup,
            19,
            caller=CallerIdentity("bob", "other", "default", "bob"),
        ),
    ]
    app = configured_app(db, source_config())
    engines = {
        run["source_id"]: Engine(
            started=asyncio.Event(),
            release=asyncio.Event(),
        )
        for run in runs
    }

    async def factory(request):
        return ManagedExecutionService(
            ExecutionWriter(db),
            engines[request.headers["X-Source-Id"]],
        )

    monkeypatch.setattr(api, "get_execution_service", factory)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        try:
            results = await asyncio.gather(
                *(execute(http, run) for run in runs),
            )
            assert [r.status_code for r in results] == [202, 202]
            await asyncio.wait_for(
                asyncio.gather(*(e.started.wait() for e in engines.values())),
                2,
            )
        finally:
            tasks = tuple(getattr(app.state, "managed_cron_tasks", ()))
            for engine in engines.values():
                engine.release.set()
            await asyncio.gather(*tasks)
    assert state(db, runs[0])["pause_reason"] == "auto_unread_threshold"
    assert state(db, runs[1])["pause_reason"] is None
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_recover_uses_source_policy_without_rerunning_engine(
    execution_setup,
    monkeypatch,
):
    db = execution_setup[0]
    run = await queued(execution_setup, 9)
    writer = ExecutionWriter(db)
    await writer.accept(run["execution_id"], key(run))
    started = await writer.start(
        run["execution_id"],
        key(run),
        "original-worker",
    )
    receipts = SessionResultReceipts(Session())
    await receipts.save(started, ExecutionOutcome("success", preview="saved"))
    engine = Engine()

    async def factory(_request):
        return ManagedExecutionService(writer, engine, receipts=receipts)

    monkeypatch.setattr(api, "get_execution_service", factory)
    app = configured_app(db, source_config())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        response = await http.post(
            "/api/internal/cron/managed/recover",
            headers=headers(run),
            json={
                "execution_id": run["execution_id"],
                "job_id": run["job_id"],
            },
        )
    assert response.status_code == 200
    assert response.json()["recovered"] is True
    assert state(db, run) == {
        "unread_execution_count": 10,
        "pause_reason": None,
    }
    assert not engine.calls
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "unavailable", "wrong_source"])
@pytest.mark.parametrize("action", ["execute", "recover"])
async def test_unusable_configuration_does_not_start_business(
    execution_setup,
    monkeypatch,
    failure,
    action,
    caplog,
):
    db = execution_setup[0]
    run = await queued(execution_setup, 9)
    config = source_config()
    original = config.resolve_config

    async def broken(source):
        if failure == "unavailable":
            raise RuntimeError("private-config-value")
        return await original("other")

    monkeypatch.setattr(config, "resolve_config", broken)
    app = configured_app(db, None if failure == "missing" else config)
    engine = Engine()

    async def factory(_request):
        return ManagedExecutionService(ExecutionWriter(db), engine)

    monkeypatch.setattr(api, "get_execution_service", factory)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        if action == "execute":
            response = await execute(http, run)
        else:
            response = await http.post(
                "/api/internal/cron/managed/recover",
                headers=headers(run),
                json={
                    "execution_id": run["execution_id"],
                    "job_id": run["job_id"],
                },
            )
        await asyncio.gather(*getattr(app.state, "managed_cron_tasks", ()))
    assert response.status_code == 503
    assert response.json()["detail"] == "cron_source_config_unavailable"
    assert not engine.calls
    assert state(db, run)["unread_execution_count"] == 9
    assert "private-config-value" not in caplog.text
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_authorized_source_overrides_and_restores_foreign_context(
    execution_setup,
    monkeypatch,
):
    db = execution_setup[0]
    run = await queued(execution_setup, 19)
    config = source_config()
    outer = await config.resolve_config("other")
    app = configured_app(db, config)

    async def factory(_request):
        return ManagedExecutionService(ExecutionWriter(db), Engine())

    monkeypatch.setattr(api, "get_execution_service", factory)
    with bind_source_system_config(outer):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http:
            response = await execute(http, run)
            assert response.status_code == 202
            assert get_current_source_system_config() is outer
            await asyncio.gather(*app.state.managed_cron_tasks)
    assert get_current_source_system_config() is None
    assert state(db, run)["pause_reason"] == "auto_unread_threshold"


@pytest.mark.asyncio
async def test_unauthorized_request_does_not_resolve_source_config(
    execution_setup,
):
    db = execution_setup[0]
    run = await queued(execution_setup, 9)
    config = source_config()
    app = configured_app(db, config)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        response = await http.post(
            "/api/internal/cron/managed/execute",
            headers={**headers(run), "X-User-Id": "intruder"},
            json={
                "execution_id": run["execution_id"],
                "job_id": run["job_id"],
                "dispatch_token": run["dispatch_owner"],
            },
        )
    assert response.status_code == 404
    assert config.calls == []
    assert state(db, run)["unread_execution_count"] == 9
