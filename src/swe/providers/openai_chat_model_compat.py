# -*- coding: utf-8 -*-
"""OpenAI chat model compatibility wrappers."""

from __future__ import annotations

import json
from contextlib import aclosing
from contextvars import ContextVar
from datetime import datetime
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Type

from agentscope.model import OpenAIChatModel
from agentscope.model._model_response import ChatResponse
from openai import LengthFinishReasonError
from pydantic import BaseModel

from swe.local_models.tag_parser import (
    parse_tool_calls_from_text,
    text_contains_tool_call_tag,
)
from swe.observability.instrumentation import (
    observe_openai_response,
    record_openai_request,
)
from swe.utils.close_aware_stream import CloseAwareStream

_openai_stream_close: ContextVar[list[Any] | None] = ContextVar(
    "openai_stream_close",
    default=None,
)


class _ObservableOpenAICompletions:
    def __init__(self, completions: Any) -> None:
        self._completions = completions

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)

    async def create(self, **kwargs: Any) -> Any:
        streaming = bool(kwargs.get("stream"))
        record_openai_request(
            streaming=streaming, request_model=kwargs["model"]
        )
        response = await self._completions.create(**kwargs)
        close = getattr(response, "close", None)
        closers = _openai_stream_close.get()
        if streaming and closers is not None and callable(close):

            async def close_response() -> None:
                if not getattr(
                    getattr(response, "response", None), "is_closed", False
                ):
                    await close()

            closers.append(close_response)
        return response

    async def parse(self, **kwargs: Any) -> Any:
        record_openai_request(streaming=False, request_model=kwargs["model"])
        try:
            return await self._completions.parse(**kwargs)
        except LengthFinishReasonError as exc:
            observe_openai_response(exc.completion)
            raise

    def stream(self, **kwargs: Any) -> Any:
        return _ObservableStructuredStream(self._completions, kwargs)


class _ObservableStructuredStream:
    def __init__(self, completions: Any, kwargs: dict[str, Any]) -> None:
        self._completions = completions
        self._kwargs = kwargs
        self._manager = None

    async def __aenter__(self) -> Any:
        self._manager = self._completions.stream(**self._kwargs)
        record_openai_request(
            streaming=True, request_model=self._kwargs["model"]
        )
        return await self._manager.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        return await self._manager.__aexit__(*exc)


def _clone_with_overrides(obj: Any, **overrides: Any) -> Any:
    """Clone a stream object into a mutable namespace with overrides."""
    data = dict(getattr(obj, "__dict__", {}))
    data.update(overrides)
    return SimpleNamespace(**data)


