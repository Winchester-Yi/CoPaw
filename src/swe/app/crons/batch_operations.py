"""Manager-facing batch priority settings and Scheduler operations proxy."""

from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ...config.context import is_valid_identity_value
from ...utils.bbk import is_primary_bbk_id
from .api import (
    _claim_dispatch_mode_operation,
    _get_source_job_or_404,
    _is_broadcast_child,
    get_cron_manager,
)
from .manager import CronManager
from .models import CronJobSpec, JobsFile
from .monitor_sync_client import get_scheduler_api_url
from .batch_run_state_client import request_run_state
from .scheduler_adapter import NoopSchedulerAdapter, SchedulerAdapter

router = APIRouter(prefix="/cron", tags=["cron"])


def manager_identity(request: Request) -> tuple[str, str]:
    if request.headers.get("X-User-Role", "").strip().lower() not in {
        "manager",
        "admin",
    }:
        raise HTTPException(403, "Manager role required")
    source = request.headers.get("X-Source-Id", "").strip()
    actor = request.headers.get("X-User-Id", "").strip()
    if not is_valid_identity_value(source) or not is_valid_identity_value(
        actor
    ):
        raise HTTPException(400, "Source and actor required")
    return source, actor


class BatchPriority(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: list[str] = Field(default_factory=list, max_length=500)
    branch_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("user_ids", "branch_ids")
    @classmethod
    def validate_ids(cls, values):
        values = [v.strip() for v in values]
        if any(not is_valid_identity_value(v) for v in values):
            raise ValueError("优先名单包含无效标识")
        if len(set(values)) != len(values):
            raise ValueError("优先名单不能重复")
        return values

    @field_validator("branch_ids")
    @classmethod
    def validate_branches(cls, values):
        if any(not is_primary_bbk_id(v) for v in values):
            raise ValueError("请选择有效的一级分行")
        return values


def patch_priority(
    jobs: JobsFile,
    job_id: str,
    source: str,
    priority: BatchPriority,
) -> tuple[bool, CronJobSpec]:
    for index, job in enumerate(jobs.jobs):
        if job.id != job_id:
            continue
        if job.source_id != source or _is_broadcast_child(job):
            raise HTTPException(
                403, "Only the source parent can configure priority"
            )
        updated = job.model_copy(
            update={
                "meta": {
                    **(job.meta or {}),
                    "batch_dispatch_priority": priority.model_dump(),
                }
            }
        )
        jobs.jobs[index] = updated
        return job != updated, updated
    raise HTTPException(404, "job not found")


@router.put(
    "/jobs/{job_id}/batch-dispatch/priority",
    response_model=CronJobSpec,
    dependencies=[Depends(manager_identity)],
)
async def save_priority(
    request: Request,
    job_id: str,
    body: BatchPriority,
    mgr: CronManager = Depends(get_cron_manager),
):
    source, _ = manager_identity(request)
    job = await _get_source_job_or_404(mgr, job_id)
    if job.source_id != source or _is_broadcast_child(job):
        raise HTTPException(
            403, "Only the source parent can configure priority"
        )
    store, snapshot = await _claim_dispatch_mode_operation(request, job)
    try:
        # Patch only metadata against the latest definition under the job lock.
        async with mgr._lock:
            _, updated, _ = await mgr._mutate_jobs_file_locked(
                lambda jobs: patch_priority(jobs, job_id, source, body),
            )
        # The ordinary sync is fire-and-forget. Saving priority must wait for
        # the definition used by Scheduler to acknowledge the new metadata.
        sync = mgr._monitor_sync_client
        if sync is None or not sync._enabled:
            raise HTTPException(503, "优先策略已本地保存，但定义同步未启用")
        try:
            client = await sync._get_client()
            response = await client.post(
                "/monitor/sync/job",
                json=sync._build_job_sync_data(
                    updated, agent_id=mgr._agent_id
                ),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                502, "优先策略已本地保存，但定义同步失败，请重试保存"
            ) from exc
        await store.finish_task(snapshot.task_id)
        return updated
    except Exception as exc:
        await store.record_task_failed(snapshot.task_id, str(exc))
        raise


async def _proxy(request: Request, batch: str, action: str, body=None):
    source, actor = manager_identity(request)
    url = (
        f"{get_scheduler_api_url().rstrip('/')}"
        "/scheduler/cron/dispatch/batches/"
        f"{quote(batch, safe='')}/{action}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                "POST" if body is not None else "GET",
                url,
                json=body,
                params=dict(request.query_params),
                headers={
                    "X-Source-Id": source,
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
    except httpx.RequestError as exc:
        raise HTTPException(
            502, "Scheduler 暂不可用，请刷新确认入队结果"
        ) from exc


@router.get("/dispatch/batches/{batch_id}/failures")
async def get_failures(request: Request, batch_id: str):
    return await _proxy(request, batch_id, "failures")


@router.post("/dispatch/batches/{batch_id}/retry")
async def retry_failures(request: Request, batch_id: str, body: dict):
    return await _proxy(request, batch_id, "retry", body)


class BatchRunStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool
    expected_version: int = Field(ge=1)


async def _run_state_parent(request: Request, job_id: str, mgr: CronManager):
    source, actor = manager_identity(request)
    job = await _get_source_job_or_404(mgr, job_id)
    if job.source_id != source:
        raise HTTPException(403, "Source parent required")
    value = (job.meta or {}).get("broadcast_dispatch_intents_enabled")
    enabled = (
        value is True or value == 1 or str(value).lower() in {"true", "1"}
    )
    if _is_broadcast_child(job) or not enabled:
        raise HTTPException(400, "仅批调度父任务可操作整批运行状态")
    if not job.tenant_id:
        raise HTTPException(409, "父任务租户信息缺失，请先修复任务定义")
    return job, actor


@router.get(
    "/jobs/{job_id}/batch-dispatch/run-state",
    dependencies=[Depends(manager_identity)],
)
async def get_batch_run_state(
    request: Request, job_id: str, mgr: CronManager = Depends(get_cron_manager)
):
    job, actor = await _run_state_parent(request, job_id, mgr)
    return await request_run_state(job, actor)


@router.put(
    "/jobs/{job_id}/batch-dispatch/run-state",
    dependencies=[Depends(manager_identity)],
)
async def save_batch_run_state(
    request: Request,
    job_id: str,
    body: BatchRunStateUpdate,
    mgr: CronManager = Depends(get_cron_manager),
):
    job, actor = await _run_state_parent(request, job_id, mgr)
    store, snapshot = await _claim_dispatch_mode_operation(request, job)
    try:
        if not body.paused:
            await _ensure_batch_wakeup(mgr, job)
        result = await request_run_state(
            job, actor, method="PUT", body=body.model_dump()
        )
        if body.paused:
            # Gate existing queues before stopping future external callbacks.
            await _pause_batch_wakeup(mgr, job)
        await store.finish_task(snapshot.task_id)
        return result
    except Exception as exc:
        await store.record_task_failed(snapshot.task_id, str(exc))
        raise


def _batch_wakeup_target(
    mgr: CronManager, job: CronJobSpec
) -> tuple[SchedulerAdapter, str]:
    external_id = str(
        (job.meta or {}).get("batch_dispatch_external_job_id") or ""
    ).strip()
    adapter = mgr._scheduler_adapter
    if (
        not external_id
        or adapter is None
        or isinstance(adapter, NoopSchedulerAdapter)
    ):
        raise HTTPException(409, "批调度定时器未就绪，请重新保存批调度配置")
    normal_id = str((job.meta or {}).get("external_job_id") or "").strip()
    if external_id == normal_id:
        raise HTTPException(409, "批调度与普通定时器 ID 冲突，请修复配置")
    return adapter, external_id


async def _ensure_batch_wakeup(mgr: CronManager, job: CronJobSpec) -> None:
    adapter, external_id = _batch_wakeup_target(mgr, job)
    try:
        # Repair old batch timers only; leave the ordinary timer unchanged.
        await adapter.resume_job(external_id)
    except Exception as exc:
        raise HTTPException(
            502, "批调度定时器恢复失败，未更新整批运行状态，请重试"
        ) from exc


async def _pause_batch_wakeup(mgr: CronManager, job: CronJobSpec) -> None:
    """Synchronize only after the durable internal pause has succeeded."""
    try:
        adapter, external_id = _batch_wakeup_target(mgr, job)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code, f"内部批调度已暂停；{exc.detail}"
        ) from exc
    try:
        await adapter.pause_job(external_id)
    except Exception as exc:
        raise HTTPException(
            502,
            "内部批调度已暂停，但外部批调度定时器暂停失败；"
            "请刷新状态后重试同步外部暂停",
        ) from exc
