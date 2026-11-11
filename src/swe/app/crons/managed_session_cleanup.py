# -*- coding: utf-8 -*-
"""按新版DB绑定清理会话文件；不补写jobs.json、不删除SQL执行历史。"""

from datetime import datetime, timedelta, timezone

KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
WHERE = " AND ".join(
    f"{key}=%s AND {key} COLLATE utf8mb4_bin=%s" for key in KEYS
)
COUNTERS = (
    "sessions_seen",
    "sessions_cleaned",
    "sessions_skipped_locked",
    "runs_removed",
    "messages_removed",
)
PAGE_SIZE = 200


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


class ManagedTaskSessionCleaner:
    """复用原裁剪算法和会话原子锁，状态写入用版本条件保护新执行。"""

    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _page(self, scope, after):
        joins = " AND ".join(
            f"d.{key}=s.{key} AND d.{key} COLLATE utf8mb4_bin=s.{key}"
            for key in KEYS
        )
        clauses = " AND ".join(
            f"d.{key}=%s AND d.{key} COLLATE utf8mb4_bin=%s"
            for key in KEYS[:3]
        )
        params = tuple(value for value in scope for _ in range(2))
        return await self.db.fetch_all(
            "SELECT d.tenant_id,d.source_id,d.agent_id,d.job_id,d.creator_user_id,"
            "s.job_id AS state_job_id,s.task_session_id,s.state_version,s.last_scheduled_run_at "
            f"FROM swe_cron_jobs_v2 d LEFT JOIN swe_cron_job_states s ON {joins} "
            f"WHERE {clauses} "
            "AND d.management_owner='scheduler' AND d.deleted_at IS NULL AND d.job_id COLLATE utf8mb4_bin>%s "
            "ORDER BY d.job_id COLLATE utf8mb4_bin LIMIT %s",
            (*params, after, PAGE_SIZE),
        )

    async def cleanup(self, workspace, tenant_id, source_id, policy):
        """仅原来源维护范围内的当前Agent，稳定分段读取所有新版绑定。"""
        result = dict.fromkeys(COUNTERS, 0)
        if not policy.enabled:
            return result
        session = workspace.runner.session
        scope = (tenant_id, source_id, workspace.agent_id)
        cutoff = _utc(self.clock()) - timedelta(days=policy.retention_days)
        after = ""
        while True:
            rows = await self._page(scope, after)
            for row in rows:
                if (
                    tuple(row[key] for key in KEYS[:3]) != scope
                    or row["state_job_id"] != row["job_id"]
                ):
                    raise RuntimeError("cron_state_record_missing")
                await self._clean(session, row, cutoff, result)
            if len(rows) < PAGE_SIZE:
                return result
            after = rows[-1]["job_id"]

    async def _clean(self, session, row, cutoff, result):
        # 仅复用纯裁剪算法，导入不会创建CronManager或读取旧任务仓库。
        from .session_cleanup import _prune_task_session_state

        if not row["task_session_id"] or not row["creator_user_id"]:
            return
        result["sessions_seen"] += 1
        snapshot = None

        def mutate(state):
            nonlocal snapshot
            snapshot = _prune_task_session_state(state, cutoff)
            return snapshot.state if snapshot.changed else state

        try:
            await session.mutate_session_state(
                session_id=row["task_session_id"],
                user_id=row["creator_user_id"],
                mutator=mutate,
                create_if_not_exist=True,
                timeout_seconds=5.0,
            )
        except TimeoutError:
            result["sessions_skipped_locked"] += 1
            return
        if snapshot is None or not snapshot.changed:
            return
        result["sessions_cleaned"] += 1
        result["runs_removed"] += snapshot.removed_runs
        result["messages_removed"] += snapshot.removed_messages
        await self._clear_expired_state(row, snapshot, cutoff)

    async def _clear_expired_state(self, row, snapshot, cutoff):
        """只清除已无保留结果的过期展示；近期结果/未读以DB为准，不降级新事实。"""
        if snapshot.has_result:
            return
        latest = row["last_scheduled_run_at"]
        if latest is not None and _utc(latest) >= cutoff:
            return
        key = tuple(row[name] for name in KEYS)
        params = tuple(value for value in key for _ in range(2))
        joins = " AND ".join(
            f"d.{name}=%s AND d.{name} COLLATE utf8mb4_bin=%s" for name in KEYS
        )
        await self.db.execute(
            "UPDATE swe_cron_job_states SET has_scheduled_result=0,latest_scheduled_preview='',"
            "last_scheduled_run_at=NULL,unread_execution_count=0,state_version=state_version+1,updated_at=%s "
            f"WHERE {WHERE} AND state_version=%s AND task_session_id=%s "
            "AND (last_scheduled_run_at IS NULL OR last_scheduled_run_at<%s) "
            f"AND EXISTS (SELECT 1 FROM swe_cron_jobs_v2 d WHERE {joins} "
            "AND d.management_owner='scheduler' AND d.deleted_at IS NULL)",
            (
                _utc(self.clock()).replace(tzinfo=None),
                *params,
                row["state_version"],
                row["task_session_id"],
                cutoff.replace(tzinfo=None),
                *params,
            ),
        )
