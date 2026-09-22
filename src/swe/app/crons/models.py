# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..channels.schema import DEFAULT_CHANNEL
from ...providers.models import ModelSlotConfig

# ---------------------------------------------------------------------------
# APScheduler v3 uses ISO 8601 weekday numbering (0=Mon … 6=Sun) for
# CronTrigger(day_of_week=...), while standard crontab uses 0=Sun … 6=Sat.
# from_crontab() does NOT convert either.  Three-letter English abbreviations
# (mon, tue, …, sun) are unambiguous in both systems, so we normalise the
# 5th cron field to abbreviations at validation time.
# ---------------------------------------------------------------------------

_CRONTAB_NUM_TO_NAME: dict[str, str] = {
    "0": "sun",
    "1": "mon",
    "2": "tue",
    "3": "wed",
    "4": "thu",
    "5": "fri",
    "6": "sat",
    "7": "sun",
}

DEFAULT_CRON_TIMEOUT_SECONDS = 7200
DEFAULT_CRON_MISFIRE_GRACE_SECONDS = 300
MAX_CRON_SKILL_IDS_LENGTH = 200
_SKILL_ID_SAFE_CHARS = frozenset("_.:-")
_SKILL_ID_SPLIT_PATTERN = re.compile(r"[,\s]+")


def _crontab_dow_to_name(field: str) -> str:
    """Convert the day-of-week field from crontab numbers to abbreviations.

    Handles: ``*``, single values, comma-separated lists, and ranges.
    Already-named values (``mon``, ``tue``, …) are passed through unchanged.
    """
    if field == "*":
        return field

    def _convert_token(tok: str) -> str:
        if "/" in tok:
            base, step = tok.rsplit("/", 1)
            return f"{_convert_token(base)}/{step}"
        if "-" in tok:
            parts = tok.split("-", 1)
            return "-".join(_CRONTAB_NUM_TO_NAME.get(p, p) for p in parts)
        return _CRONTAB_NUM_TO_NAME.get(tok, tok)

    return ",".join(_convert_token(t) for t in field.split(","))


def normalize_cron_skill_ids(value: Any) -> str:
    """将技能 ID 列表归一化为逗号分隔字符串。"""
    if value is None:
        return ""

    raw_items = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        for item in _SKILL_ID_SPLIT_PATTERN.split(str(raw_item)):
            skill_id = item.strip()
            if not skill_id:
                continue
            if not _is_skill_id(skill_id):
                raise ValueError(f"invalid skill_ids item: {skill_id}")
            if skill_id not in seen:
                seen.add(skill_id)
                normalized.append(skill_id)

    result = ",".join(normalized)
    if len(result) > MAX_CRON_SKILL_IDS_LENGTH:
        raise ValueError("skill_ids total length must be <= 200")
    return result


def cron_skill_ids_contains(skill_ids: Any, skill_id: str) -> bool:
    """按逗号边界精确判断技能 ID 是否存在。"""
    try:
        normalized = normalize_cron_skill_ids(skill_ids)
        target = normalize_cron_skill_ids(skill_id)
    except ValueError:
        return False
    if not normalized or not target or "," in target:
        return False
    return f",{target}," in f",{normalized},"


def _is_skill_id(value: str) -> bool:
    return bool(value) and all(
        char.isalnum() or char in _SKILL_ID_SAFE_CHARS for char in value
    )


class ScheduleSpec(BaseModel):
    type: Literal["cron"] = "cron"
    cron: str = Field(...)
    timezone: str = "UTC"

    @field_validator("cron")
    @classmethod
    def normalize_cron_5_fields(cls, v: str) -> str:
        parts = [p for p in v.split() if p]
        if len(parts) == 5:
            parts[4] = _crontab_dow_to_name(parts[4])
            return " ".join(parts)

        if len(parts) == 4:
            # treat as: hour dom month dow
            hour, dom, month, dow = parts
            return f"0 {hour} {dom} {month} {_crontab_dow_to_name(dow)}"

        if len(parts) == 3:
            # treat as: dom month dow
            dom, month, dow = parts
            return f"0 0 {dom} {month} {_crontab_dow_to_name(dow)}"

        # 6 fields (seconds) or too short: reject
        raise ValueError(
            "cron must have 5 fields "
            "(or 4/3 fields that can be normalized); seconds not supported.",
        )


class DispatchTarget(BaseModel):
    user_id: str
    session_id: str


