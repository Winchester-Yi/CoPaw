# -*- coding: utf-8 -*-
"""会话文件的纯裁剪算法和写锁，不读取任务定义或实例化管理器。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TASK_MESSAGES_STATE_KEY = "task_messages"


@dataclass(frozen=True)
class _TaskSessionCleanupSnapshot:
    state: dict[str, Any]
    changed: bool
    has_result: bool
    latest_preview: str
    latest_run_at: datetime | None
    removed_runs: int = 0
    removed_messages: int = 0
    skipped_runs: int = 0


def _parse_cleanup_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00"),
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_task_message_preview(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
    return "".join(parts).strip()


@dataclass(frozen=True)
class _TaskSessionCleanupParts:
    next_state: dict[str, Any]
    memory_state: dict[str, Any]
    memory_content: list[Any]
    task_runs: list[Any]
    task_messages: list[Any]


@dataclass(frozen=True)
class _TaskRunPruneResult:
    adjusted_runs: list[Any]
    retained_content: list[Any]
    removed_runs: int
    skipped_runs: int
    latest_preview: str
    latest_run_at: datetime | None
    aborted: bool = False


@dataclass(frozen=True)
class _TaskMessagePruneResult:
    kept_messages: list[Any]
    removed_messages: int
    latest_preview: str
    latest_run_at: datetime | None


def _cleanup_state_parts(state: dict[str, Any]) -> _TaskSessionCleanupParts:
    next_state = deepcopy(state)
    raw_runs = next_state.get("task_runs")
    task_runs = raw_runs if isinstance(raw_runs, list) else []
    agent_state = next_state.get("agent")
    if not isinstance(agent_state, dict):
        agent_state = {}
        next_state["agent"] = agent_state
    memory_state = agent_state.get("memory")
    if not isinstance(memory_state, dict):
        memory_state = {}
        agent_state["memory"] = memory_state
    content = memory_state.get("content")
    memory_content = content if isinstance(content, list) else []
    raw_messages = next_state.get(TASK_MESSAGES_STATE_KEY)
    task_messages = raw_messages if isinstance(raw_messages, list) else []
    return _TaskSessionCleanupParts(
        next_state=next_state,
        memory_state=memory_state,
        memory_content=memory_content,
        task_runs=task_runs,
        task_messages=task_messages,
    )


def _task_run_memory_range(
    raw_run: dict[str, Any],
    memory_content_length: int,
) -> tuple[int, int] | None:
    raw_start = raw_run.get("memory_start")
    raw_end = raw_run.get("memory_end")
    if not isinstance(raw_start, int) or not isinstance(raw_end, int):
        return None
    if raw_start < 0 or raw_end < raw_start:
        return None
    if raw_end > memory_content_length:
        return None
    return raw_start, raw_end


def _adjust_task_run_memory_ranges(
    kept_runs: list[Any],
    keep_mask: list[bool],
) -> list[Any]:
    retained_counts = [0]
    for kept in keep_mask:
        retained_counts.append(retained_counts[-1] + int(kept))

    adjusted_runs: list[Any] = []
    for raw_run in kept_runs:
        if not isinstance(raw_run, dict):
            adjusted_runs.append(raw_run)
            continue
        start = int(raw_run["memory_start"])
        end = int(raw_run["memory_end"])
        adjusted = dict(raw_run)
        adjusted["memory_start"] = retained_counts[start]
        adjusted["memory_end"] = retained_counts[end]
        adjusted_runs.append(adjusted)
    return adjusted_runs


def _prune_task_runs(
    task_runs: list[Any],
    memory_content: list[Any],
    cutoff: datetime,
) -> _TaskRunPruneResult:
    kept_runs: list[Any] = []
    removed_runs = 0
    skipped_runs = 0
    keep_mask = [True] * len(memory_content)
    latest_preview = ""
    latest_run_at: datetime | None = None

    for raw_run in task_runs:
        if not isinstance(raw_run, dict):
            kept_runs.append(raw_run)
            skipped_runs += 1
            continue
        memory_range = _task_run_memory_range(raw_run, len(memory_content))
        if memory_range is None:
            return _TaskRunPruneResult(
                adjusted_runs=[],
                retained_content=[],
                removed_runs=0,
                skipped_runs=1,
                latest_preview="",
                latest_run_at=None,
                aborted=True,
            )
        raw_start, raw_end = memory_range
        ended_at = _parse_cleanup_datetime(raw_run.get("ended_at"))
        if ended_at is not None and ended_at < cutoff:
            removed_runs += 1
            for index in range(raw_start, raw_end):
                keep_mask[index] = False
            continue
        kept_runs.append(raw_run)
        if ended_at is None:
            skipped_runs += 1
            continue
        if latest_run_at is None or ended_at >= latest_run_at:
            latest_run_at = ended_at
            latest_preview = str(raw_run.get("preview_text") or "")

    retained_content = [
        item for index, item in enumerate(memory_content) if keep_mask[index]
    ]
    return _TaskRunPruneResult(
        adjusted_runs=_adjust_task_run_memory_ranges(kept_runs, keep_mask),
        retained_content=retained_content,
        removed_runs=removed_runs,
        skipped_runs=skipped_runs,
        latest_preview=latest_preview,
        latest_run_at=latest_run_at,
    )


def _prune_task_messages(
    task_messages: list[Any],
    cutoff: datetime,
    latest_preview: str,
    latest_run_at: datetime | None,
) -> _TaskMessagePruneResult:
    kept_messages: list[Any] = []
    removed_messages = 0
    for raw_message in task_messages:
        if not isinstance(raw_message, dict):
            kept_messages.append(raw_message)
            continue
        timestamp = _parse_cleanup_datetime(raw_message.get("timestamp"))
        if timestamp is not None and timestamp < cutoff:
            removed_messages += 1
            continue
        kept_messages.append(raw_message)
        if timestamp is not None and (
            latest_run_at is None or timestamp >= latest_run_at
        ):
            latest_run_at = timestamp
            message_preview = _extract_task_message_preview(raw_message)
            if message_preview:
                latest_preview = message_preview

    return _TaskMessagePruneResult(
        kept_messages=kept_messages,
        removed_messages=removed_messages,
        latest_preview=latest_preview,
        latest_run_at=latest_run_at,
    )


def _aborted_cleanup_snapshot(
    state: dict[str, Any],
) -> _TaskSessionCleanupSnapshot:
    return _TaskSessionCleanupSnapshot(
        state=state,
        changed=False,
        has_result=False,
        latest_preview="",
        latest_run_at=None,
        skipped_runs=1,
    )


def _prune_task_session_state(
    state: dict[str, Any],
    cutoff: datetime,
) -> _TaskSessionCleanupSnapshot:
    parts = _cleanup_state_parts(state)
    run_result = _prune_task_runs(
        parts.task_runs,
        parts.memory_content,
        cutoff,
    )
    if run_result.aborted:
        return _aborted_cleanup_snapshot(state)

    message_result = _prune_task_messages(
        parts.task_messages,
        cutoff,
        run_result.latest_preview,
        run_result.latest_run_at,
    )

    parts.memory_state["content"] = run_result.retained_content
    parts.next_state["task_runs"] = run_result.adjusted_runs
    parts.next_state[TASK_MESSAGES_STATE_KEY] = message_result.kept_messages
    changed = (
        run_result.removed_runs > 0 or message_result.removed_messages > 0
    )
    return _TaskSessionCleanupSnapshot(
        state=parts.next_state,
        changed=changed,
        has_result=message_result.latest_run_at is not None,
        latest_preview=message_result.latest_preview[:10],
        latest_run_at=message_result.latest_run_at,
        removed_runs=run_result.removed_runs,
        removed_messages=message_result.removed_messages,
        skipped_runs=run_result.skipped_runs,
    )


@asynccontextmanager
async def task_session_write_lock(
    session,
    session_id,
    user_id,
    *,
    timeout_seconds=5.0,
):
    factory = getattr(session, "session_write_lock", None)
    if not callable(factory):
        yield
        return
    async with factory(session_id, user_id, timeout_seconds=timeout_seconds):
        yield
