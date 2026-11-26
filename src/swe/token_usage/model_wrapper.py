# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses."""

from contextlib import aclosing
from datetime import date
from typing import Any, AsyncGenerator, Literal, Type

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage
from pydantic import BaseModel

from .manager import get_token_usage_manager
from ..utils.close_aware_stream import CloseAwareStream


class TokenRecordingModelWrapper(ChatModelBase):
    """Wraps a ChatModelBase to record token usage on each call."""

    def __init__(self, provider_id: str, model: ChatModelBase) -> None:
        self._model = model
        super().__init__(
            model_name=getattr(model, "model_name", "unknown"),
            stream=getattr(model, "stream", True),
        )
        self._provider_id = provider_id

    @property
    def stream(self) -> bool:
        return self._model.stream

    @stream.setter
    def stream(self, value: bool) -> None:
        self._model.stream = value

    def _extract_usage(self, result: ChatResponse) -> ChatUsage | None:
        """Extract usage from result using same logic as old version.

        Args:
            result: Model response

        Returns:
            ChatUsage or None
        """
        usage = None

        # 1. Check result.metadata.usage
        metadata = getattr(result, "metadata", None)
        if metadata and isinstance(metadata, dict):
            usage = metadata.get("usage")

        # 2. Check result.usage directly
        if usage is None:
            usage = getattr(result, "usage", None)

        # 3. Try to get from raw response
        if usage is None:
            raw = getattr(result, "raw", None)
            if raw:
                usage = getattr(raw, "usage", None)
                if usage is None and isinstance(raw, dict):
                    usage = raw.get("usage")

        # 4. Try to get from model's _last_usage (fallback)
        if usage is None:
            usage = getattr(self._model, "_last_usage", None)

        return usage

    async def _record_usage(self, usage: ChatUsage | None) -> None:
        if usage is None:
            return
        pt = getattr(usage, "input_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", 0) or 0
        if pt > 0 or ct > 0:
            await get_token_usage_manager().record(
                provider_id=self._provider_id,
                model_name=self.model_name,
                prompt_tokens=pt,
                completion_tokens=ct,
                at_date=date.today(),
            )

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        result = await self._model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )

        if isinstance(result, AsyncGenerator):
            return CloseAwareStream(self._wrap_stream(result), result.aclose)

        # Extract usage using same logic as old version
        usage = self._extract_usage(result)
        await self._record_usage(usage)
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
    ) -> AsyncGenerator[ChatResponse, None]:
        last_usage: ChatUsage | None = None
        async with aclosing(stream):
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    last_usage = chunk.usage
                yield chunk

        # Try model's _last_usage if no usage from stream
        if last_usage is None:
            last_usage = getattr(self._model, "_last_usage", None)

        await self._record_usage(last_usage)

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to wrapped model."""
        return getattr(self._model, name)
