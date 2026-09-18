# -*- coding: utf-8 -*-
"""Models for high-frequency question analysis APIs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


MAX_QUERY_DAYS = 31
MAX_TASK_QUERY_DAYS = 7
MAX_MESSAGE_ROWS = 10000
MAX_RANK_NO = 10
MAX_SAMPLE_QUESTIONS = 4
MAX_SAMPLE_QUESTION_LENGTH = 1000


def _strip_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class HighFrequencyQuestionMessageQueryRequest(BaseModel):
    """Request for querying source user messages."""

    source_id: str = Field(default="", max_length=64)
    start_time: datetime
    end_time: datetime
    bbk_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("source_id")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("bbk_id")
    @classmethod
    def _strip_bbk_id(cls, value: Optional[str]) -> Optional[str]:
        return _strip_optional(value)

    @model_validator(mode="after")
    def _validate_time_range(
        self,
    ) -> "HighFrequencyQuestionMessageQueryRequest":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        if self.end_time - self.start_time > timedelta(days=MAX_QUERY_DAYS):
            raise ValueError("time range must not exceed 31 days")
        return self


class HighFrequencyQuestionMessageResponse(BaseModel):
    """Single source message returned for analysis."""

    user_id: Optional[str] = None
    bbk_id: Optional[str] = None
    content: str
    skills_used: list[str] = Field(default_factory=list)


class HighFrequencyQuestionMessageListResponse(BaseModel):
    """Message query response."""

    total: int
    message_count: int
    user_count: int
    data: list[HighFrequencyQuestionMessageResponse] = Field(
        default_factory=list,
    )


class HighFrequencyQuestionResultItem(BaseModel):
    """Single high-frequency question ranking result."""

    scope_type: Literal["ALL", "ORG"]
    bbk_id: str = Field(..., min_length=1, max_length=64)
    rank_no: int = Field(..., ge=1, le=MAX_RANK_NO)
    topic_name: str = Field(..., min_length=1, max_length=255)
    message_count: int = Field(..., ge=0)
    valid_message_count: int = Field(..., ge=0)
    user_count: int = Field(..., ge=0)
    total_skill_used_count: int = Field(..., ge=0)
    skill_used_count: int = Field(..., ge=0)
    top_skill: Optional[str] = Field(default=None, max_length=255)
    bbk_dis: Any = Field(default_factory=dict)
    sample_questions: list[str] = Field(default_factory=list)

    @field_validator("bbk_id", "topic_name")
    @classmethod
    def _strip_required_fields(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("field must not be blank")
        return stripped

    @field_validator("sample_questions")
    @classmethod
    def _validate_sample_questions(cls, values: list[str]) -> list[str]:
        if len(values) > MAX_SAMPLE_QUESTIONS:
            raise ValueError("sample_questions must contain at most 4 items")

        normalized: list[str] = []
        for value in values:
            stripped = str(value or "").strip()
            if not stripped:
                continue
            if len(stripped) > MAX_SAMPLE_QUESTION_LENGTH:
                raise ValueError(
                    "sample question must not exceed 1000 characters",
                )
            normalized.append(stripped)
        return normalized

    @field_validator("top_skill")
    @classmethod
    def _strip_top_skill(cls, value: Optional[str]) -> Optional[str]:
        return _strip_optional(value)

    @model_validator(mode="after")
    def _validate_result_item(self) -> "HighFrequencyQuestionResultItem":
        if self.scope_type == "ALL" and self.bbk_id != "ALL":
            raise ValueError("bbk_id must be ALL when scope_type is ALL")
        if self.scope_type == "ORG" and self.bbk_id == "ALL":
            raise ValueError("bbk_id must not be ALL when scope_type is ORG")
        if self.message_count > self.valid_message_count:
            raise ValueError(
                "message_count must not exceed valid_message_count",
            )
        if self.total_skill_used_count > self.valid_message_count:
            raise ValueError(
                "total_skill_used_count must not exceed valid_message_count",
            )
        if self.skill_used_count > self.message_count:
            raise ValueError("skill_used_count must not exceed message_count")
        return self


class HighFrequencyQuestionResultSaveRequest(BaseModel):
    """Request for saving high-frequency question results."""

    source_id: str = Field(default="", max_length=64)
    batch_id: str = Field(..., min_length=1, max_length=64)
    stat_start_time: datetime
    stat_end_time: datetime
    results: list[HighFrequencyQuestionResultItem] = Field(
        ...,
        min_length=1,
    )

    @field_validator("source_id")
    @classmethod
    def _strip_source_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("batch_id")
    @classmethod
    def _strip_batch_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("field is required")
        return stripped

    @model_validator(mode="after")
    def _validate_save_request(
        self,
    ) -> "HighFrequencyQuestionResultSaveRequest":
        if self.stat_start_time >= self.stat_end_time:
            raise ValueError(
                "stat_start_time must be earlier than stat_end_time",
            )

        seen: set[tuple[str, str, str, str, int]] = set()
        batch_counts: tuple[int, int, int] | None = None
        for result in self.results:
            key = (
                self.source_id,
                self.batch_id,
                result.scope_type,
                result.bbk_id,
                result.rank_no,
            )
            if key in seen:
                raise ValueError(
                    "duplicate source_id + batch_id + scope_type + bbk_id + rank_no",
                )
            seen.add(key)
            current_counts = (
                result.valid_message_count,
                result.user_count,
                result.total_skill_used_count,
            )
            if batch_counts is None:
                batch_counts = current_counts
            elif current_counts != batch_counts:
                raise ValueError(
                    "valid_message_count, user_count, and total_skill_used_count "
                    "must be consistent within the same batch",
                )
        return self


class HighFrequencyQuestionResultSaveResponse(BaseModel):
    """Response for saving a result batch."""

    batch_id: str
    saved_count: int


class HighFrequencyQuestionCriteriaRequest(BaseModel):
    """Base criteria for task submission and result lookup."""

    source_id: str = Field(default="", max_length=64)
    start_time: datetime
    end_time: datetime
    bbk_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("source_id")
    @classmethod
    def _strip_source_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("bbk_id")
    @classmethod
    def _strip_optional_bbk_id(cls, value: Optional[str]) -> Optional[str]:
        return _strip_optional(value)

    @model_validator(mode="after")
    def _validate_task_time_range(
        self,
    ) -> "HighFrequencyQuestionCriteriaRequest":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        if self.end_time.date() - self.start_time.date() > timedelta(
            days=MAX_TASK_QUERY_DAYS,
        ):
            raise ValueError("date range must not exceed 7 days")
        return self


class HighFrequencyQuestionTaskSubmitRequest(
    HighFrequencyQuestionCriteriaRequest,
):
    """Request for submitting a high-frequency question analysis task."""

    force: bool = Field(
        default=False,
        description="Whether to bypass recent cached results and create a new task.",
    )


class HighFrequencyQuestionPrewarmRequest(BaseModel):
    """Request for scheduler-driven high-frequency question prewarm."""

    source_id: str = Field(default="", max_length=64)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    bbk_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("source_id")
    @classmethod
    def _strip_source_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("bbk_id")
    @classmethod
    def _strip_optional_bbk_id(cls, value: Optional[str]) -> Optional[str]:
        return _strip_optional(value)

    @model_validator(mode="after")
    def _validate_optional_time_range(
        self,
    ) -> "HighFrequencyQuestionPrewarmRequest":
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must be provided together")
        if self.start_time is not None and self.end_time is not None:
            if self.start_time >= self.end_time:
                raise ValueError("start_time must be earlier than end_time")
            if self.end_time.date() - self.start_time.date() > timedelta(
                days=MAX_TASK_QUERY_DAYS,
            ):
                raise ValueError("date range must not exceed 7 days")
        return self


class HighFrequencyQuestionScheduledTaskRequest(BaseModel):
    """Body-only request for scheduler-driven high-frequency question tasks."""

    source_id: str = Field(..., min_length=1, max_length=64)
    bbk_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("source_id")
    @classmethod
    def _strip_required_source_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_id must not be blank")
        return stripped

    @field_validator("bbk_id")
    @classmethod
    def _strip_optional_bbk_id(cls, value: Optional[str]) -> Optional[str]:
        return _strip_optional(value)


class HighFrequencyQuestionTopic(BaseModel):
    """Single high-frequency question topic returned to frontend."""

    rank_no: int
    topic_name: str
    message_count: int
    valid_message_count: int
    skill_used_count: int = 0
    top_skill: Optional[str] = None
    bbk_dis: dict[str, Any] = Field(default_factory=dict)
    sample_questions: list[str] = Field(default_factory=list)


class HighFrequencyQuestionResultQueryResponse(BaseModel):
    """Result lookup response for high-frequency question analysis."""

    state: Literal["AVAILABLE", "AVAILABLE_STALE", "EMPTY"]
    task_id: Optional[str] = None
    batch_id: Optional[str] = None
    status: Optional[str] = None
    source_id: str
    stat_start_time: Optional[datetime] = None
    stat_end_time: Optional[datetime] = None
    scope_type: Optional[Literal["ALL", "ORG"]] = None
    bbk_id: Optional[str] = None
    result_updated_at: Optional[datetime] = None
    message_count: int = 0
    user_count: int = 0
    total_skill_used_count: int = 0
    topic_count: int = 0
    skill_gap_topic_count: int = 0
    topics: list[HighFrequencyQuestionTopic] = Field(default_factory=list)
    message: Optional[str] = None


class HighFrequencyQuestionTaskSubmitResponse(
    HighFrequencyQuestionResultQueryResponse,
):
    """Task submission response.

    Cache hits return an AVAILABLE result. New tasks return RUNNING with task_id
    and no topics.
    """
    

    state: Literal["AVAILABLE", "RUNNING"]