def _sanitize_tool_call(tool_call: Any) -> Any | None:
    """Normalize a tool call for parser safety, or drop it if unusable."""
    if not hasattr(tool_call, "index"):
        return None

    function = getattr(tool_call, "function", None)
    if function is None:
        return None

    has_name = hasattr(function, "name")
    has_arguments = hasattr(function, "arguments")

    raw_name = getattr(function, "name", "")
    if isinstance(raw_name, str):
        safe_name = raw_name
    elif raw_name is None:
        safe_name = ""
    else:
        safe_name = str(raw_name)

    raw_arguments = getattr(function, "arguments", "")
    if isinstance(raw_arguments, str):
        safe_arguments = raw_arguments
    elif raw_arguments is None:
        safe_arguments = ""
    else:
        try:
            safe_arguments = json.dumps(raw_arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            safe_arguments = str(raw_arguments)

    if (
        has_name
        and has_arguments
        and isinstance(raw_name, str)
        and isinstance(
            raw_arguments,
            str,
        )
    ):
        return tool_call

    safe_function = SimpleNamespace(
        name=safe_name,
        arguments=safe_arguments,
    )
    return _clone_with_overrides(tool_call, function=safe_function)


def _sanitize_chunk(chunk: Any) -> Any:
    """Drop/normalize malformed tool-calls in a streaming chunk."""
    choices = getattr(chunk, "choices", None)
    if not choices:
        return chunk

    sanitized_choices: list[Any] = []
    changed = False

    for choice in choices:
        delta = getattr(choice, "delta", None)
        if delta is None:
            sanitized_choices.append(choice)
            continue

        raw_tool_calls = getattr(delta, "tool_calls", None)
        if not raw_tool_calls:
            sanitized_choices.append(choice)
            continue

        choice_changed = False
        sanitized_tool_calls: list[Any] = []
        for tool_call in raw_tool_calls:
            sanitized = _sanitize_tool_call(tool_call)
            if sanitized is not tool_call:
                choice_changed = True
            if sanitized is not None:
                sanitized_tool_calls.append(sanitized)

        if choice_changed:
            changed = True
            sanitized_delta = _clone_with_overrides(
                delta,
                tool_calls=sanitized_tool_calls,
            )
            sanitized_choice = _clone_with_overrides(
                choice,
                delta=sanitized_delta,
            )
            sanitized_choices.append(sanitized_choice)
            continue

        sanitized_choices.append(choice)

    if not changed:
        return chunk
    return _clone_with_overrides(chunk, choices=sanitized_choices)


def _sanitize_stream_item(item: Any) -> Any:
    """Sanitize either plain stream chunks or structured stream items."""
    if hasattr(item, "chunk"):
        chunk = item.chunk
        sanitized_chunk = _sanitize_chunk(chunk)
        if sanitized_chunk is chunk:
            return item
        return _clone_with_overrides(item, chunk=sanitized_chunk)

    return _sanitize_chunk(item)


class _SanitizedStream:
    """Proxy OpenAI async stream that sanitizes each emitted item and
    captures ``extra_content`` from tool-call chunks (used by Gemini
    thinking models to carry ``thought_signature``)."""

    def __init__(self, stream: Any):
        self._stream = stream
        self._ctx_stream: Any | None = None
        self.extra_contents: dict[str, Any] = {}

    async def __aenter__(self) -> "_SanitizedStream":
        self._ctx_stream = await self._stream.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc: Any,
        tb: Any,
    ) -> bool | None:
        return await self._stream.__aexit__(exc_type, exc, tb)

    def __aiter__(self) -> "_SanitizedStream":
        return self

    async def __anext__(self) -> Any:
        if self._ctx_stream is None:
            raise StopAsyncIteration
        try:
            item = await self._ctx_stream.__anext__()
        except LengthFinishReasonError as exc:
            observe_openai_response(exc.completion)
            raise
        observe_openai_response(getattr(item, "chunk", item))
        self._capture_extra_content(item)
        return _sanitize_stream_item(item)

    def _capture_extra_content(self, item: Any) -> None:
        """Store ``extra_content`` keyed by tool-call id."""
        chunk = getattr(item, "chunk", item)
        choices = getattr(chunk, "choices", None) or []
        for choice in choices:
            delta = getattr(choice, "delta", None)
            if not delta:
                continue
            for tc in getattr(delta, "tool_calls", None) or []:
                tc_id = getattr(tc, "id", None)
                if not tc_id:
                    continue
                extra = getattr(tc, "extra_content", None)
                if extra is None:
                    model_extra = getattr(tc, "model_extra", None)
                    if isinstance(model_extra, dict):
                        extra = model_extra.get("extra_content")
                if extra:
                    self.extra_contents[tc_id] = extra


