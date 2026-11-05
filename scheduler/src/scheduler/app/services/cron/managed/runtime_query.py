# -*- coding: utf-8 -*-
"""一次标量 SELECT 读取当前工作区任务状态；不加载定义 JSON。"""

from .contracts import ManagedCronUnavailable
from .repository import ManagedCronRepository, _JOIN
from .runtime_contracts import RuntimeStatusRequest, RuntimeStatusResponse
from .views import _next_run_times, task_view

_DEFINITION_COLUMNS = (
    "job_id",
    "definition_version",
    "enabled",
    "creator_user_id",
    "cron_expr",
    "timezone",
    "run_start_date",
    "run_end_date",
)
_STATE_COLUMNS = (
    "job_id",
    "task_chat_id",
    "task_session_id",
    "dispatch_state",
    "pause_reason",
    "auto_paused_at",
    "last_status",
    "last_run_at",
    "last_error",
    "has_scheduled_result",
    "latest_scheduled_preview",
    "last_scheduled_run_at",
    "unread_execution_count",
    "management_status",
    "management_version",
    "state_version",
)
_SELECT = (
    "SELECT "
    + ", ".join(f"d.`{name}`" for name in _DEFINITION_COLUMNS)
    + ", "
    + ", ".join(f"s.`{name}` AS `state_{name}`" for name in _STATE_COLUMNS)
    + f" FROM swe_cron_jobs_v2 d LEFT JOIN swe_cron_job_states s ON {_JOIN}"
)


def _item(row, actor_id):
    base = {
        "id": row["job_id"],
        "definition_version": row["definition_version"],
    }
    if row["state_job_id"] is None:
        return {**base, "status": "state_missing"}
    times = _next_run_times(row)
    error = row["state_last_error"]
    task = task_view(row, actor_id)
    preview = task["latest_scheduled_preview"]
    task.update(
        latest_scheduled_preview=preview[:256],
        latest_scheduled_preview_truncated=len(preview) > 256,
    )
    return {
        **base,
        "status": "ok",
        "enabled": bool(row["enabled"]),
        **{
            key: row[f"state_{key}"]
            for key in (
                "management_version",
                "state_version",
                "management_status",
                "dispatch_state",
            )
        },
        "state": {
            "last_status": row["state_last_status"],
            "last_run_at": row["state_last_run_at"],
            "last_error": error[:1024] if error is not None else None,
            "last_error_truncated": error is not None and len(error) > 1024,
            "next_run_at": times[0] if times else None,
            "next_run_times": times,
        },
        "task": task,
    }


async def runtime_statuses(
    repo: ManagedCronRepository,
    caller,
    request: RuntimeStatusRequest,
):
    clauses, params = repo._page_filter(caller, False, None)
    placeholders = ", ".join("%s" for _ in request.job_ids)
    # IN 保留索引候选条件，二进制谓词避免数据库默认 collation 扩大 ID 集合。
    clauses.extend(
        (
            f"d.job_id IN ({placeholders})",
            f"d.job_id COLLATE utf8mb4_bin IN ({placeholders})",
        ),
    )
    params.extend(request.job_ids)
    params.extend(request.job_ids)
    try:
        if not getattr(repo.db, "is_connected", False):
            raise ManagedCronUnavailable("cron_database_unavailable")
        rows = await repo.db.fetch_all(
            f"{_SELECT} WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        by_id = {row["job_id"]: _item(row, caller.actor_id) for row in rows}
        return RuntimeStatusResponse.model_validate(
            {
                "items": [
                    by_id.get(job_id, {"id": job_id, "status": "unavailable"})
                    for job_id in request.job_ids
                ],
            },
        )
    except ManagedCronUnavailable:
        raise
    except Exception as exc:
        raise ManagedCronUnavailable("cron_database_unavailable") from exc
