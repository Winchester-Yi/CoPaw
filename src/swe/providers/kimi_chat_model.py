# -*- coding: utf-8 -*-
"""Kimi-specific OpenAI-compatible chat model helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncGenerator, Type

from agentscope.model._model_response import ChatResponse
from pydantic import BaseModel

from swe.local_models.tag_parser import (
    extract_thinking_from_text,
    normalize_thinking_prefix,
    strip_think_tags,
    text_contains_think_tag,
)

from .openai_chat_model_compat import OpenAIChatModelCompat


class KimiChatModel(OpenAIChatModelCompat):
    """OpenAI-compatible model with Kimi think-tag normalization."""

    async def _iter_base_stream_responses(
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        async for parsed in super()._parse_openai_stream_response(
            start_datetime=start_datetime,
            response=response,
            structured_model=structured_model,
        ):
            yield parsed

    def _normalize_kimi_response(
        self,
        parsed: ChatResponse,
    ) -> ChatResponse:
        normalized_content: list[dict[str, Any]] = []

        for block in parsed.content:
            if block.get("type") == "thinking":
                thinking = normalize_thinking_prefix(
                    block.get("thinking") or "",
                )
                if thinking:
                    normalized_content.append(
                        {
                            **block,
                            "thinking": thinking,
                        },
                    )
                continue

            if block.get("type") != "text":
                normalized_content.append(block)
                continue

            text = block.get("text") or ""
            if not text_contains_think_tag(text):
                normalized_content.append(block)
                continue

            parsed_thinking = extract_thinking_from_text(text)
            thinking = normalize_thinking_prefix(parsed_thinking.thinking)
            remaining_text = strip_think_tags(
                parsed_thinking.remaining_text,
            ).strip()

            if thinking:
                normalized_content.append(
                    {
                        "type": "thinking",
                        "thinking": thinking,
                    },
                )
            if remaining_text:
                normalized_content.append(
                    {
                        **block,
                        "text": remaining_text,
                    },
                )

        parsed.content = normalized_content
        return parsed

    async def _iter_kimi_stream_responses(
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        async for parsed in self._iter_base_stream_responses(
            start_datetime=start_datetime,
            response=response,
            structured_model=structured_model,
        ):
            yield self._normalize_kimi_response(parsed)

    async def _parse_openai_stream_response(
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        async for parsed in self._iter_kimi_stream_responses(
            start_datetime=start_datetime,
            response=response,
            structured_model=structured_model,
        ):
            yield parsed
