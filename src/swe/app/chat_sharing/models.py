# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatShareRecord(BaseModel):
    token: str
    chat_id: str
    creator_id: str
    snapshot_key: str
    tenant_id: str = "default"
    created_at: datetime
    access_count: int = 0
    last_access_at: datetime | None = None


class ChatShareSnapshot(BaseModel):
    chat_name: str = "分享的会话"
    messages: list[dict[str, Any]] = Field(default_factory=list)


class ChatShareOptions(BaseModel):
    """Authenticated history and per-turn status used by the selector."""

    chat_name: str = "分享的会话"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    turn_statuses: dict[str, str] = Field(default_factory=dict)
