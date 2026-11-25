# -*- coding: utf-8 -*-
"""Chat models for runner with UUID management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from pydantic import BaseModel, Field
from agentscope_runtime.engine.schemas.agent_schemas import Message

from ..channels.schema import DEFAULT_CHANNEL

MANUAL_CHAT_TITLE_META_KEY = "session_title_edited"


class ChatSpec(BaseModel):
    """Chat specification with UUID identifier.

    Stored in Redis and can be persisted in JSON file.
    """

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Chat UUID identifier",
    )
    name: str = Field(default="New Chat", description="Chat name")
    session_id: str = Field(
        ...,
        description="Session identifier (channel:user_id format)",
    )
    user_id: str = Field(..., description="User identifier")
    channel: str = Field(default=DEFAULT_CHANNEL, description="Channel name")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Chat creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Chat last update timestamp",
    )
    meta: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata",
    )
    status: str = Field(
        default="idle",
        description="Conversation status: idle, running, or stopping",
    )


class ChatMessage(Message):
    """Chat message returned by the chat detail API."""

    timestamp: str | None = Field(
        default=None,
        description="Canonical backend-provided message timestamp",
    )


class ChatCompactionBoundary(BaseModel):
    """One visible divider anchored to an archived compaction batch."""

    id: str
    archived_message_count: int
    first_message_id: str
    last_message_id: str
    created_at: str
    first_timestamp: str | None = None
    last_timestamp: str | None = None


class ChatArchiveMetadata(BaseModel):
    """Archive availability included with the current-memory chat detail."""

    has_more: bool = False
    boundaries: list[ChatCompactionBoundary] = Field(default_factory=list)


class ChatArchivePage(ChatArchiveMetadata):
    """One page of archived conversation messages."""

    messages: list[ChatMessage] = Field(default_factory=list)
    next_cursor: str | None = None


class ChatHistory(BaseModel):
    """Complete chat view with spec and state."""

    chat: ChatSpec | None = Field(
        default=None,
        description="Chat metadata for direct detail loading",
    )
    messages: list[ChatMessage] = Field(default_factory=list)
    status: str = Field(
        default="idle",
        description="Conversation status: idle, running, or stopping",
    )
    turn_status: str | None = Field(
        default=None,
        description="Terminal status for the requested answer turn",
    )
    archive: ChatArchiveMetadata = Field(default_factory=ChatArchiveMetadata)


class ChatPage(BaseModel):
    """Paginated chat list response."""

    items: list[ChatSpec] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    has_more: bool
    next_cursor: str | None = None


class ChatsFile(BaseModel):
    """Chat registry file for JSON repository.

    Stores chat_id (UUID) -> session_id mappings for persistence.
    """

    version: int = 1
    chats: list[ChatSpec] = Field(default_factory=list)