class DispatchSpec(BaseModel):
    type: Literal["channel"] = "channel"
    channel: str = Field(default=DEFAULT_CHANNEL)
    target: DispatchTarget
    mode: Literal["stream", "final"] = Field(default="stream")
    meta: Dict[str, Any] = Field(default_factory=dict)


class JobRuntimeSpec(BaseModel):
    max_concurrency: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(
        default=DEFAULT_CRON_TIMEOUT_SECONDS,
        ge=1,
    )
    misfire_grace_seconds: int = Field(
        default=DEFAULT_CRON_MISFIRE_GRACE_SECONDS,
        ge=0,
    )


class CronJobRequest(BaseModel):
    """Passthrough payload to runner.stream_query(request=...).

    This is aligned with AgentRequest(extra="allow"). We keep it permissive.
    """

    model_config = ConfigDict(extra="allow")

    input: Any = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None


TaskType = Literal["text", "agent"]


class CronJobSpec(BaseModel):
    id: str
    name: str
    enabled: bool = True
    plan_id: Optional[str] = Field(
        default=None,
        max_length=255,
        validation_alias=AliasChoices("planId", "plan_id"),
        description="关联计划ID",
    )

    # Tenant isolation: each job belongs to a tenant
    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Tenant ID for job isolation. If None, uses default tenant."
        ),
    )

    # Identity headers from request
    bbk_id: Optional[str] = Field(
        default=None,
        description="分行号 (from X-Bbk-Id header)",
    )
    source_id: Optional[str] = Field(
        default=None,
        description="来源标识 (from X-Source-Id header)",
    )
    tenant_name: Optional[str] = Field(
        default=None,
        description="租户姓名 (from X-User-Name header)",
    )
    scope_id: Optional[str] = Field(
        default=None,
        description="运行时 scope 标识 (tenant_id + source_id)",
    )

    schedule: ScheduleSpec
    task_type: TaskType = "agent"
    text: Optional[str] = None
    request: Optional[CronJobRequest] = None
    model_slot: Optional[ModelSlotConfig] = None
    skill_ids: str = ""
    dispatch: DispatchSpec

    runtime: JobRuntimeSpec = Field(default_factory=JobRuntimeSpec)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("skill_ids", mode="before")
    @classmethod
    def _normalize_skill_ids(cls, value: Any) -> str:
        return normalize_cron_skill_ids(value)

    @model_validator(mode="after")
    def _validate_task_type_fields(self) -> "CronJobSpec":
        if self.task_type == "text":
            if not (self.text and self.text.strip()):
                raise ValueError("task_type is text but text is empty")
            self.model_slot = None
        elif self.task_type == "agent":
            if self.request is None:
                raise ValueError("task_type is agent but request is missing")
            # Default request context to the dispatch target when omitted.
            target = self.dispatch.target
            self.request = self.request.model_copy(
                update={
                    "user_id": self.request.user_id or target.user_id,
                    "session_id": self.request.session_id or target.session_id,
                },
            )
        return self


class JobsFile(BaseModel):
    version: int = 1
    definition_version: int = 0
    jobs: list[CronJobSpec] = Field(default_factory=list)


class CronJobState(BaseModel):
    next_run_at: Optional[datetime] = None
    next_run_times: list[datetime] = Field(default_factory=list)
    last_run_at: Optional[datetime] = None
    last_prefetch_at: Optional[datetime] = None
    last_status: Optional[
        Literal["success", "error", "running", "skipped", "cancelled"]
    ] = None
    last_error: Optional[str] = None
    external_job_id: Optional[str] = None


class CronJobView(BaseModel):
    spec: CronJobSpec
    state: CronJobState = Field(default_factory=CronJobState)
    task: Optional[CronTaskView] = None


class CronTaskView(BaseModel):
    visible_in_my_tasks: bool = False
    chat_id: Optional[str] = None
    session_id: Optional[str] = None
    has_scheduled_result: bool = False
    latest_scheduled_preview: str = ""
    unread_execution_count: int = 0
    last_scheduled_run_at: Optional[datetime] = None
    is_running: bool = False
    is_paused: bool = False
    pause_reason: Optional[Literal["manual", "auto_unread_threshold"]] = None
    auto_paused_at: Optional[datetime] = None


class CronJobListItem(CronJobSpec):
    state: CronJobState = Field(default_factory=CronJobState)
    task: Optional[CronTaskView] = None
