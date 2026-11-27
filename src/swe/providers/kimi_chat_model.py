# -*- coding: utf-8 -*-
"""Kimi-specific OpenAI-compatible chat model helpers."""

from __future__ import annotations

from contextlib import aclosing
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

    def _prepend_open_think_tag_to_first_frame(
        self,
        parsed: ChatResponse,
    ) -> None:
        """Assume Kimi omitted ``<think>`` on the current reasoning snapshot."""
        if not parsed.content:
            return

        for block in parsed.content:
            if block.get("type") == "thinking":
                return

        for block in parsed.content:
            if block.get("type") != "text":
                continue

            text = block.get("text") or ""
            if not text.strip() or text_contains_think_tag(text):
                return

            block["text"] = f"<think>{text}"
            return

    def _content_has_thinking_started(self, parsed: ChatResponse) -> bool:
        """Return True once a snapshot already carries thinking markers."""
        for block in parsed.content:
            if block.get("type") == "thinking":
                return True
            if block.get("type") != "text":
                continue
            if text_contains_think_tag(block.get("text") or ""):
                return True
        return False

    async def _iter_base_stream_responses(
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        responses = super()._parse_openai_stream_response(
            start_datetime=start_datetime,
            response=response,
            structured_model=structured_model,
        )
        async with aclosing(responses):
            async for parsed in responses:
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
        should_inject_open_think = True

        responses = self._iter_base_stream_responses(
            start_datetime=start_datetime,
            response=response,
            structured_model=structured_model,
        )
        async with aclosing(responses):
            async for parsed in responses:
                source_has_thinking_started = (
                    self._content_has_thinking_started(
                        parsed,
                    )
                )

                if should_inject_open_think:
                    self._prepend_open_think_tag_to_first_frame(parsed)

                if source_has_thinking_started:
                    should_inject_open_think = False

                yield self._normalize_kimi_response(parsed)

    # Return the close-aware stream before its first iteration.
    def _parse_openai_stream_response(  # pylint: disable=invalid-overridden-method
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        return self._iter_kimi_stream_responses(
            start_datetime,
            response,
            structured_model,
        )
