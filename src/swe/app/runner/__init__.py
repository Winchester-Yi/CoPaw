# -*- coding: utf-8 -*-
"""Runner module with chat manager for coordinating repository."""

from __future__ import annotations

__all__ = [
    "AgentRunner",
    "BaseChatRepository",
    "ChatHistory",
    "ChatManager",
    "ChatsFile",
    "ChatSpec",
    "JsonChatRepository",
    "router",
]


def __getattr__(name: str):
    if name == "AgentRunner":
        from .runner import AgentRunner as _AgentRunner

        return _AgentRunner
    if name == "router":
        from .api import router as _router

        return _router
    if name == "ChatManager":
        from .manager import ChatManager as _ChatManager

        return _ChatManager
    if name in {"ChatSpec", "ChatHistory", "ChatsFile"}:
        from .models import (
            ChatHistory as _ChatHistory,
            ChatsFile as _ChatsFile,
            ChatSpec as _ChatSpec,
        )

        exports = {
            "ChatSpec": _ChatSpec,
            "ChatHistory": _ChatHistory,
            "ChatsFile": _ChatsFile,
        }
        return exports[name]
    if name in {"BaseChatRepository", "JsonChatRepository"}:
        from .repo import (
            BaseChatRepository as _BaseChatRepository,
            JsonChatRepository as _JsonChatRepository,
        )

        exports = {
            "BaseChatRepository": _BaseChatRepository,
            "JsonChatRepository": _JsonChatRepository,
        }
        return exports[name]
    raise AttributeError(name)
