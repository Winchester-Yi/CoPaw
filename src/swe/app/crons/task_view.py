# -*- coding: utf-8 -*-
"""Pure task projection shared by runtime and read-only Cron lists."""

from __future__ import annotations

from .models import CronJobSpec, CronJobState, CronTaskView

MANUAL_PAUSE_REASON = "manual"


def build_cron_task_view(
    spec: CronJobSpec,
    state: CronJobState,
    user_id: str | None,
) -> CronTaskView:
    """Build the existing task response without binding chats or writing data."""
    meta = spec.meta or {}
    creator_user_id = meta.get("creator_user_id")
    visible_in_my_tasks = bool(
        spec.task_type in {"agent", "text"}
        and creator_user_id
        and creator_user_id == user_id,
    )
    pause_reason = meta.get("pause_reason")
    if visible_in_my_tasks and not pause_reason and not spec.enabled:
        pause_reason = MANUAL_PAUSE_REASON
    return CronTaskView(
        visible_in_my_tasks=visible_in_my_tasks,
        chat_id=meta.get("task_chat_id"),
        session_id=meta.get("task_session_id"),
        has_scheduled_result=bool(
            meta.get("task_has_scheduled_result", False),
        ),
        latest_scheduled_preview=str(
            meta.get("task_last_scheduled_preview", "") or "",
        ),
        unread_execution_count=int(
            meta.get("task_unread_execution_count", 0) or 0,
        ),
        last_scheduled_run_at=meta.get("task_last_scheduled_run_at"),
        is_running=state.last_status == "running",
        is_paused=bool(pause_reason),
        pause_reason=pause_reason,
        auto_paused_at=meta.get("auto_paused_at"),
    )
