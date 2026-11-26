# -*- coding: utf-8 -*-
"""新看板查询模型收敛筛选参数，列表与明细分页界限一致。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BoardListQuery(BaseModel):
    start_time: datetime | None = None
    end_time: datetime | None = None
    status: (
        Literal["received", "pending", "running", "completed", "failed"] | None
    ) = None
    child_status: (
        Literal[
            "queued",
            "dispatching",
            "accepted",
            "running",
            "waiting_subtasks",
            "success",
            "error",
            "dispatch_failed",
            "skipped",
            "cancelled",
        ]
        | None
    ) = None
    has_members: bool | None = None
    follow_min: int | None = Field(default=None, ge=0, le=5)
    follow_max: int | None = Field(default=None, ge=0, le=5)
    query: str | None = Field(default=None, max_length=200)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class BoardOccupancyQuery(BaseModel):
    provider_id: str | None = Field(default=None, min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=100)


class BoardWorkerTrendQuery(BaseModel):
    start_time: datetime
    end_time: datetime
    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)


class BoardDetailQuery(BaseModel):
    member_page: int = Field(default=1, ge=1)
    member_limit: int = Field(default=50, ge=1, le=200)
    member_status: (
        Literal[
            "queued",
            "dispatching",
            "accepted",
            "running",
            "success",
            "error",
            "dispatch_failed",
            "skipped",
            "cancelled",
        ]
        | None
    ) = None
    subtask_status: (
        Literal[
            "waiting_subtasks",
            "success",
            "error",
            "skipped",
            "cancelled",
            "unconfirmed",
        ]
        | None
    ) = None
    member_role: Literal["parent", "child"] | None = None
    member_query: str | None = Field(default=None, max_length=200)
    event_page: int = Field(default=1, ge=1)
    event_limit: int = Field(default=50, ge=1, le=200)
    event_type: str | None = Field(default=None, max_length=64)
    event_execution_id: str | None = Field(default=None, max_length=128)
