# -*- coding: utf-8 -*-
"""按已加载 ID 查询运行展示状态，不携带任务定义。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from .identity import identity_value


class RuntimeStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_ids: list[StrictStr] = Field(min_length=1, max_length=200)

    @field_validator("job_ids")
    @classmethod
    def validate_ids(cls, values):
        # 原始容量由 Field 先检查，不能让重复 ID 绕过批次上限。
        return list(dict.fromkeys(identity_value(value) for value in values))


class RuntimeState(BaseModel):
    last_status: str | None
    last_run_at: datetime | str | None
    last_error: str | None
    last_error_truncated: bool
    next_run_at: str | None
    next_run_times: list[str]


class RuntimeTask(BaseModel):
    visible_in_my_tasks: bool
    chat_id: str | None
    session_id: str | None
    is_running: bool
    is_paused: bool
    pause_reason: str | None
    auto_paused_at: datetime | str | None
    has_scheduled_result: bool
    latest_scheduled_preview: str
    latest_scheduled_preview_truncated: bool
    last_scheduled_run_at: datetime | str | None
    unread_execution_count: int


class RuntimeUnavailable(BaseModel):
    id: str
    status: Literal["unavailable"]


class RuntimeStateMissing(BaseModel):
    id: str
    status: Literal["state_missing"]
    definition_version: int


class RuntimeOk(BaseModel):
    id: str
    status: Literal["ok"]
    definition_version: int
    management_version: int
    state_version: int
    management_status: str | None
    dispatch_state: str | None
    enabled: bool
    state: RuntimeState
    task: RuntimeTask


class RuntimeStatusResponse(BaseModel):
    items: list[
        Annotated[
            RuntimeOk | RuntimeUnavailable | RuntimeStateMissing,
            Field(discriminator="status"),
        ]
    ]
