# -*- coding: utf-8 -*-
"""Trace manager module.

Provides the TraceManager for event collection, batching, and storage.
"""
import asyncio
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .config import TracingConfig
from ..database import DatabaseConnection
from .models import EventType, Span, Trace, TraceStatus
from .store import TraceStore
from .sanitizer import sanitize_dict, sanitize_string

logger = logging.getLogger(__name__)

# Context variable for current trace
_current_trace: ContextVar[Optional["TraceContext"]] = ContextVar(
    "current_trace",
    default=None,
)


class TraceContext:
    """Context for the current trace."""

    def __init__(
        self,
        trace_id: str,
        user_id: str,
        session_id: str,
        channel: str,
    ):
        self.trace_id = trace_id
        self.user_id = user_id
        self.session_id = session_id
        self.channel = channel
        self.start_time = datetime.now()
        self.trace: Optional[Trace] = None
        self._span_stack: list[str] = []

    def push_span(self, span_id: str) -> None:
        """Push a span ID onto the stack."""
        self._span_stack.append(span_id)

    def pop_span(self) -> Optional[str]:
        """Pop a span ID from the stack."""
        return self._span_stack.pop() if self._span_stack else None

    @property
    def current_span_id(self) -> Optional[str]:
        """Get current span ID."""
        return self._span_stack[-1] if self._span_stack else None


def get_current_trace() -> Optional[TraceContext]:
    """Get the current trace context."""
    return _current_trace.get()


def set_current_trace(ctx: Optional[TraceContext]) -> None:
    """Set the current trace context."""
    _current_trace.set(ctx)


