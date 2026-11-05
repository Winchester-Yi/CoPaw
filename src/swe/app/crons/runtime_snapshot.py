# -*- coding: utf-8 -*-
"""Verified, immutable legacy runtime projections, independent of spec caches."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...config.context import is_valid_identity_value
from .cron_utils import compute_next_run_times
from .models import CronJobSpec, CronJobState, CronTaskView, JobsFile
from .task_view import MANUAL_PAUSE_REASON, build_cron_task_view


class RuntimeStatusesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_ids: list[
        Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    ] = Field(
        min_length=1,
        max_length=200,
    )

    @field_validator("job_ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        if any(
            not is_valid_identity_value(v) or v != v.strip() for v in values
        ):
            raise ValueError("invalid job ID")
        return list(dict.fromkeys(values))


class RuntimeState(BaseModel):
    last_status: str | None
    last_run_at: str | None
    last_error: str | None
    last_error_truncated: bool
    next_run_at: str | None
    next_run_times: list[str]


class RuntimeTask(CronTaskView):
    latest_scheduled_preview_truncated: bool


class RuntimeOkItem(BaseModel):
    id: str
    status: Literal["ok"] = "ok"
    enabled: bool
    state: RuntimeState
    task: RuntimeTask


class RuntimeMissingItem(BaseModel):
    id: str
    status: Literal["unavailable", "binding_missing"]


class RuntimeStatusesResponse(BaseModel):
    items: list[RuntimeOkItem | RuntimeMissingItem]


@dataclass(frozen=True)
class RuntimeJob:
    id: str
    enabled: bool
    cron: str
    timezone: str
    tenant_id: str | None
    source_id: str | None
    scope_id: str | None
    creator_user_id: str | None
    task: tuple[tuple[str, str | bool | int | None], ...]

    @classmethod
    def from_spec(cls, spec: CronJobSpec) -> RuntimeJob:
        creator = spec.meta.get("creator_user_id")
        task = build_cron_task_view(spec, CronJobState(), None)
        preview = task.latest_scheduled_preview
        values = task.model_dump(mode="json")
        values["latest_scheduled_preview"] = preview[:256]
        values["latest_scheduled_preview_truncated"] = len(preview) > 256
        return cls(
            spec.id,
            spec.enabled,
            spec.schedule.cron,
            spec.schedule.timezone,
            spec.tenant_id,
            spec.source_id,
            spec.scope_id,
            creator,
            tuple(values.items()),
        )

    def runtime_item(self, state: CronJobState, user_id: str, timezone: str):
        state = state.model_copy(deep=True)
        runs = compute_next_run_times(
            self.cron,
            self.timezone or timezone,
            count=3,
        )
        values = state.model_dump(mode="json")
        error = values["last_error"]
        task = dict(self.task)
        task["visible_in_my_tasks"] = self.creator_user_id == user_id
        if (
            task["visible_in_my_tasks"]
            and not task["pause_reason"]
            and not self.enabled
        ):
            task["pause_reason"] = MANUAL_PAUSE_REASON
            task["is_paused"] = True
        task["is_running"] = state.last_status == "running"
        return RuntimeOkItem(
            id=self.id,
            enabled=self.enabled,
            state=RuntimeState(
                last_status=state.last_status,
                last_run_at=values["last_run_at"],
                last_error=error[:1024] if error is not None else None,
                last_error_truncated=error is not None and len(error) > 1024,
                next_run_at=runs[0].isoformat() if runs else None,
                next_run_times=[run.isoformat() for run in runs],
            ),
            task=RuntimeTask.model_validate(task),
        )


def _signature(stat: os.stat_result | None) -> tuple:
    if stat is None:
        return (False,)
    return (
        True,
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
    )


class _ChangedDuringRead(OSError):
    pass


class RuntimeSnapshotCache:
    """One compact version per repository; a cancelled waiter cannot cancel IO."""

    def __init__(self, path: Path):
        self.path = path
        self._signature: tuple | None = None
        self._snapshot: Mapping[str, RuntimeJob] = MappingProxyType({})
        self._inflight: asyncio.Task | None = None

    def _path_signature(self) -> tuple:
        try:
            return _signature(self.path.stat())
        except FileNotFoundError:
            return _signature(None)

    async def get(self) -> Mapping[str, RuntimeJob]:
        signature = await asyncio.to_thread(self._path_signature)
        if signature == self._signature:
            return self._snapshot
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.create_task(self._refresh())
            # Consume failures even when every waiter is cancelled.
            self._inflight.add_done_callback(
                lambda task: (
                    task.exception() if not task.cancelled() else None
                ),
            )
        return await asyncio.shield(self._inflight)

    async def _refresh(self) -> Mapping[str, RuntimeJob]:
        signature, snapshot = await asyncio.to_thread(self._load_sync)
        self._signature, self._snapshot = signature, snapshot
        return snapshot

    def _load_sync(self) -> tuple[tuple, Mapping[str, RuntimeJob]]:
        for attempt in range(2):
            try:
                return self._read_once()
            except _ChangedDuringRead:
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def _read_once(self) -> tuple[tuple, Mapping[str, RuntimeJob]]:
        expected = self._path_signature()
        if expected == (False,):
            if self._path_signature() != expected:
                raise _ChangedDuringRead("cron file appeared during stat")
            return expected, MappingProxyType({})
        try:
            with self.path.open("rb") as stream:
                before = _signature(os.fstat(stream.fileno()))
                if before != expected:
                    raise _ChangedDuringRead("cron file replaced before open")
                content = stream.read()
                after = _signature(os.fstat(stream.fileno()))
        except FileNotFoundError as exc:
            raise _ChangedDuringRead("cron file removed before open") from exc
        if before != after or after != self._path_signature():
            raise _ChangedDuringRead("cron file changed during read")
        try:
            jobs = JobsFile.model_validate_json(content)
            snapshot = MappingProxyType(
                {job.id: RuntimeJob.from_spec(job) for job in jobs.jobs},
            )
        except ValueError:
            if after != self._path_signature():
                raise _ChangedDuringRead("cron file changed during validation")
            raise
        if after != self._path_signature():
            raise _ChangedDuringRead("cron file changed during projection")
        return after, snapshot
