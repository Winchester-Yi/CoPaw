# -*- coding: utf-8 -*-
"""Model wrapper for tracing LLM calls.

Provides TracingModelWrapper that intercepts LLM calls to record tracing events.
"""

import asyncio
import logging
from contextlib import aclosing
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Optional, Sequence, Union

from agentscope.model._model_response import ChatResponse

from ..utils.close_aware_stream import CloseAwareStream
from .manager import get_trace_manager, get_current_trace

logger = logging.getLogger(__name__)


def _current_task_label() -> str:
    """返回当前 asyncio task 的诊断标识。"""
    task = asyncio.current_task()
    if task is None:
        return "no-task"
    return f"task-{id(task)}"


class TracingModelWrapper:
    """Wrapper that records tracing events for LLM calls.

    This wrapper intercepts LLM calls to:
    1. Record LLM_INPUT event at call start
    2. Record LLM_OUTPUT event at call end
    3. Track token usage and latency
    """

    def __init__(
        self,
        provider_id: str,
        model: Any,
        trace_context: Optional[dict[str, Any]] = None,
    ):
        """Initialize tracing model wrapper.

        Args:
            provider_id: Provider identifier (e.g., "dashscope", "openai")
            model: The underlying ChatModelBase to wrap
        """
        self.provider_id = provider_id
        self._model = model
        self._model_name = getattr(model, "model_name", None) or getattr(
            model,
            "config",
            {},
        ).get("model_name", "unknown")
        self._trace_context = dict(trace_context or {})

    @property
    def model_name(self) -> str:
        """Get model name."""
        return self._model_name

    @property
    def config(self) -> dict:
        """Get model config."""
        return getattr(self._model, "config", {})

    async def __call__(
        self,
        messages: Sequence[dict],
        tools: Optional[Sequence[dict]] = None,
        tool_choice: Optional[Union[str, dict]] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Call the wrapped model and record tracing events.

        Args:
            messages: Chat messages
            tools: Optional tools for function calling
            tool_choice: Optional tool choice
            **kwargs: Additional arguments

        Returns:
            ChatResponse from the wrapped model
        """
        # Check if tracing is enabled
        try:
            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                return await self._call_model(
                    messages,
                    tools,
                    tool_choice,
                    **kwargs,
                )
        except RuntimeError:
            # Tracing not initialized
            return await self._call_model(
                messages,
                tools,
                tool_choice,
                **kwargs,
            )

        # Get trace context
        trace_ctx = self._resolve_trace_context()
        if trace_ctx is None:
            # 记录缺少 trace context 的情况，帮助排查 token 为 0 的问题
            logger.warning(
                "TracingModelWrapper: no trace context, skipping tracing. "
                "This may result in token=0 in database. "
                "provider=%s, model=%s",
                self.provider_id,
                self._model_name,
            )
            return await self._call_model(
                messages,
                tools,
                tool_choice,
                **kwargs,
            )

        logger.debug(
            "TracingModelWrapper preparing LLM span: trace_id=%s "
            "user_id=%s session_id=%s source_id=%s task=%s provider=%s "
            "model=%s",
            trace_ctx.trace_id,
            trace_ctx.user_id,
            trace_ctx.session_id,
            trace_ctx.source_id,
            _current_task_label(),
            self.provider_id,
            self._model_name,
        )

        # Emit LLM_INPUT event
        span_id = await self._emit_llm_start(trace_ctx, trace_mgr)

        try:
            # Call the actual model
            result = await self._call_model(
                messages,
                tools,
                tool_choice,
                **kwargs,
            )

            # Handle streaming response
            if isinstance(result, AsyncGenerator):
                if span_id:
                    return CloseAwareStream(
                        self._wrap_stream(
                            result,
                            trace_ctx,
                            trace_mgr,
                            span_id,
                        ),
                        result.aclose,
                    )
                # No span_id, just return the stream without tracing
                return result

            # Extract token usage
            input_tokens, output_tokens = self._extract_tokens(result)

            # Emit LLM_OUTPUT event
            if span_id:
                await self._emit_llm_end(
                    trace_ctx,
                    trace_mgr,
                    span_id,
                    input_tokens,
                    output_tokens,
                )

            return result

        except Exception as e:
            # Record error in trace
            if span_id:
                try:
                    await trace_mgr.update_span(
                        span_id=span_id,
                        trace_id=trace_ctx.trace_id,
                        error=str(e),
                    )
                except Exception as trace_error:
                    logger.warning(
                        "Failed to record error in trace: %s",
                        trace_error,
                    )
            raise

    def _resolve_trace_context(self) -> Any | None:
        """优先使用请求绑定的 trace 上下文，缺失时回退到 ContextVar。"""
        bound_trace_id = str(self._trace_context.get("trace_id") or "")
        current_trace = get_current_trace()
        if not bound_trace_id:
            return current_trace

        if current_trace is not None and getattr(
            current_trace,
            "trace_id",
            None,
        ) not in {None, bound_trace_id}:
            logger.warning(
                "TracingModelWrapper detected mismatched current trace; "
                "using bound trace context instead. current=%s bound=%s task=%s",
                getattr(current_trace, "trace_id", None),
                bound_trace_id,
                _current_task_label(),
            )

        return SimpleNamespace(
            trace_id=bound_trace_id,
            user_id=str(self._trace_context.get("user_id") or ""),
            session_id=str(self._trace_context.get("session_id") or ""),
            channel=str(self._trace_context.get("channel") or ""),
            source_id=str(self._trace_context.get("source_id") or ""),
            user_name=self._trace_context.get("user_name"),
            bbk_id=self._trace_context.get("bbk_id"),
        )

    async def _wrap_stream(
        self,
        stream: AsyncGenerator,
        trace_ctx,
        trace_mgr,
        span_id: str,
    ) -> AsyncGenerator[ChatResponse, None]:
        """Wrap streaming response to collect token usage."""
        last_usage = None
        chunk_count = 0
        async with aclosing(stream):
            async for chunk in stream:
                chunk_count += 1
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    last_usage = chunk_usage
                yield chunk

        # Extract tokens from stream usage
        input_tokens, output_tokens = self._extract_stream_tokens(last_usage)

        # 调试日志：帮助排查 token 为 0 的问题
        if input_tokens == 0 and output_tokens == 0:
            logger.debug(
                "Stream token extraction: no usage found. "
                "chunks=%d, last_usage=%s, span_id=%s",
                chunk_count,
                type(last_usage).__name__ if last_usage else None,
                span_id,
            )
        else:
            logger.debug(
                "Stream token extraction: input=%d, output=%d, span_id=%s",
                input_tokens,
                output_tokens,
                span_id,
            )

        # Emit LLM_OUTPUT event
        if span_id:
            await self._emit_llm_end(
                trace_ctx,
                trace_mgr,
                span_id,
                input_tokens,
                output_tokens,
            )

    def _extract_stream_tokens(self, usage: Any) -> tuple[int, int]:
        """Extract token counts from stream usage.

        AgentScope 在流式响应的最后一个 chunk 中返回 usage，
        格式为 ChatUsage(input_tokens, output_tokens, time)。
        OpenAI 原始响应中 usage 字段名为 prompt_tokens / completion_tokens。
        """
        input_tokens = 0
        output_tokens = 0

        if usage:
            # ChatUsage 对象 (AgentScope 格式)
            if hasattr(usage, "input_tokens"):
                input_tokens = usage.input_tokens or 0
            elif hasattr(usage, "prompt_tokens"):
                # OpenAI 原生格式
                input_tokens = usage.prompt_tokens or 0
            elif isinstance(usage, dict):
                input_tokens = usage.get(
                    "input_tokens",
                    usage.get("prompt_tokens", 0),
                )

            if hasattr(usage, "output_tokens"):
                output_tokens = usage.output_tokens or 0
            elif hasattr(usage, "completion_tokens"):
                # OpenAI 原生格式
                output_tokens = usage.completion_tokens or 0
            elif isinstance(usage, dict):
                output_tokens = usage.get(
                    "output_tokens",
                    usage.get("completion_tokens", 0),
                )

        return input_tokens, output_tokens

    async def _call_model(
        self,
        messages: Sequence[dict],
        tools: Optional[Sequence[dict]] = None,
        tool_choice: Optional[Union[str, dict]] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Call the wrapped model."""
        return await self._model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

    async def _emit_llm_start(
        self,
        trace_ctx,
        trace_mgr,
    ) -> Optional[str]:
        """Emit LLM start event."""
        try:
            return await trace_mgr.emit_llm_input(
                trace_id=trace_ctx.trace_id,
                model_name=f"{self.provider_id}:{self._model_name}",
                input_tokens=0,  # Will be updated after call
                source_id=trace_ctx.source_id,
                user_id=trace_ctx.user_id,
                session_id=trace_ctx.session_id,
                channel=trace_ctx.channel,
                user_name=trace_ctx.user_name,
                bbk_id=trace_ctx.bbk_id,
            )
        except Exception as e:
            logger.warning("Failed to emit LLM start event: %s", e)
            return None

    async def _emit_llm_end(
        self,
        trace_ctx,
        trace_mgr,
        span_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Emit LLM end event."""
        try:
            await trace_mgr.emit_llm_output(
                trace_id=trace_ctx.trace_id,
                span_id=span_id,
                output_tokens=output_tokens,
                input_tokens=input_tokens,
            )
        except Exception as e:
            logger.warning("Failed to emit LLM end event: %s", e)

    def _extract_tokens(self, result: ChatResponse) -> tuple[int, int]:
        """Extract token counts from model response.

        尝试从多个位置获取 usage 信息：
        1. result.metadata.usage (AgentScope 格式)
        2. result.usage (AgentScope ChatUsage)
        3. result.raw.usage (OpenAI 原生响应)

        支持两种字段命名：
        - AgentScope: input_tokens / output_tokens
        - OpenAI: prompt_tokens / completion_tokens
        """
        input_tokens = 0
        output_tokens = 0
        usage = None

        # 1. Check result.metadata.usage (AgentScope 格式)
        metadata = getattr(result, "metadata", None)
        if metadata and isinstance(metadata, dict):
            usage = metadata.get("usage")

        # 2. Check result.usage directly (AgentScope ChatUsage)
        if usage is None:
            usage = getattr(result, "usage", None)

        # 3. Try to get from raw response (OpenAI 原生)
        if usage is None:
            raw = getattr(result, "raw", None)
            if raw:
                usage = getattr(raw, "usage", None)
                if usage is None and isinstance(raw, dict):
                    usage = raw.get("usage")

        if not usage:
            return 0, 0

        # Handle different usage formats
        # AgentScope ChatUsage 格式
        if hasattr(usage, "input_tokens"):
            input_tokens = usage.input_tokens or 0
        # OpenAI 原生格式
        elif hasattr(usage, "prompt_tokens"):
            input_tokens = usage.prompt_tokens or 0
        elif isinstance(usage, dict):
            input_tokens = usage.get(
                "input_tokens",
                usage.get("prompt_tokens", 0),
            )

        # AgentScope ChatUsage 格式
        if hasattr(usage, "output_tokens"):
            output_tokens = usage.output_tokens or 0
        # OpenAI 原生格式
        elif hasattr(usage, "completion_tokens"):
            output_tokens = usage.completion_tokens or 0
        elif isinstance(usage, dict):
            output_tokens = usage.get(
                "output_tokens",
                usage.get("completion_tokens", 0),
            )

        return input_tokens, output_tokens

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to wrapped model."""
        return getattr(self._model, name)

    @property
    def stream(self) -> bool:
        return self._model.stream

    @stream.setter
    def stream(self, value: bool) -> None:
        self._model.stream = value
