# -*- coding: utf-8 -*-
"""旧文件任务的会话清理适配器；全量切换后可单独移除。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ...config.context import resolve_runtime_identity
from ..source_system_config.runtime import (
    resolve_cron_task_session_cleanup_config,
)
from .session_cleanup import _prune_task_session_state, task_session_write_lock

logger = logging.getLogger(__name__)


def apply_cleanup_meta(jobs_file, job_id, snapshot):
    for index, job in enumerate(jobs_file.jobs):
        if job.id != job_id:
            continue
        meta = dict(job.meta or {})
        meta["task_has_scheduled_result"] = snapshot.has_result
        meta["task_last_scheduled_preview"] = (
            snapshot.latest_preview if snapshot.has_result else ""
        )
        meta["task_last_scheduled_run_at"] = (
            snapshot.latest_run_at if snapshot.has_result else None
        )
        meta["task_unread_execution_count"] = 0
        jobs_file.jobs[index] = job.model_copy(update={"meta": meta})
        return True, None
    return False, None


async def resolve_cleanup_policy(service, runtime_tenant_id):
    _, source_id, _ = resolve_runtime_identity(runtime_tenant_id)
    if service is None or not source_id:
        return resolve_cron_task_session_cleanup_config(None)
    try:
        config = await service.resolve_config(source_id)
    except Exception:
        logger.warning(
            "Failed to resolve task session cleanup source config",
            exc_info=True,
        )
        return resolve_cron_task_session_cleanup_config(None)
    return resolve_cron_task_session_cleanup_config(config)


async def cleanup_legacy_workspace_sessions(workspace, *, now=None):
    from .repo.json_repo import JsonJobRepository

    policy = await resolve_cleanup_policy(
        getattr(workspace, "_source_system_config_service", None),
        workspace.tenant_id,
    )
    cleaner = LegacyTaskSessionCleaner(
        JsonJobRepository(workspace.workspace_dir / "jobs.json"),
        getattr(workspace.runner, "session", None),
        policy,
    )
    return await cleaner.run(now=now)


class LegacyTaskSessionCleaner:
    def __init__(self, repo, session, config, *, lock=None):
        self.repo = repo
        self.session = session
        self.config = config
        self.lock = lock if lock is not None else asyncio.Lock()

    async def run(self, *, now=None):
        result = {
            "enabled": self.config.enabled,
            "retention_days": self.config.retention_days,
            "sessions_seen": 0,
            "sessions_cleaned": 0,
            "sessions_skipped_locked": 0,
            "runs_removed": 0,
            "messages_removed": 0,
        }
        if not self.config.enabled or self.session is None:
            return result
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        cutoff = observed.astimezone(timezone.utc) - timedelta(
            days=self.config.retention_days,
        )
        for job in await self.repo.list_jobs():
            await self._clean_job(job, cutoff, result)
        logger.info("Task session cleanup result: %s", result)
        return result

    async def _clean_job(self, job, cutoff, result):
        meta = job.meta or {}
        session_id, user_id = meta.get("task_session_id"), meta.get(
            "creator_user_id",
        )
        if not session_id or not user_id:
            return
        result["sessions_seen"] += 1
        try:
            snapshot = await self._prune(str(session_id), str(user_id), cutoff)
        except TimeoutError:
            result["sessions_skipped_locked"] += 1
            logger.info(
                "Task session cleanup skipped locked session: %s",
                session_id,
            )
            return
        if snapshot is None or not snapshot.changed:
            return
        result["sessions_cleaned"] += 1
        result["runs_removed"] += snapshot.removed_runs
        result["messages_removed"] += snapshot.removed_messages
        await self._persist(job.id, snapshot)

    async def _prune(self, session_id, user_id, cutoff):
        mutation = getattr(self.session, "mutate_session_state", None)
        if callable(mutation):
            snapshot = None

            def mutate(state):
                nonlocal snapshot
                snapshot = _prune_task_session_state(state, cutoff)
                return snapshot.state if snapshot.changed else state

            await mutation(
                session_id=session_id,
                user_id=user_id,
                mutator=mutate,
                create_if_not_exist=True,
                timeout_seconds=5.0,
            )
            return snapshot
        async with task_session_write_lock(self.session, session_id, user_id):
            state = await self.session.get_session_state_dict(
                session_id,
                user_id,
                allow_not_exist=True,
            )
            snapshot = _prune_task_session_state(state, cutoff)
            if snapshot.changed:
                await self.session.save_merged_state(
                    session_id=session_id,
                    user_id=user_id,
                    state=snapshot.state,
                )
            return snapshot

    async def _persist(self, job_id, snapshot):
        def mutate(jobs_file):
            return apply_cleanup_meta(jobs_file, job_id, snapshot)

        async with self.lock:
            mutation = getattr(self.repo, "mutate_jobs_file", None)
            if callable(mutation):
                await mutation(mutate)
                return
            jobs_file = await self.repo.load()
            changed, _ = mutate(jobs_file)
            if changed:
                await self.repo.save(jobs_file)
