# -*- coding: utf-8 -*-
"""Body-free discovery responses, separate from complete Chat metadata."""

from datetime import datetime

from pydantic import BaseModel, Field

from .expert_binding import ChatExpertBinding, read_expert_binding

DISPLAY_METADATA_TYPES = {
    "plan_mode_enabled": bool,
    "goal_mode_enabled": bool,
    "session_title_generated": bool,
    "session_kind": str,
    "task_job_id": str,
    "management_owner": str,
    "creator_user_id": str,
    "source_id": str,
    "agent_id": str,
}
MAX_SUMMARY_PAGE_SIZE = 200
MAX_SUMMARY_CURSOR_LENGTH = 2048


def display_metadata(metadata):
    """Project existing light display fields without requesting NAS content."""
    return {
        key: value
        for key, value in metadata.items()
        if type(value) is DISPLAY_METADATA_TYPES.get(key)
        and (not isinstance(value, str) or len(value.encode("utf-8")) <= 1024)
    }


def validate_summary_page(page_size, cursor):
    if not 1 <= page_size <= MAX_SUMMARY_PAGE_SIZE:
        raise ValueError("Summary page_size must be between 1 and 200")
    if cursor is not None and (
        not isinstance(cursor, str) or len(cursor) > MAX_SUMMARY_CURSOR_LENGTH
    ):
        raise ValueError("Invalid chat summary cursor")


class ChatSummary(BaseModel):
    id: str
    session_id: str
    user_id: str
    channel: str
    name: str
    created_at: datetime
    updated_at: datetime
    status: str
    display_metadata: dict = Field(default_factory=dict)
    expert_binding: ChatExpertBinding | None = None

    @classmethod
    def from_chat(cls, chat):
        """Compatibility projection for repositories storing complete records."""
        return cls(
            **chat.model_dump(exclude={"meta"}),
            display_metadata=display_metadata(chat.meta),
            expert_binding=(
                read_expert_binding(chat)
                if "expert_main_binding" in chat.meta
                else None
            ),
        )


class ChatSummaryPage(BaseModel):
    items: list[ChatSummary] = Field(default_factory=list)
    page_size: int = Field(ge=1, le=MAX_SUMMARY_PAGE_SIZE)
    has_more: bool
    next_cursor: str | None = None
