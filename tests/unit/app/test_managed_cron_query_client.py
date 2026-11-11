# -*- coding: utf-8 -*-
"""业务只读客户端完整翻页、约束身份，不触碰旧管理器。"""

import httpx
import pytest


@pytest.mark.asyncio
async def test_managed_reader_pages_and_preserves_bound_ids():
    from swe.app.crons.managed_query_client import ManagedCronQueryClient

    calls = []

    async def serve(request):
        calls.append(request)
        if request.url.params.get("cursor"):
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "original-id",
                        "name": "原任务",
                        "tenant_id": "alice",
                        "source_id": "bank",
                        "agent_id": "research",
                        "management_owner": "scheduler",
                        "meta": {
                            "task_session_id": "original-session",
                            "task_chat_id": "original-chat",
                        },
                    },
                ],
                "next_cursor": "page-2",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        result = await ManagedCronQueryClient(
            "http://scheduler/api",
            http=http,
        ).list_jobs("alice", "bank", "research")
    assert len(calls) == 2
    assert all("/scheduler/cron/jobs" in request.url.path for request in calls)
    assert calls[0].headers["X-User-Id"] == "alice"
    assert result[0]["id"] == "original-id"
    assert result[0]["meta"]["task_session_id"] == "original-session"


@pytest.mark.asyncio
async def test_managed_reader_rejects_foreign_identity():
    from swe.app.crons.managed_query_client import ManagedCronQueryClient

    async def serve(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "tenant_id": "bob",
                        "source_id": "bank",
                        "agent_id": "research",
                        "management_owner": "scheduler",
                    },
                ],
                "next_cursor": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        with pytest.raises(ValueError, match="identity"):
            await ManagedCronQueryClient(
                "http://scheduler/api",
                http=http,
            ).list_jobs("alice", "bank", "research")


@pytest.mark.asyncio
async def test_progress_uses_scheduler_day_and_never_parses_cron_in_swe():
    from datetime import datetime
    from swe.app.crons.managed_query_client import ManagedCronQueryClient

    async def serve(request):
        assert request.url.path == "/api/scheduler/cron/progress"
        assert request.url.params["date"] == "2026-10-10"
        return httpx.Response(
            200,
            json={
                "date": "2026-10-10",
                "timezone": "Asia/Shanghai",
                "next_cursor": None,
                "items": [
                    {
                        "job_id": "id",
                        "tenant_id": "alice",
                        "source_id": "bank",
                        "agent_id": "default",
                        "management_owner": "scheduler",
                        "task_name": "任务",
                        "status": "completed",
                        "status_text": "已完成",
                        "scheduled_time": "2026-10-10T09:00:00+08:00",
                        "time_info": "09:00已完成",
                        "meta": {"task_chat_id": "original-chat"},
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        items = await ManagedCronQueryClient(
            "http://scheduler/api",
            http=http,
        ).query_progress(
            "alice",
            "bank",
            "default",
            datetime.fromisoformat("2026-10-09T18:00:00+00:00"),
        )
    assert items[0]["meta"]["task_chat_id"] == "original-chat"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"items": [], "next_cursor": "same"},
        {"items": []},
        {"items": None, "next_cursor": None},
    ],
)
async def test_invalid_pagination_never_falls_back_to_old_tasks(payload):
    from swe.app.crons.managed_query_client import ManagedCronQueryClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload),
        ),
    ) as http:
        with pytest.raises(ValueError):
            await ManagedCronQueryClient(
                "http://scheduler/api",
                http=http,
            ).list_jobs("alice", "bank")
