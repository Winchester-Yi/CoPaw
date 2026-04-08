# -*- coding: utf-8 -*-
"""Registry for SWE-local chat model classes."""

from __future__ import annotations

from typing import Type

from agentscope.model import ChatModelBase

OPENAI_COMPATIBLE_CHAT_MODELS = frozenset(
    {
        "OpenAIChatModel",
        "KimiChatModel",
    },
)


def is_openai_compatible_chat_model(chat_model: str) -> bool:
    """Return whether *chat_model* uses the OpenAI-compatible provider path."""
    return chat_model in OPENAI_COMPATIBLE_CHAT_MODELS


def get_local_chat_model_cls(
    chat_model: str,
) -> Type[ChatModelBase] | None:
    """Resolve a SWE-local chat model class by name."""
    if chat_model == "KimiChatModel":
        from .kimi_chat_model import KimiChatModel

        return KimiChatModel
    return None
