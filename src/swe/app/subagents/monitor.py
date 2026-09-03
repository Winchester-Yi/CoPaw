# -*- coding: utf-8 -*-
"""User-facing Background SubAgent run monitor snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ...agents.tools.subagent_background import (
    build_background_subagent_scope,
    get_default_background_subagent_supervisor,
)
from ...config.config import AgentProfileConfig
from ...runtime_workers import run_runtime_state_work
from .models import (
    BackgroundRunStatus,
    BackgroundSubAgentRunRecord,
    TERMINAL_BACKGROUND_RUN_STATUSES,
)
from .run_store import PerRunSubAgentRunStore
from .supervisor import (
    BackgroundSubAgentNotManageable,
    BackgroundSubAgentScope,
    BackgroundSubAgentSupervisor,
)

_PREVIEW_LIMIT = 160


class SubAgentBudgetConsumption(BaseModel):
    """Elapsed time-budget consumption for a Background SubAgent Run."""

    elapsed_ms: int
    timeout_ms: int
    turns_used: int
    max_turns: int
    ratio: float


class SubAgentRunSnapshotItem(BaseModel):
    """Slim user-facing snapshot item for one Background SubAgent Run."""

    run_id: str
    agent_name: str
    nickname: str | None = None
    objective: str
    status: BackgroundRunStatus
    stoppable: bool
    definition_match: dict[str, Any] = Field(default_factory=dict)
    budget_consumption: SubAgentBudgetConsumption
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    summary_preview: str | None = None
    error_preview: str | None = None


class SubAgentRunSnapshot(BaseModel):
    """Slim snapshot for the current chat's Background SubAgent Runs."""

    chat_id: str
    session_id: str
    runs: list[SubAgentRunSnapshotItem] = Field(default_factory=list)


class SubAgentCancelResult(BaseModel):
    """Result returned after a frontend stop request."""

    run: SubAgentRunSnapshotItem


