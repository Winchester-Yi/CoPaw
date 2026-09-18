# -*- coding: utf-8 -*-
"""List existing jobs without bootstrapping tenants or starting runtime."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from swe.config import utils as config_utils
from swe.config.config import AgentsConfig
from swe.config.context import resolve_storage_tenant_id
from swe.runtime_workers import run_runtime_state_work

from ..crons.cron_utils import compute_next_run_times
from ..crons.models import CronJobListItem, CronJobSpec, CronJobState
from ..crons.repo.json_repo import JsonJobRepository
from ..crons.task_view import build_cron_task_view

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/external/cron", tags=["external-cron"])
_STORAGE_UNAVAILABLE = "Persisted job storage unavailable"


def _checked_path(path: Path, root: Path) -> Path:
    """Reject persisted paths and symlinks that leave the selected scope."""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="Agent workspace is unavailable",
        ) from exc
    return resolved


def _resolve_jobs_path(
    storage_id: str,
    agent_id: str | None,
) -> tuple[Path, str, str]:
    """Read profile references without config repair or tenant initialization."""
    base = Path(config_utils.WORKING_DIR).expanduser().resolve()
    tenant_dir = base / storage_id
    _checked_path(tenant_dir, tenant_dir)
    config_path = _checked_path(tenant_dir / "config.json", tenant_dir)
    display_timezone = "UTC"
    try:
        contents = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if agent_id not in (None, "default"):
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{agent_id}' not found",
            )
        selected_id = "default"
        workspace = tenant_dir / "workspaces" / "default"
    else:
        # load_config() may repair/back up files; normalize only in memory.
        data = config_utils._normalize_working_dir_bound_paths(  # pylint: disable=protected-access
            json.loads(contents),
        )
        if not isinstance(data, dict):
            raise ValueError("Tenant config must be an object")
        agents = AgentsConfig.model_validate(data.get("agents", {}))
        selected_id = agent_id or agents.active_agent or "default"
        profile = agents.profiles.get(selected_id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{selected_id}' not found",
            )
        if not profile.enabled:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{selected_id}' is disabled",
            )
        workspace = Path(profile.workspace_dir)
        display_timezone = data.get("user_timezone") or "UTC"
        if not isinstance(display_timezone, str):
            raise ValueError("User timezone must be a string")
    workspace = _checked_path(workspace, tenant_dir / "workspaces")
    jobs_path = _checked_path(workspace / "jobs.json", workspace)
    return jobs_path, selected_id, display_timezone


def _build_job_list(
    jobs: list[CronJobSpec],
    states: dict[str, CronJobState],
    user_id: str | None,
    display_timezone: str,
) -> list[CronJobListItem]:
    """Project persisted jobs and copied state without repairing bindings."""
    items = []
    for job in jobs:
        state = states.get(job.id, CronJobState())
        try:
            next_runs = compute_next_run_times(
                job.schedule.cron,
                job.schedule.timezone or display_timezone or "UTC",
                count=3,
            )
            state.next_run_times = next_runs
            state.next_run_at = next_runs[0] if next_runs else None
        except Exception:
            # Match the original list: retain known state if display math fails.
            logger.debug(
                "Failed to compute next_run_at for job %s",
                job.id,
                exc_info=True,
            )
        items.append(
            CronJobListItem(
                **job.model_dump(mode="json"),
                state=state,
                task=build_cron_task_view(job, state, user_id),
            )
        )
    return items


@router.get("/jobs", response_model=list[CronJobListItem])
async def list_external_jobs(request: Request) -> list[CronJobListItem]:
    """Use the original jobs list contract with no bootstrap or write-on-read.

    X-Tenant-Id and X-Source-Id follow the existing identity middleware.
    X-User-Id is optional and affects task visibility, not list filtering.
    X-Agent-Id optionally selects an existing enabled Agent profile.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    source_id = getattr(request.state, "source_id", None)
    if not tenant_id or not source_id:
        raise HTTPException(
            status_code=400,
            detail="X-Tenant-Id and X-Source-Id are required",
        )
    storage_id = resolve_storage_tenant_id(
        tenant_id,
        source_id,
        scope_id=getattr(request.state, "scope_id", None),
    )
    user_id = getattr(request.state, "user_id", None) or request.headers.get(
        "X-User-Id",
    )
    try:
        jobs_path, selected_id, display_timezone = (
            await run_runtime_state_work(
                _resolve_jobs_path,
                storage_id,
                request.headers.get("X-Agent-Id") or None,
            )
        )
        jobs = await JsonJobRepository(jobs_path).list_jobs()
        states = {}
        manager = getattr(request.app.state, "multi_agent_manager", None)
        if manager is not None:
            cache_key = manager._cache_key(  # pylint: disable=protected-access
                selected_id,
                storage_id,
            )
            workspace = manager.agents.get(cache_key)
            cron_manager = getattr(workspace, "cron_manager", None)
            if cron_manager is not None:
                states = {
                    job.id: cron_manager.get_state(job.id).model_copy(
                        deep=True
                    )
                    for job in jobs
                }
                display_timezone = (
                    cron_manager._timezone
                )  # pylint: disable=protected-access
        return await run_runtime_state_work(
            _build_job_list,
            jobs,
            states,
            user_id,
            display_timezone,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning(
            "external_job_storage_unavailable: %s", type(exc).__name__
        )
        raise HTTPException(
            status_code=503,
            detail=_STORAGE_UNAVAILABLE,
        ) from exc
