# -*- coding: utf-8 -*-
"""Task state persistence using JSON file."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from swe.constant import WORKING_DIR
from .models import BackupTask, BackupTaskStatus, BackupTaskType


def _normalize_datetime(dt: datetime) -> datetime:
    """Normalize datetime to UTC for comparison.

    Handles both offset-naive and offset-aware datetimes.
    """
    if dt.tzinfo is None:
        # Assume naive datetime is in UTC
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TaskStore:
    """JSON file-based task storage with atomic writes."""

    def __init__(self, file_path: Path | None = None):
        if file_path is None:
            file_path = WORKING_DIR / "backup_tasks.json"
        self._file_path = file_path
        self._lock = threading.Lock()

    def _ensure_file(self) -> None:
        """Ensure file exists."""
        if not self._file_path.exists():
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_path.write_text("{}")

    def _load_all(self) -> dict[str, BackupTask]:
        """Load all tasks from file."""
        self._ensure_file()
        try:
            content = self._file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            return {k: BackupTask(**v) for k, v in data.items()}
        except (json.JSONDecodeError, KeyError):
            return {}

    def _save_all(self, tasks: dict[str, BackupTask]) -> None:
        """Save all tasks to file atomically."""
        data = {k: v.model_dump(mode="json") for k, v in tasks.items()}
        json_str = json.dumps(data, indent=2, ensure_ascii=False, default=str)

        # Atomic write using NamedTemporaryFile
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self._file_path.parent,
            delete=False,
            encoding="utf-8",
        ) as tmp_file:
            tmp_file.write(json_str)
            temp_path = tmp_file.name
        os.replace(temp_path, self._file_path)

    def save(self, task: BackupTask) -> None:
        """Save or update a task."""
        with self._lock:
            tasks = self._load_all()
            tasks[task.task_id] = task
            self._save_all(tasks)

    def get(self, task_id: str) -> Optional[BackupTask]:
        """Get task by ID."""
        with self._lock:
            tasks = self._load_all()
            return tasks.get(task_id)

    def get_all(
        self,
        status: BackupTaskStatus | None = None,
        task_type: str | None = None,
        limit: int = 50,
    ) -> list[BackupTask]:
        """Get tasks with optional filters.

        Note: The lock is released before filtering by status/type, which is
        acceptable for this use case as filtering doesn't require strict
        consistency.
        """
        with self._lock:
            tasks = self._load_all()
            result = list(tasks.values())

        if status:
            result = [t for t in result if t.status == status]
        if task_type:
            result = [
                t for t in result if t.task_type == BackupTaskType(task_type)
            ]

        # Sort by created_at desc (handle mixed timezone-aware/naive datetimes)
        result.sort(
            key=lambda t: _normalize_datetime(t.created_at),
            reverse=True,
        )
        return result[:limit]

    def has_running_task(self) -> bool:
        """Check if any task is currently running."""
        with self._lock:
            tasks = self._load_all()
            return any(
                t.status == BackupTaskStatus.RUNNING for t in tasks.values()
            )

    def delete(self, task_id: str) -> bool:
        """Delete a task. Returns False if task not found or is running."""
        with self._lock:
            tasks = self._load_all()
            task = tasks.get(task_id)
            if task is None:
                return False
            if task.status == BackupTaskStatus.RUNNING:
                return False
            del tasks[task_id]
            self._save_all(tasks)
            return True
