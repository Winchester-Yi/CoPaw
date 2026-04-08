# -*- coding: utf-8 -*-
"""Tracing hook for CoPaw Agent.

This hook integrates tracing events into the agent's lifecycle,
capturing LLM calls, tool executions, and skill invocations.
"""
import logging
from typing import Any, Optional

from ...tracing import get_trace_manager, has_trace_manager

logger = logging.getLogger(__name__)


class TracingHook:
    """Hook for capturing tracing events during agent execution.

    This hook integrates with the agent's pre/post reasoning hooks
    to capture:
    - LLM input/output events
    - Tool call start/end events
    - Skill invocation events
    """

    def __init__(
        self,
        trace_id: str,
        user_id: str,
        session_id: str,
        channel: str,
    ):
        """Initialize tracing hook.

        Args:
            trace_id: Trace identifier
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
        """
        self.trace_id = trace_id
        self.user_id = user_id
        self.session_id = session_id
        self.channel = channel
        self._current_llm_span_id: Optional[str] = None
        self._current_tool_span_id: Optional[str] = None
        self._tool_spans: dict[str, str] = {}  # tool_call_id -> span_id
        self._in_skill_context: bool = (
            False  # Flag to skip internal tool tracing during skill execution
        )

    async def on_llm_start(
        self,
        model_name: str,
        input_tokens: int = 0,
    ) -> Optional[str]:
        """Called when LLM starts generating.

        Args:
            model_name: Model name
            input_tokens: Input token count

        Returns:
            Span ID
        """
        if not has_trace_manager():
            return None

        try:
            manager = get_trace_manager()
            span_id = await manager.emit_llm_input(
                trace_id=self.trace_id,
                model_name=model_name,
                input_tokens=input_tokens,
                user_id=self.user_id,
                session_id=self.session_id,
                channel=self.channel,
            )
            self._current_llm_span_id = span_id
            return span_id
        except Exception as e:
            logger.warning("Failed to emit LLM start event: %s", e)
            return None

    async def on_llm_end(
        self,
        output_tokens: int = 0,
        input_tokens: int = 0,
    ) -> None:
        """Called when LLM finishes generating.

        Args:
            output_tokens: Output token count
            input_tokens: Input token count (updated if provided)
        """
        if not self._current_llm_span_id:
            return

        if not has_trace_manager():
            return

        try:
            manager = get_trace_manager()
            await manager.emit_llm_output(
                trace_id=self.trace_id,
                span_id=self._current_llm_span_id,
                output_tokens=output_tokens,
                input_tokens=input_tokens,
            )
            self._current_llm_span_id = None
        except Exception as e:
            logger.warning("Failed to emit LLM end event: %s", e)

    async def on_tool_start(
        self,
        tool_name: str,
        tool_input: Optional[dict[str, Any]],
        tool_call_id: Optional[str] = None,
        mcp_server: Optional[str] = None,
    ) -> str:
        """Called when a tool starts executing.

        Args:
            tool_name: Tool name
            tool_input: Tool input
            tool_call_id: Optional tool call ID for tracking
            mcp_server: Optional MCP server name if this is an MCP tool

        Returns:
            Span ID (empty string if skipped)
        """
        # Skip tool tracing when inside a skill execution
        if self._in_skill_context:
            logger.debug(
                "Skipping tool '%s' tracing (inside skill context)",
                tool_name,
            )
            return ""

        if not has_trace_manager():
            return ""

        try:
            manager = get_trace_manager()
            span_id = await manager.emit_tool_call_start(
                trace_id=self.trace_id,
                tool_name=tool_name,
                tool_input=tool_input,
                user_id=self.user_id,
                session_id=self.session_id,
                channel=self.channel,
                mcp_server=mcp_server,
            )
            if tool_call_id:
                self._tool_spans[tool_call_id] = span_id
            self._current_tool_span_id = span_id
            return span_id
        except Exception as e:
            logger.warning("Failed to emit tool start event: %s", e)
            return ""

    async def on_tool_end(
        self,
        tool_output: Optional[str],
        tool_call_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Called when a tool finishes executing.

        Args:
            tool_output: Tool output
            tool_call_id: Optional tool call ID for tracking
            error: Optional error message
        """
        # Find span ID
        span_id = None
        if tool_call_id:
            span_id = self._tool_spans.pop(tool_call_id, None)
        if not span_id:
            span_id = self._current_tool_span_id

        if not span_id:
            return

        if not has_trace_manager():
            return

        try:
            manager = get_trace_manager()
            await manager.emit_tool_call_end(
                trace_id=self.trace_id,
                span_id=span_id,
                tool_output=tool_output,
                error=error,
            )
            if span_id == self._current_tool_span_id:
                self._current_tool_span_id = None
        except Exception as e:
            logger.warning("Failed to emit tool end event: %s", e)

    async def on_skill_start(
        self,
        skill_name: str,
        skill_input: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """Called when a skill starts executing.

        Args:
            skill_name: Skill name
            skill_input: Skill input parameters

        Returns:
            Span ID
        """
        # Set skill context flag to skip internal tool tracing
        self._in_skill_context = True

        if not has_trace_manager():
            return None

        try:
            manager = get_trace_manager()
            span_id = await manager.emit_skill_invocation(
                trace_id=self.trace_id,
                skill_name=skill_name,
                user_id=self.user_id,
                session_id=self.session_id,
                channel=self.channel,
                skill_input=skill_input,
            )
            self._current_tool_span_id = span_id  # Reuse for skill tracking
            return span_id
        except Exception as e:
            logger.warning("Failed to emit skill event: %s", e)
            return None

    async def on_skill_end(
        self,
        skill_output: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Called when a skill finishes executing.

        Args:
            skill_output: Skill output
            error: Optional error message
        """
        # Clear skill context flag
        self._in_skill_context = False

        if not self._current_tool_span_id:
            return

        if not has_trace_manager():
            return

        try:
            manager = get_trace_manager()
            await manager.end_skill_invocation(
                trace_id=self.trace_id,
                span_id=self._current_tool_span_id,
                skill_output=skill_output,
                error=error,
            )
            self._current_tool_span_id = None
        except Exception as e:
            logger.warning("Failed to emit skill end event: %s", e)

    async def __call__(
        self,
        agent,
        kwargs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Hook callable for integration with agent.

        This is called during the agent's reasoning loop.
        It captures tool calls and LLM events from the message flow.

        Args:
            agent: The agent instance
            kwargs: Input arguments to the _reasoning method

        Returns:
            None (hook doesn't modify kwargs)
        """
        # This hook is called during reasoning - actual event capture
        # happens through the on_* methods called by the agent
        return None


class TracingHookRegistry:
    """Registry for managing tracing hooks per trace.

    This provides a way to get the appropriate hook for a trace
    from anywhere in the code.
    """

    _hooks: dict[str, TracingHook] = {}

    @classmethod
    def register(cls, trace_id: str, hook: TracingHook) -> None:
        """Register a tracing hook for a trace.

        Args:
            trace_id: Trace identifier
            hook: Tracing hook instance
        """
        cls._hooks[trace_id] = hook
        logger.debug("TracingHookRegistry registered: trace_id=%s", trace_id)

    @classmethod
    def unregister(cls, trace_id: str) -> None:
        """Unregister a tracing hook.

        Args:
            trace_id: Trace identifier
        """
        cls._hooks.pop(trace_id, None)

    @classmethod
    def get(cls, trace_id: str) -> Optional[TracingHook]:
        """Get the tracing hook for a trace.

        Args:
            trace_id: Trace identifier

        Returns:
            TracingHook or None
        """
        return cls._hooks.get(trace_id)

    @classmethod
    def clear(cls) -> None:
        """Clear all registered hooks."""
        cls._hooks.clear()
