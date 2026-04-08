# -*- coding: utf-8 -*-
"""Model wrapper for tracing LLM calls.

Provides TracingModelWrapper that intercepts LLM calls to record tracing events.
"""
import logging
from typing import Any, AsyncGenerator, Optional, Sequence, Union

from agentscope.model._model_response import ChatResponse

from .manager import get_trace_manager, get_current_trace

logger = logging.getLogger(__name__)


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
        trace_ctx = get_current_trace()
        if trace_ctx is None:
            return await self._call_model(
                messages,
                tools,
                tool_choice,
                **kwargs,
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
                user_id=trace_ctx.user_id,
                session_id=trace_ctx.session_id,
                channel=trace_ctx.channel,
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

        Args:
            result: Model response

        Returns:
            Tuple of (input_tokens, output_tokens)
        """
        input_tokens = 0
        output_tokens = 0

        # Try to get usage from result
        usage = getattr(result, "usage", None)
        if usage is None:
            # Try to get from raw response
            raw = getattr(result, "raw", None)
            if raw:
                usage = getattr(raw, "usage", None)
                if usage is None and isinstance(raw, dict):
                    usage = raw.get("usage")

        if usage:
            # Handle different usage formats
            if hasattr(usage, "input_tokens"):
                input_tokens = usage.input_tokens or 0
            elif isinstance(usage, dict):
                input_tokens = usage.get(
                    "input_tokens",
                    usage.get("prompt_tokens", 0),
                )

            if hasattr(usage, "output_tokens"):
                output_tokens = usage.output_tokens or 0
            elif isinstance(usage, dict):
                output_tokens = usage.get(
                    "output_tokens",
                    usage.get("completion_tokens", 0),
                )

        return input_tokens, output_tokens

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to wrapped model."""
        return getattr(self._model, name)

    # Support for streaming responses
    async def stream(
        self,
        messages: Sequence[dict],
        tools: Optional[Sequence[dict]] = None,
        tool_choice: Optional[Union[str, dict]] = None,
        **kwargs: Any,
    ) -> AsyncGenerator:
        """Stream from the wrapped model with tracing.

        Args:
            messages: Chat messages
            tools: Optional tools for function calling
            tool_choice: Optional tool choice
            **kwargs: Additional arguments

        Yields:
            Chunks from the model stream
        """
        # Check if tracing is enabled
        try:
            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                async for chunk in self._model.stream(
                    messages,
                    tools,
                    tool_choice,
                    **kwargs,
                ):
                    yield chunk
                return
        except RuntimeError:
            async for chunk in self._model.stream(
                messages,
                tools,
                tool_choice,
                **kwargs,
            ):
                yield chunk
            return

        # Get trace context
        trace_ctx = get_current_trace()
        if trace_ctx is None:
            async for chunk in self._model.stream(
                messages,
                tools,
                tool_choice,
                **kwargs,
            ):
                yield chunk
            return

        # Emit LLM_INPUT event
        span_id = await self._emit_llm_start(trace_ctx, trace_mgr)

        last_usage = None
        try:
            async for chunk in self._model.stream(
                messages,
                tools,
                tool_choice,
                **kwargs,
            ):
                # Track usage from stream chunks
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    last_usage = chunk_usage
                yield chunk

            # Extract final usage
            input_tokens, output_tokens = self._extract_stream_tokens(
                last_usage,
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

        except Exception as e:
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

    def _extract_stream_tokens(self, usage: Any) -> tuple[int, int]:
        """Extract token counts from stream usage."""
        if usage is None:
            return 0, 0

        input_tokens = 0
        output_tokens = 0

        if hasattr(usage, "input_tokens"):
            input_tokens = usage.input_tokens or 0
        elif isinstance(usage, dict):
            input_tokens = usage.get(
                "input_tokens",
                usage.get("prompt_tokens", 0),
            )

        if hasattr(usage, "output_tokens"):
            output_tokens = usage.output_tokens or 0
        elif isinstance(usage, dict):
            output_tokens = usage.get(
                "output_tokens",
                usage.get("completion_tokens", 0),
            )

        return input_tokens, output_tokens
