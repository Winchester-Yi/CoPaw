"""HTTP-only access to Scheduler-owned batch controls."""

from urllib.parse import quote

import httpx
from fastapi import HTTPException

from .monitor_sync_client import get_scheduler_api_url


async def request_run_state(
    job, actor: str, *, method: str = "GET", body=None
):
    url = (
        f"{get_scheduler_api_url().rstrip('/')}"
        "/scheduler/cron/dispatch/parents/"
        f"{quote(job.id, safe='')}/run-state"
    )
    if method == "POST":
        url += "/initialize"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method,
                url,
                params={"tenant_id": job.tenant_id},
                json=body,
                headers={
                    "X-Source-Id": job.source_id,
                    "X-User-Id": actor,
                },
            )
        if response.is_error:
            try:
                detail = response.json().get(
                    "detail", "Scheduler request failed"
                )
            except ValueError:
                detail = "Scheduler request failed"
            raise HTTPException(response.status_code, detail)
        return response.json()
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(
            502, "Scheduler 暂不可用，请刷新确认批调度状态"
        ) from exc


async def initialize_run_state(job, sync, agent_id: str) -> None:
    """Publish the parent definition before compatibility initialization."""
    if sync is None or not sync._enabled:
        raise RuntimeError("批调度定义同步未启用，无法初始化独立运行状态")
    try:
        client = await sync._get_client()
        response = await client.post(
            "/monitor/sync/job",
            json=sync._build_job_sync_data(job, agent_id=agent_id),
        )
        response.raise_for_status()
        await request_run_state(job, "batch-configuration", method="POST")
    except (httpx.HTTPError, HTTPException) as exc:
        raise RuntimeError(
            "批调度配置已保存，但运行状态初始化失败，请重试配置"
        ) from exc