class SubAgentRunNotManageableError(Exception):
    """Raised when the runtime knows the run but cannot stop it here."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SubAgentMonitorService:
    """Build chat-scoped snapshots and cancel eligible SubAgent runs."""

    def __init__(
        self,
        *,
        supervisor: BackgroundSubAgentSupervisor | Any,
        scope: BackgroundSubAgentScope,
    ) -> None:
        self._supervisor = supervisor
        self._scope = scope
        self._store = PerRunSubAgentRunStore(scope.run_store_dir)

    async def snapshot(
        self,
        *,
        chat_id: str,
        session_id: str,
    ) -> SubAgentRunSnapshot:
        await self._reap_active_runs()
        records = await self._records_for_session(session_id)
        return SubAgentRunSnapshot(
            chat_id=chat_id,
            session_id=session_id,
            runs=[self._snapshot_item(record) for record in records],
        )

    async def cancel(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> SubAgentRunSnapshotItem | None:
        record = await self._get_record(run_id)
        if record is None or record.spec.parent_thread_id != session_id:
            return None
        if record.status != "running":
            raise ValueError("subagent run is not running")
        result = await self._supervisor.cancel(self._scope, run_id)
        if result is None:
            return None
        if isinstance(result, BackgroundSubAgentNotManageable):
            raise SubAgentRunNotManageableError(result.reason)
        if result.status != "cancelled":
            raise ValueError("subagent run is not running")
        return self._snapshot_item(result)

    async def _reap_active_runs(self) -> None:
        wait = getattr(self._supervisor, "wait", None)
        if wait is not None:
            await wait(self._scope, timeout_ms=0)

    async def _records_for_session(
        self,
        session_id: str,
    ) -> list[BackgroundSubAgentRunRecord]:
        return await run_runtime_state_work(
            self._records_for_session_sync,
            session_id,
        )

    def _records_for_session_sync(
        self,
        session_id: str,
    ) -> list[BackgroundSubAgentRunRecord]:
        records: list[BackgroundSubAgentRunRecord] = []
        for path in sorted(self._scope.run_store_dir.glob("subagent-*.json")):
            record = self._read_record(path)
            if record is None:
                continue
            if record.spec.parent_thread_id == session_id:
                records.append(record)
        return sorted(records, key=lambda item: item.created_at)

    async def _get_record(
        self,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord | None:
        try:
            return await self._supervisor.get(self._scope, run_id)
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _read_record(path: Path) -> BackgroundSubAgentRunRecord | None:
        try:
            return BackgroundSubAgentRunRecord.model_validate(
                json.loads(path.read_text(encoding="utf-8")),
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _snapshot_item(
        self,
        record: BackgroundSubAgentRunRecord,
    ) -> SubAgentRunSnapshotItem:
        return SubAgentRunSnapshotItem(
            run_id=record.run_id,
            agent_name=record.spec.name,
            nickname=record.nickname,
            objective=record.spec.objective,
            status=record.status,
            stoppable=record.status == "running",
            definition_match=record.definition_match.model_dump(mode="json"),
            budget_consumption=_budget_consumption(record),
            created_at=_dump_time(record.created_at),
            started_at=_dump_time(record.started_at),
            finished_at=_dump_time(record.finished_at),
            duration_ms=_duration_ms(record),
            summary_preview=_preview(
                (
                    getattr(record.result, "summary", None)
                    if record.result is not None
                    else None
                ),
            ),
            error_preview=_preview(_first_error_message(record)),
        )


def create_monitor_service(workspace: Any) -> SubAgentMonitorService:
    """Create a monitor service for the current workspace."""
    parent_agent_config = getattr(workspace, "config", None)
    if parent_agent_config is None:
        parent_agent_config = AgentProfileConfig(
            id=str(getattr(workspace, "agent_id", "") or "default"),
            name=str(getattr(workspace, "agent_id", "") or "Agent"),
            workspace_dir=str(getattr(workspace, "workspace_dir", ".") or "."),
        )
    request_context: dict[str, Any] = {
        "tenant_id": getattr(workspace, "tenant_id", None) or "default",
        "agent_id": getattr(workspace, "agent_id", None)
        or getattr(parent_agent_config, "id", None)
        or "default",
    }
    run_store_dir = getattr(workspace, "subagent_run_store_dir", None)
    if run_store_dir is not None:
        request_context["_subagent_run_store_dir"] = str(run_store_dir)
    scope = build_background_subagent_scope(
        parent_agent_config=parent_agent_config,
        request_context=request_context,
    )
    supervisor = (
        getattr(
            workspace,
            "subagent_supervisor",
            None,
        )
        or get_default_background_subagent_supervisor()
    )
    return SubAgentMonitorService(supervisor=supervisor, scope=scope)


def _budget_consumption(
    record: BackgroundSubAgentRunRecord,
) -> SubAgentBudgetConsumption:
    timeout_ms = max(int(record.effective_budget.timeout_ms), 0)
    elapsed_ms = _elapsed_ms(record)
    ratio = 1.0 if timeout_ms <= 0 else min(elapsed_ms / timeout_ms, 1.0)
    return SubAgentBudgetConsumption(
        elapsed_ms=elapsed_ms,
        timeout_ms=timeout_ms,
        turns_used=(
            record.result.metrics.turns_used
            if record.result is not None
            else record.turns_used
        ),
        max_turns=max(int(record.effective_budget.max_turns), 0),
        ratio=ratio,
    )


def _elapsed_ms(record: BackgroundSubAgentRunRecord) -> int:
    now = datetime.now(timezone.utc)
    start = record.started_at or record.created_at or now
    end = (
        record.finished_at or now
        if record.status in TERMINAL_BACKGROUND_RUN_STATUSES
        else now
    )
    return max(int((end - start).total_seconds() * 1000), 0)


def _duration_ms(record: BackgroundSubAgentRunRecord) -> int | None:
    if record.started_at is None:
        return None
    end = record.finished_at or datetime.now(timezone.utc)
    return max(int((end - record.started_at).total_seconds() * 1000), 0)


def _first_error_message(record: BackgroundSubAgentRunRecord) -> str | None:
    if not record.errors:
        return None
    return record.errors[0].message


def _preview(value: str | None) -> str | None:
    if not value:
        return None
    return value[:_PREVIEW_LIMIT]


def _dump_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
