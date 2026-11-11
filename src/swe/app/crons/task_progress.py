# -*- coding: utf-8 -*-
"""任务进度展示算法，与定义存储和管理器无关。"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from .cron_utils import compute_next_run_at
from .models import CronJobSpec, CronJobState


class TaskProgressView:
    def __init__(self, timezone_name="UTC"):
        self._timezone = timezone_name

    def _calculate_run_times_on_date(
        self,
        job: CronJobSpec,
        date: datetime,
    ) -> list[datetime]:
        """Calculate all scheduled run times for a job on a given date.

        Args:
            job: The cron job specification
            date: The date to calculate run times for

        Returns:
            List of scheduled run times on that date
        """
        cron_expr = job.schedule.cron if job.schedule else ""
        tz_name = job.schedule.timezone or self._timezone or "UTC"
        if not cron_expr:
            return []

        start_of_day = date.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if start_of_day.tzinfo is None:
            start_of_day = start_of_day.replace(tzinfo=timezone.utc)

        run_times: list[datetime] = []
        cursor = start_of_day
        safety_limit = 20

        for _ in range(safety_limit):
            try:
                next_run = compute_next_run_at(
                    cron_expr,
                    tz_name,
                    now=cursor,
                )
            except Exception:
                break

            if next_run.date() != date.date():
                break

            run_times.append(next_run)
            cursor = next_run + timedelta(seconds=1)

        return run_times

    def _determine_task_status(
        self,
        scheduled_time: datetime,
        state: CronJobState,
        date: datetime,
    ) -> str:
        """Determine task status based on schedule and execution state.

        Args:
            scheduled_time: The scheduled execution time
            state: The job's current state
            date: The query date

        Returns:
            Status string: "completed", "in_progress", "pending", "error", "cancelled"
        """
        now = datetime.now(timezone.utc)
        last_run = state.last_run_at

        if state.last_status == "running":
            return "in_progress"
        if scheduled_time > now:
            return "pending"
        if last_run and last_run.date() == date.date():
            if state.last_status == "success":
                return "completed"
            if state.last_status in ("error", "cancelled"):
                return state.last_status
            return "in_progress"
        return "pending"

    def _build_task_status_display(
        self,
        task_status: str,
        scheduled_time: Optional[datetime],
        last_run: Optional[datetime],
    ) -> tuple[str, str]:
        """Build status_text and time_info for display.

        Args:
            task_status: The task status
            scheduled_time: Scheduled execution time (UTC)
            last_run: Last actual run time (UTC)

        Returns:
            Tuple of (status_text, time_info)
        """
        status_map = {
            "completed": ("已完成", "已执行完成"),
            "in_progress": ("进行中", "任务执行中"),
            "pending": ("待开始", "等待执行"),
            "error": ("执行失败", "执行失败"),
            "cancelled": ("已取消", "任务已取消"),
        }

        if task_status not in status_map:
            return ("未知", "")

        status_text, default_info = status_map[task_status]

        # Convert UTC to local time for display
        def utc_to_local(dt: Optional[datetime]) -> Optional[datetime]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                # Assume UTC if no timezone
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()

        local_last_run = utc_to_local(last_run)
        local_scheduled_time = utc_to_local(scheduled_time)

        if task_status == "completed" and local_last_run:
            time_info = f"{local_last_run.strftime('%H:%M')}已完成"
        elif task_status == "in_progress" and local_last_run:
            time_info = f"{local_last_run.strftime('%H:%M')}已启动"
        elif task_status == "error" and local_last_run:
            time_info = f"{local_last_run.strftime('%H:%M')}执行失败"
        elif task_status == "pending" and local_scheduled_time:
            time_info = f"将于{local_scheduled_time.strftime('%H:%M')}执行"
        else:
            time_info = default_info

        return (status_text, time_info)
