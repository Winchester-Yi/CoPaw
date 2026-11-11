# -*- coding: utf-8 -*-
"""SWE只提交刷新意图，不能向Scheduler提交过时业务配置或虚构角色。"""

import json

from httpx import AsyncClient, MockTransport, Response
import pytest

from swe.app.crons.system_bridge import RefreshScope, SystemRefreshClient


@pytest.mark.asyncio
async def test_workspace_internal_refresh_never_forges_manager_or_sends_config():
    seen = []

    async def reply(request):
        seen.append(request)
        return Response(202, json={"items": []})

    async with AsyncClient(transport=MockTransport(reply)) as http:
        result = await SystemRefreshClient(
            "http://scheduler/api",
            http=http,
        ).workspace(
            RefreshScope("alice", "bank", "default"),
            request_id="startup",
        )
    assert result["submission_status"] == "accepted"
    request = seen[0]
    assert (
        request.url.path
        == "/api/scheduler/internal/cron/system/workspaces/refresh"
    )
    assert "X-User-Role" not in request.headers
    assert json.loads(request.content) == {"request_id": "startup"}
    assert request.headers["X-Tenant-Id"] == "alice"


@pytest.mark.asyncio
async def test_agent_refresh_uses_management_intent_not_direct_platform():
    seen = []

    async def reply(request):
        seen.append(request)
        return Response(202, json={"job_id": "job", "sync_status": "pending"})

    async with AsyncClient(transport=MockTransport(reply)) as http:
        result = await SystemRefreshClient(
            "http://scheduler/api",
            http=http,
        ).refresh(
            RefreshScope("alice", "bank", "default"),
            "heartbeat",
            request_id="save",
        )
    assert result["submission_status"] == "accepted"
    assert seen[0].url.path == "/api/scheduler/cron/system/jobs/refresh"
    assert json.loads(seen[0].content) == {
        "request_id": "save",
        "task_type": "heartbeat",
    }


@pytest.mark.asyncio
async def test_submission_failure_is_visible_and_never_claims_config_rollback():
    async with AsyncClient(
        transport=MockTransport(
            lambda request: Response(503, text="private server error"),
        ),
    ) as http:
        result = await SystemRefreshClient(
            "http://scheduler/api",
            http=http,
        ).refresh(
            RefreshScope("alice", "bank", "default"),
            "dream",
            request_id="save-1",
        )
    assert result == {
        "submission_status": "failed",
        "error_code": "system_sync_not_submitted",
        "request_id": "save-1",
    }
    assert "private" not in str(result)