class TraceManager:
    """Manager for trace lifecycle and event collection.

    Handles:
    - Trace creation and completion
    - Span creation and updates
    - Batch writing for performance
    - Data sanitization
    """

    def __init__(
        self,
        config: TracingConfig,
        storage_path: Optional[Path] = None,
    ):
        """Initialize trace manager.

        Args:
            config: Tracing configuration
            storage_path: Optional custom storage path
        """
        self.config = config
        self._store: Optional[TraceStore] = None
        self._storage_path = storage_path

        # Batch queue for spans
        self._span_queue: list[Span] = []
        self._span_queue_lock = asyncio.Lock()

        # Pending spans cache (for update before flush)
        self._pending_spans: dict[str, Span] = {}

        # Background flush task
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        # Active traces
        self._active_traces: dict[str, Trace] = {}

    @property
    def store(self) -> TraceStore:
        """Get trace store."""
        if self._store is None:
            raise RuntimeError("TraceManager not initialized")
        return self._store

    @property
    def enabled(self) -> bool:
        """Check if tracing is enabled."""
        return self.config.enabled

    async def initialize(self, storage_path: Optional[Path] = None) -> None:
        """Initialize the trace manager.

        Args:
            storage_path: Optional storage path override
        """
        if not self.config.enabled:
            logger.info("Tracing is disabled")
            return

        # Use provided path or configured path or default
        if storage_path:
            self._storage_path = storage_path
        elif self._storage_path is None:
            # Will be set by caller with workspace context
            self._storage_path = Path("tracing")

        # Create database connection if configured
        db: Optional[DatabaseConnection] = None
        if self.config.database:
            try:
                db = DatabaseConnection(self.config.database)
                await db.connect()
                logger.info(
                    "Database connection established: %s",
                    self.config.database.host,
                )
            except Exception as e:
                logger.warning(
                    "Failed to connect to database, falling back to JSON: %s",
                    e,
                )
                db = None

        # Create store (with database or JSON file)
        self._store = TraceStore(self.config, self._storage_path, db)

        # Start flush task
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())

        # Start cleanup task if retention is configured
        self._cleanup_task: Optional[asyncio.Task] = None
        if self.config.retention_days > 0:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        if db and db.is_connected:
            logger.info(
                "TraceManager initialized (database storage: %s)",
                self.config.database.host,
            )
        else:
            logger.info(
                "TraceManager initialized (JSON file storage: %s)",
                self._storage_path,
            )

    async def close(self) -> None:
        """Close the trace manager."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # Final flush
        await self._flush_spans()

        if self._store:
            await self._store.flush()
            await self._store.close()

        logger.info("TraceManager closed")

    # Trace lifecycle

    async def start_trace(
        self,
        user_id: str,
        session_id: str,
        channel: str,
        trace_id: Optional[str] = None,
        user_message: Optional[str] = None,
    ) -> str:
        """Start a new trace.

        Args:
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            trace_id: Optional trace ID (generated if not provided)
            user_message: Optional user's input message

        Returns:
            Trace ID
        """
        if not self.enabled:
            return trace_id or str(uuid.uuid4())

        trace_id = trace_id or str(uuid.uuid4())

        # Sanitize user message
        if self.config.sanitize_output and user_message:
            user_message = sanitize_string(
                user_message,
                self.config.max_output_length,
            )

        trace = Trace(
            trace_id=trace_id,
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            start_time=datetime.now(),
            status=TraceStatus.RUNNING,
            user_message=user_message,
        )

        await self.store.create_trace(trace)
        self._active_traces[trace_id] = trace

        # Create context
        ctx = TraceContext(trace_id, user_id, session_id, channel)
        ctx.trace = trace
        set_current_trace(ctx)

        logger.debug("Started trace: %s", trace_id)
        return trace_id

    async def end_trace(
        self,
        trace_id: str,
        status: TraceStatus = TraceStatus.COMPLETED,
        error: Optional[str] = None,
    ) -> None:
        """End a trace.

        Args:
            trace_id: Trace identifier
            status: Final status
            error: Optional error message
        """
        if not self.enabled:
            return

        # Flush pending spans before ending trace
        await self._flush_spans()

        trace = self._active_traces.pop(
            trace_id,
            None,
        ) or await self.store.get_trace(trace_id)
        if trace is None:
            logger.warning("Trace not found: %s", trace_id)
            return

        trace.end_time = datetime.now()
        trace.duration_ms = int(
            (trace.end_time - trace.start_time).total_seconds() * 1000,
        )
        trace.status = status
        trace.error = error

        await self.store.update_trace(trace)

        # Clear context
        ctx = get_current_trace()
        if ctx and ctx.trace_id == trace_id:
            set_current_trace(None)

        logger.debug(
            "Ended trace: %s (status: %s, duration: %dms)",
            trace_id,
            status,
            trace.duration_ms,
        )

    # Span operations

    async def emit_span(
        self,
        trace_id: str,
        event_type: EventType,
        name: str,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        parent_span_id: Optional[str] = None,
        model_name: Optional[str] = None,
        input_tokens: Optional[int] = None,
        tool_name: Optional[str] = None,
        skill_name: Optional[str] = None,
        tool_input: Optional[dict[str, Any]] = None,
        start_time: Optional[datetime] = None,
        mcp_server: Optional[str] = None,
    ) -> str:
        """Emit a new span event.

        Args:
            trace_id: Trace identifier
            event_type: Event type
            name: Span name
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            parent_span_id: Optional parent span ID
            model_name: Optional model name
            input_tokens: Optional input token count
            tool_name: Optional tool name
            skill_name: Optional skill name
            tool_input: Optional tool input (will be sanitized)
            start_time: Optional start time
            mcp_server: Optional MCP server name if this is an MCP tool

        Returns:
            Span ID
        """
        if not self.enabled:
            return str(uuid.uuid4())

        span_id = str(uuid.uuid4())

        # Get parent from context if not provided
        if parent_span_id is None:
            ctx = get_current_trace()
            if ctx and ctx.trace_id == trace_id:
                parent_span_id = ctx.current_span_id

        # Sanitize tool input if configured
        if self.config.sanitize_output and tool_input:
            tool_input = sanitize_dict(
                tool_input,
                self.config.max_output_length,
            )

        span = Span(
            span_id=span_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            name=name,
            event_type=event_type,
            start_time=start_time or datetime.now(),
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            model_name=model_name,
            input_tokens=input_tokens,
            tool_name=tool_name,
            skill_name=skill_name,
            tool_input=tool_input,
            mcp_server=mcp_server,
        )

        # Add to pending cache and queue atomically
        async with self._span_queue_lock:
            self._pending_spans[span_id] = span
            self._span_queue.append(span)

            # Check if we need to flush
            if len(self._span_queue) >= self.config.batch_size:
                asyncio.create_task(self._flush_spans())

        return span_id

    async def update_span(
        self,
        span_id: str,
        trace_id: str,
        output_tokens: Optional[int] = None,
        input_tokens: Optional[int] = None,
        tool_output: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        span: Optional[Span] = None,
    ) -> None:
        """Update an existing span.

        Args:
            span_id: Span identifier
            trace_id: Trace identifier
            output_tokens: Optional output token count
            input_tokens: Optional input token count (updates span if provided)
            tool_output: Optional tool output (will be sanitized)
            error: Optional error message
            metadata: Optional metadata
            span: Optional span object (to avoid re-fetching)
        """
        if not self.enabled:
            return

        span = await self._find_span(span_id, trace_id, span)
        if span is None:
            logger.warning("Span not found for update: %s", span_id)
            return

        self._update_span_fields(
            span,
            output_tokens,
            input_tokens,
            tool_output,
            error,
            metadata,
        )
        self._update_trace_totals(trace_id, span, output_tokens)

        # Persist if not in pending cache
        if span_id not in self._pending_spans:
            await self.store.update_span(span)

    async def _find_span(
        self,
        span_id: str,
        trace_id: str,
        span: Optional[Span],
    ) -> Optional[Span]:
        """Find span from cache or store."""
        if span is not None:
            return span

        # Check pending cache first
        span = self._pending_spans.get(span_id)
        if span is not None:
            return span

        # Check store
        spans = await self.store.get_spans(trace_id)
        for s in spans:
            if s.span_id == span_id:
                return s

        return None

    def _update_span_fields(
        self,
        span: Span,
        output_tokens: Optional[int],
        input_tokens: Optional[int],
        tool_output: Optional[str],
        error: Optional[str],
        metadata: Optional[dict[str, Any]],
    ) -> None:
        """Update span fields."""
        span.end_time = datetime.now()
        span.duration_ms = int(
            (span.end_time - span.start_time).total_seconds() * 1000,
        )
        span.output_tokens = output_tokens
        if input_tokens is not None and input_tokens > 0:
            span.input_tokens = input_tokens
        span.tool_output = (
            sanitize_string(tool_output, self.config.max_output_length)
            if self.config.sanitize_output
            else tool_output
        )
        span.error = error
        span.metadata = metadata

    def _update_trace_totals(
        self,
        trace_id: str,
        span: Span,
        output_tokens: Optional[int],
    ) -> None:
        """Update trace statistics from span."""
        trace = self._active_traces.get(trace_id)
        if not trace:
            return

        if output_tokens:
            trace.total_output_tokens += output_tokens
        if span.input_tokens:
            trace.total_input_tokens += span.input_tokens
        if span.model_name:
            trace.model_name = span.model_name
        if span.tool_name and span.tool_name not in trace.tools_used:
            trace.tools_used.append(span.tool_name)
        if span.skill_name and span.skill_name not in trace.skills_used:
            trace.skills_used.append(span.skill_name)

    async def emit_llm_input(
        self,
        trace_id: str,
        model_name: str,
        input_tokens: int,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
    ) -> str:
        """Emit LLM input event.

        Args:
            trace_id: Trace identifier
            model_name: Model name
            input_tokens: Input token count
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier

        Returns:
            Span ID
        """
        return await self.emit_span(
            trace_id=trace_id,
            event_type=EventType.LLM_INPUT,
            name=f"llm_call_{model_name}",
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            model_name=model_name,
            input_tokens=input_tokens,
        )

    async def emit_llm_output(
        self,
        trace_id: str,
        span_id: str,
        output_tokens: int,
        input_tokens: int = 0,
    ) -> None:
        """Emit LLM output event (updates the span).

        Args:
            trace_id: Trace identifier
            span_id: Span identifier from LLM input
            output_tokens: Output token count
            input_tokens: Input token count (updates span if provided)
        """
        await self.update_span(
            span_id=span_id,
            trace_id=trace_id,
            output_tokens=output_tokens,
            input_tokens=input_tokens if input_tokens > 0 else None,
        )

    async def emit_tool_call_start(
        self,
        trace_id: str,
        tool_name: str,
        tool_input: Optional[dict[str, Any]],
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        mcp_server: Optional[str] = None,
    ) -> str:
        """Emit tool call start event.

        Args:
            trace_id: Trace identifier
            tool_name: Tool name
            tool_input: Tool input
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            mcp_server: Optional MCP server name if this is an MCP tool

        Returns:
            Span ID
        """
        return await self.emit_span(
            trace_id=trace_id,
            event_type=EventType.TOOL_CALL_START,
            name=f"tool_{tool_name}",
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            tool_name=tool_name,
            tool_input=tool_input,
            mcp_server=mcp_server,
        )

    async def emit_tool_call_end(
        self,
        trace_id: str,
        span_id: str,
        tool_output: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        """Emit tool call end event.

        Args:
            trace_id: Trace identifier
            span_id: Span identifier from tool call start
            tool_output: Tool output
            error: Optional error message
        """
        # First, find the span (in pending cache or store)
        span = self._pending_spans.get(span_id)
        if span is None:
            spans = await self.store.get_spans(trace_id)
            for s in spans:
                if s.span_id == span_id:
                    span = s
                    break

        if span is None:
            logger.warning("Span not found for tool_call_end: %s", span_id)
            return

        # Update event_type to TOOL_CALL_END for proper statistics
        span.event_type = EventType.TOOL_CALL_END

        # Update other fields, passing the span object to avoid re-fetching
        await self.update_span(
            span_id=span_id,
            trace_id=trace_id,
            tool_output=tool_output,
            error=error,
            span=span,
        )

    async def emit_skill_invocation(
        self,
        trace_id: str,
        skill_name: str,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        skill_input: Optional[dict[str, Any]] = None,
    ) -> str:
        """Emit skill invocation event.

        Args:
            trace_id: Trace identifier
            skill_name: Skill name
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            skill_input: Optional skill input parameters

        Returns:
            Span ID
        """
        return await self.emit_span(
            trace_id=trace_id,
            event_type=EventType.SKILL_INVOCATION,
            name=f"skill_{skill_name}",
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            skill_name=skill_name,
            tool_input=skill_input,
        )

    async def end_skill_invocation(
        self,
        trace_id: str,
        span_id: str,
        skill_output: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """End skill invocation event.

        Args:
            trace_id: Trace identifier
            span_id: Span identifier from skill invocation
            skill_output: Optional skill output
            error: Optional error message
        """
        await self.update_span(
            span_id=span_id,
            trace_id=trace_id,
            tool_output=skill_output,
            error=error,
        )

    # Background flush

    async def _flush_loop(self) -> None:
        """Background loop for flushing queued spans."""
        while self._running:
            try:
                await asyncio.sleep(self.config.flush_interval)
                await self._flush_spans()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in flush loop: %s", e)

    async def _flush_spans(self) -> None:
        """Flush queued spans to storage."""
        async with self._span_queue_lock:
            if not self._span_queue:
                return
            spans = self._span_queue.copy()
            self._span_queue.clear()
            # Clear pending cache atomically with queue clear
            for span in spans:
                self._pending_spans.pop(span.span_id, None)

        if spans:
            try:
                await self.store.batch_create_spans(spans)
                await self.store.flush()
                logger.debug("Flushed %d spans", len(spans))
            except Exception as e:
                logger.error("Failed to flush spans: %s", e)

    async def _cleanup_loop(self) -> None:
        """Background loop for cleaning up old trace data."""
        # Run cleanup once per day (at startup and then every 24 hours)
        while self._running:
            try:
                # Initial cleanup on startup
                await self._cleanup_old_data()

                # Wait 24 hours between cleanups
                await asyncio.sleep(24 * 60 * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in cleanup loop: %s", e)
                # Wait 1 hour before retry on error
                await asyncio.sleep(60 * 60)

    async def _cleanup_old_data(self) -> None:
        """Clean up trace data older than retention period."""
        if self.config.retention_days <= 0:
            return

        try:
            cutoff_date = datetime.now() - timedelta(
                days=self.config.retention_days,
            )
            logger.info(
                "Cleaning up trace data older than %s (retention: %d days)",
                cutoff_date.strftime("%Y-%m-%d"),
                self.config.retention_days,
            )

            # Clean up JSON files older than retention period
            if self._storage_path:
                deleted_count = 0
                for file_path in self._storage_path.glob("traces_*.json"):
                    try:
                        # Extract date from filename
                        filename = file_path.stem  # traces_2024-01-15
                        date_str = filename.replace("traces_", "")
                        file_date = datetime.strptime(date_str, "%Y-%m-%d")

                        if file_date < cutoff_date:
                            file_path.unlink()
                            deleted_count += 1
                    except (ValueError, OSError) as e:
                        logger.warning(
                            "Failed to process file %s: %s",
                            file_path,
                            e,
                        )

                if deleted_count > 0:
                    logger.info("Deleted %d old trace files", deleted_count)

            # Clean up in-memory data older than retention period
            await self.store.cleanup_old_data(cutoff_date)

        except Exception as e:
            logger.error("Failed to cleanup old data: %s", e)


# Global trace manager instance
_trace_manager: Optional[TraceManager] = None


def get_trace_manager() -> TraceManager:
    """Get the global trace manager.

    Raises:
        RuntimeError: If trace manager not initialized

    Returns:
        TraceManager instance
    """
    if _trace_manager is None:
        raise RuntimeError(
            "TraceManager not initialized. Call init_trace_manager() first.",
        )
    return _trace_manager


def has_trace_manager() -> bool:
    """Check if trace manager is initialized."""
    return _trace_manager is not None


async def init_trace_manager(
    config: Optional[TracingConfig] = None,
    storage_path: Optional[Path] = None,
) -> TraceManager:
    """Initialize the global trace manager.

    Args:
        config: Optional tracing configuration (uses defaults if not provided)
        storage_path: Optional storage path

    Returns:
        TraceManager instance
    """
    global _trace_manager

    if _trace_manager is not None:
        return _trace_manager

    config = config or TracingConfig()
    _trace_manager = TraceManager(config)
    await _trace_manager.initialize(storage_path)

    return _trace_manager


async def close_trace_manager() -> None:
    """Close the global trace manager."""
    global _trace_manager

    if _trace_manager is not None:
        await _trace_manager.close()
        _trace_manager = None