def _filter_placeholder_thinking_blocks(
    content: list[dict[str, Any]],
    strip_leading_think_prefix: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Drop placeholder thinking frames and trim a leading ``<think>``."""
    filtered_content: list[dict[str, Any]] = []

    for block in content:
        if block.get("type") != "thinking":
            filtered_content.append(block)
            continue

        thinking_text = block.get("thinking") or ""
        if thinking_text in ("", "<think>"):
            strip_leading_think_prefix = True
            continue

        if thinking_text.startswith("<think>"):
            strip_leading_think_prefix = True

        if strip_leading_think_prefix and thinking_text.startswith("<think>"):
            thinking_text = thinking_text.removeprefix("<think>")

        if not thinking_text:
            continue

        block["thinking"] = thinking_text
        filtered_content.append(block)

    return filtered_content, strip_leading_think_prefix


def _attach_tool_extra_content(
    content: list[dict[str, Any]],
    extra_contents: dict[str, Any],
) -> None:
    """Attach captured tool-call extra content to parsed blocks."""
    if not extra_contents:
        return

    for block in content:
        if block.get("type") != "tool_use":
            continue
        tool_id = block.get("id")
        if not isinstance(tool_id, str):
            continue
        extra_content = extra_contents.get(tool_id)
        if extra_content:
            block["extra_content"] = extra_content


def _build_tool_use_blocks(
    tool_calls: list[Any],
    key_prefix: str,
    id_prefix: str,
) -> dict[str, dict]:
    """Convert parsed tool calls into stable tool-use blocks."""
    return {
        f"{key_prefix}_{index}": {
            "type": "tool_use",
            "id": f"{id_prefix}_{index}",
            "name": tool_call.name,
            "input": tool_call.arguments,
            "raw_input": tool_call.raw_arguments,
        }
        for index, tool_call in enumerate(tool_calls)
    }


def _extract_tool_calls_from_blocks(
    content: list[dict[str, Any]],
    block_type: str,
    text_key: str,
    key_prefix: str,
    id_prefix: str,
    drop_empty_blocks: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """Extract tagged tool calls from matching blocks."""
    extracted: dict[str, dict] = {}
    updated_content: list[dict[str, Any] | None] | None = None

    for index, block in enumerate(content):
        if block.get("type") != block_type:
            continue
        text = block.get(text_key) or ""
        if not text_contains_tool_call_tag(text):
            continue

        parsed = parse_tool_calls_from_text(text)
        clean_text = parsed.text_before.strip()
        block[text_key] = clean_text

        if parsed.tool_calls:
            extracted = _build_tool_use_blocks(
                parsed.tool_calls,
                key_prefix=key_prefix,
                id_prefix=id_prefix,
            )

        if drop_empty_blocks and not clean_text:
            if updated_content is None:
                updated_content = list(content)
            updated_content[index] = None

    if updated_content is None:
        return content, extracted
    return [block for block in updated_content if block is not None], extracted


def _extract_tagged_tool_call_blocks(
    content: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict], dict[str, dict]]:
    """Extract tool-call tags embedded in thinking or text blocks."""
    content, think_tool_calls = _extract_tool_calls_from_blocks(
        content,
        block_type="thinking",
        text_key="thinking",
        key_prefix="thinking",
        id_prefix="think_call",
    )
    content, text_tool_calls = _extract_tool_calls_from_blocks(
        content,
        block_type="text",
        text_key="text",
        key_prefix="text",
        id_prefix="text_call",
        drop_empty_blocks=True,
    )

    extra = list(think_tool_calls.values()) + list(text_tool_calls.values())
    if extra:
        content = list(content) + extra

    return content, think_tool_calls, text_tool_calls


class OpenAIChatModelCompat(OpenAIChatModel):
    """OpenAIChatModel with robust parsing for malformed tool-call chunks
    and transparent ``extra_content`` (Gemini thought_signature) relay."""

    observes_provider_requests = True

    @property
    def client(self) -> Any:
        return self._client

    @client.setter
    def client(self, client: Any) -> None:
        chat = client.chat
        completions = chat.completions
        if not isinstance(completions, _ObservableOpenAICompletions):
            chat.completions = _ObservableOpenAICompletions(completions)
        self._client = client

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        closers: list[Any] = []
        token = _openai_stream_close.set(closers)
        try:
            result = await super().__call__(*args, **kwargs)
        except BaseException:
            if closers:
                try:
                    await closers[0]()
                except Exception:
                    pass
            raise
        finally:
            _openai_stream_close.reset(token)
        if not closers or not isinstance(result, AsyncGenerator):
            return result

        async def consume():
            try:
                async with aclosing(result):
                    async for chunk in result:
                        yield chunk
            finally:
                try:
                    await closers[0]()
                except Exception:
                    pass

        return CloseAwareStream(consume(), closers[0])

    def _parse_openai_completion_response(
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> ChatResponse:
        observe_openai_response(response)
        return super()._parse_openai_completion_response(
            start_datetime,
            response,
            structured_model,
        )

    # pylint: disable=too-many-branches
    async def _parse_openai_stream_response(
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        sanitized_response = _SanitizedStream(response)

        # Stable tag-extracted tool-call blocks across streaming chunks.
        # Keyed by positional strings so IDs stay consistent as chunks
        # accumulate.  Two sources: "thinking" blocks and plain "text" blocks.
        _think_tool_calls: dict[str, dict] = {}
        _text_tool_calls: dict[str, dict] = {}
        _strip_leading_think_prefix = False

        responses = super()._parse_openai_stream_response(
            start_datetime=start_datetime,
            response=sanitized_response,
            structured_model=structured_model,
        )
        async with aclosing(responses):
            async for parsed in responses:
                (
                    parsed.content,
                    _strip_leading_think_prefix,
                ) = _filter_placeholder_thinking_blocks(
                    parsed.content,
                    _strip_leading_think_prefix,
                )
                if not parsed.content:
                    continue

                _attach_tool_extra_content(
                    parsed.content,
                    sanitized_response.extra_contents,
                )
                if any(b.get("type") == "tool_use" for b in parsed.content):
                    _think_tool_calls.clear()
                    _text_tool_calls.clear()
                else:
                    (
                        parsed.content,
                        _think_tool_calls,
                        _text_tool_calls,
                    ) = _extract_tagged_tool_call_blocks(parsed.content)

                yield parsed
