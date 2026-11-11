# -*- coding: utf-8 -*-
"""系统管理客户端沿用平台协议，并用Header调用SWE纯配置接口。"""

import base64
import json

import httpx
import pytest

from scheduler.app.services.cron.system.platform import SystemPlatformClient
from scheduler.app.services.cron.system.swe_client import SystemSweClient
from tests.unit.scheduler.test_system_cron_management import (
    ADMIN,
    CONFIG,
    system_setup,
)


@pytest.mark.asyncio
async def test_system_platform_body_is_independent_and_keeps_original_timer_rule(
    system_setup,
):
    _, store, _ = system_setup
    row = await store.refresh(ADMIN, "cleanup", "save")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "content": "123"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        platform = SystemPlatformClient(
            {
                "base_url": "http://platform.test",
                "callback_base": "http://scheduler.test",
            },
            http=http,
        )
        assert await platform.register(row, CONFIG) == "123"
        await platform.update("123", row, {**CONFIG, "cron": "0 9 LW * *"})
    added = json.loads(requests[0].content)
    updated = json.loads(requests[1].content)
    assert (
        added["jobAddress"]
        == "http://scheduler.test/api/scheduler/cron/system/callback"
    )
    assert "系统任务" in added["jobDesc"] and "bank" in added["jobDesc"]
    body = json.loads(base64.urlsafe_b64decode(added["jobParam"]))
    assert body["callback_protocol"] == "scheduler_system_v1"
    assert body["task_type"] == "task_session_cleanup"
    assert (
        body["job_id"] == row["job_id"]
        and body["operation_id"] == row["operation_id"]
    )
    assert body["tenant_id"] == "alice" and body["source_id"] == "bank"
    assert updated["jobCron"] == "0 0 9 LW * ?"
    assert all("query" not in request.url.path for request in requests)


@pytest.mark.asyncio
async def test_system_config_client_passes_identity_headers_and_only_operation_reference(
    system_setup,
):
    _, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=CONFIG)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        result = await SystemSweClient(
            "http://swe.test/api",
            http=http,
        ).get_config(row)
    request = requests[0]
    assert result == CONFIG
    assert (
        request.method == "GET"
        and request.url.path == "/api/internal/cron/system/config"
    )
    assert (
        request.headers["x-user-id"]
        == request.headers["x-tenant-id"]
        == "alice"
    )
    assert (
        request.headers["x-source-id"] == "bank"
        and request.headers["x-agent-id"] == "default"
    )
    assert request.url.params["job_id"] == row["job_id"]
    assert request.url.params["operation_id"] == row["operation_id"]
    assert not request.content


@pytest.mark.asyncio
async def test_execute_request_only_contains_registered_id_and_dispatch_fence(
    system_setup,
):
    _, store, _ = system_setup
    job = await store.refresh(ADMIN, "heartbeat", "save")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            202,
            json={"execution_id": "run", "accepted": True},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        await SystemSweClient("http://swe.test", http=http).execute(
            job,
            {"execution_id": "run", "dispatch_owner": "dispatch"},
        )
    body = json.loads(requests[0].content)
    assert body == {
        "execution_id": "run",
        "job_id": job["job_id"],
        "dispatch_token": "dispatch",
    }
    assert requests[0].url.path == "/api/internal/cron/system/execute"
