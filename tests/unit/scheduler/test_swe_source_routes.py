# -*- coding: utf-8 -*-
"""Scheduler outbound routing keeps each endpoint's existing fallback."""

import json
from types import SimpleNamespace

import httpx
import pytest

from scheduler.app.services.cron.batch_status_report.runtime import (
    BatchReportWorker,
)
from scheduler.app.services.cron.managed.dispatcher import (
    ManagedSweExecutionClient,
)
from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.model_resolution import (
    resolve_agent_model,
)
from scheduler.app.services.cron.managed.swe_client import ManagedSweClient
from scheduler.app.services.cron.scheduling_service import (
    SweCronCallbackClient,
)
from scheduler.app.services.cron.swe_routing import (
    swe_base_for_source,
    validate_swe_source_routes,
)
from scheduler.app.services.cron.system.swe_client import SystemSweClient
from scheduler.env_defaults import SWE_SOURCE_ROUTES_ENV, load_env_defaults


def test_config_loads_source_route_groups_as_json(tmp_path, monkeypatch):
    config = tmp_path / "config" / "envs"
    config.mkdir(parents=True)
    groups = [
        {
            "api_base_url": "http://api",
            "internal_api_base_url": "http://internal",
            "source_ids": ["BANK"],
        },
    ]
    (config / "dev.json").write_text(
        json.dumps({SWE_SOURCE_ROUTES_ENV: groups}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scheduler.env_defaults._get_package_dir",
        lambda: tmp_path,
    )
    monkeypatch.delenv(SWE_SOURCE_ROUTES_ENV, raising=False)
    load_env_defaults("dev")
    validate_swe_source_routes()
    assert swe_base_for_source("BANK", "http://default") == "http://api"
    assert (
        swe_base_for_source("BANK", "http://default", internal=True)
        == "http://internal"
    )
    assert swe_base_for_source("bank", "http://default") == "http://default"


def test_invalid_source_routes_reject_ambiguous_mapping(monkeypatch):
    monkeypatch.setenv(
        SWE_SOURCE_ROUTES_ENV,
        json.dumps(
            [
                {
                    "api_base_url": "https://swe-a.example",
                    "internal_api_base_url": "https://swe-a-internal.example",
                    "source_ids": ["BANK"],
                },
                {
                    "api_base_url": "https://swe-b.example",
                    "internal_api_base_url": "https://swe-b-internal.example",
                    "source_ids": ["BANK"],
                },
            ],
        ),
    )
    with pytest.raises(ValueError, match=SWE_SOURCE_ROUTES_ENV):
        validate_swe_source_routes()


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://swe.example:invalid",
        "https://swe.example:65536",
        "https://swe.example:",
        "https://swe.example:0",
        "https://swe.example?",
        "https://swe.example#",
    ],
)
def test_startup_rejects_invalid_source_route_url(monkeypatch, invalid_url):
    monkeypatch.setenv(
        SWE_SOURCE_ROUTES_ENV,
        json.dumps(
            [
                {
                    "api_base_url": "https://swe-api.example:8443",
                    "internal_api_base_url": invalid_url,
                    "source_ids": ["BANK"],
                },
            ],
        ),
    )
    with pytest.raises(ValueError, match=SWE_SOURCE_ROUTES_ENV):
        validate_swe_source_routes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected_api_host", "expected_internal_host"),
    [
        ("a1", "swe-a.example", "swe-a-internal.example"),
        ("a2", "swe-a.example", "swe-a-internal.example"),
        ("b1", "swe-b.example", "swe-b-internal.example"),
        ("other", None, None),
    ],
)
async def test_source_routing_across_swe_clients(
    monkeypatch,
    source,
    expected_api_host,
    expected_internal_host,
):
    monkeypatch.setenv(
        SWE_SOURCE_ROUTES_ENV,
        json.dumps(
            [
                {
                    "api_base_url": "https://swe-a.example/api/",
                    "internal_api_base_url": "https://swe-a-internal.example/api/",
                    "source_ids": ["a1", "a2"],
                },
                {
                    "api_base_url": "https://swe-b.example",
                    "internal_api_base_url": "https://swe-b-internal.example",
                    "source_ids": ["b1", "b2"],
                },
            ],
        ),
    )
    gateway, internal = "https://gateway.example", "https://internal.example"
    monkeypatch.setenv("SCHEDULER_SWE_API_BASE_URL", gateway)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "execution_id": "run",
                "recheck_id": "recheck",
                "provider_id": "provider",
                "model": "model",
                "batch_scope": "followed_batch",
                "status": "sent",
            },
        )

    async def store_call(*args):
        return True

    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )
    caller = CallerIdentity("tenant", source, "default", "tenant")
    operation = {"operation_id": "op", "job_id": "job"}
    row = {
        **operation,
        "source_id": source,
        "route_tenant_id": "tenant",
        "route_agent_id": "default",
        "tenant_id": "tenant",
        "agent_id": "default",
        "creator_user_id": "tenant",
    }
    run = {
        **row,
        "execution_id": "run",
        "recheck_id": "recheck",
        "dispatch_owner": "dispatch",
    }
    async with httpx.AsyncClient() as http:
        management = ManagedSweClient(gateway, http=http)
        await management.prepare(operation, caller)
        await management.cleanup(operation, caller)
        await management.migration_snapshot(caller, "job")
        await management.migration_stop_legacy(operation, caller)
        execution = ManagedSweExecutionClient(gateway, http=http)
        await execution.execute(run, caller)
        await execution.recover(run, caller)
        await execution.recheck("execute", run, caller)
        await execution.recheck("recover", run, caller)
        await resolve_agent_model(row, {})
        system = SystemSweClient(gateway, http=http)
        await system.get_config(row)
        await system.execute(row, run)
        worker = BatchReportWorker(
            SimpleNamespace(
                freeze=store_call,
                start_delivery=store_call,
                finish_delivery=store_call,
            ),
            http,
            internal,
        )
        monkeypatch.setattr(
            "scheduler.app.services.cron.batch_status_report.runtime.render_parts",
            lambda *args: [],
        )
        await worker.collect(
            {"source_id": source, "business_date": "2026-11-24"},
        )
        await worker.deliver({"source_id": source})
        callback = SweCronCallbackClient(base_url=gateway)
        for recorded_domain in ("https://legacy.example/", ""):
            await callback.dispatch_job(
                tenant_id="tenant",
                source_id=source,
                agent_id="default",
                job_id="job",
                dispatch_intent_id=1,
                dispatch_batch_id="batch",
                dispatch_attempt=1,
                swe_server_domain=recorded_domain,
            )
        await SystemSweClient(internal, http=http).get_batch_retry_overrides()

    expected_hosts = (
        [expected_api_host or "gateway.example"] * 11
        + [expected_internal_host or "internal.example"] * 2
        + [
            "legacy.example",
            expected_api_host or "gateway.example",
            "internal.example",
        ]
    )
    assert [req.url.host for req in requests] == expected_hosts
    assert all(
        req.url.path.startswith("/api/internal/cron/") for req in requests
    )
    assert requests[8].headers["X-Source-Id"] == source
    for legacy in requests[-3:-1]:
        assert legacy.url.path.endswith("/callback")
        assert json.loads(legacy.content)["source_id"] == source
        assert "X-Source-Id" not in legacy.headers
        assert "X-Tenant-Id" not in legacy.headers
