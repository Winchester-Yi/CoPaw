# -*- coding: utf-8 -*-
"""Trace store module.

Provides storage operations for traces and spans using JSON files or database.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import TracingConfig
from ..database import DatabaseConnection
from .models import (
    EventType,
    MCPToolUsage,
    MCPServerUsage,
    ModelUsage,
    OverviewStats,
    SessionListItem,
    SessionStats,
    Span,
    ToolCall,
    Trace,
    TraceDetail,
    TraceListItem,
    TraceStatus,
    ToolUsage,
    SkillUsage,
    UserListItem,
    UserMessageItem,
    UserStats,
)

logger = logging.getLogger(__name__)


class TraceStore:
    """Store for traces and spans using JSON file or database storage.

    Uses daily JSON files for storage with in-memory aggregation for queries.
    Falls back to in-memory storage when no database is available.
    """

    def __init__(
        self,
        config: TracingConfig,
        storage_path: Path,
        db: Optional[DatabaseConnection] = None,
    ):
        """Initialize trace store.

        Args:
            config: Tracing configuration
            storage_path: Base path for trace storage
            db: Optional database connection for persistent storage
        """
        self.config = config
        self.storage_path = storage_path
        self.db = db
        self._use_db = db is not None and db.is_connected

        # Create storage directory for JSON files (used as fallback or primary)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # In-memory storage for current day
        self._traces: dict[str, Trace] = {}
        self._spans: dict[str, list[Span]] = {}
        self._file_lock = asyncio.Lock()

        if self._use_db:
            logger.info("TraceStore initialized with database storage")
        else:
            # Load today's data from file if exists (JSON mode)
            self._load_today_data()
            logger.info("TraceStore initialized with JSON file storage")

    async def close(self) -> None:
        """Close store and database connection."""
        if self.db is not None:
            await self.db.close()

    def _get_daily_file_path(self, date: Optional[datetime] = None) -> Path:
        """Get the JSON file path for a specific date.

        Args:
            date: Date to get file for (default: today)

        Returns:
            Path to the daily JSON file
        """
        date = date or datetime.now()
        filename = f"traces_{date.strftime('%Y-%m-%d')}.json"
        return self.storage_path / filename

    def _load_today_data(self) -> None:
        """Load today's data from file if it exists."""
        file_path = self._get_daily_file_path()
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                for trace_data in data.get("traces", []):
                    trace = Trace(**trace_data)
                    self._traces[trace.trace_id] = trace

                for span_data in data.get("spans", []):
                    span = Span(**span_data)
                    if span.trace_id not in self._spans:
                        self._spans[span.trace_id] = []
                    self._spans[span.trace_id].append(span)

                logger.info(
                    "Loaded %d traces and %d spans from %s",
                    len(self._traces),
                    sum(len(s) for s in self._spans.values()),
                    file_path,
                )
            except Exception as e:
                logger.warning("Failed to load today's tracing data: %s", e)

    async def _save_to_file(self, date: Optional[datetime] = None) -> None:
        """Save current data to daily JSON file.

        Args:
            date: Date for the file (default: today)
        """
        file_path = self._get_daily_file_path(date)

        async with self._file_lock:
            try:
                data = {
                    "traces": [t.model_dump() for t in self._traces.values()],
                    "spans": [
                        s.model_dump()
                        for spans in self._spans.values()
                        for s in spans
                    ],
                }

                # Write to temp file first, then rename for atomicity
                temp_path = file_path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(
                        data,
                        f,
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )

                temp_path.rename(file_path)
                logger.debug("Saved tracing data to %s", file_path)
            except Exception as e:
                logger.error("Failed to save tracing data: %s", e)

    # Trace operations

    async def create_trace(self, trace: Trace) -> None:
        """Create a new trace.

        Args:
            trace: Trace to create
        """
        self._traces[trace.trace_id] = trace
        if trace.trace_id not in self._spans:
            self._spans[trace.trace_id] = []

    async def update_trace(self, trace: Trace) -> None:
        """Update an existing trace.

        Args:
            trace: Trace to update
        """
        self._traces[trace.trace_id] = trace

    async def get_trace(self, trace_id: str) -> Optional[Trace]:
        """Get a trace by ID.

        Args:
            trace_id: Trace identifier

        Returns:
            Trace or None
        """
        return self._traces.get(trace_id)

    # Span operations

    async def create_span(self, span: Span) -> None:
        """Create a new span.

        Args:
            span: Span to create
        """
        if span.trace_id not in self._spans:
            self._spans[span.trace_id] = []
        self._spans[span.trace_id].append(span)

    async def update_span(self, span: Span) -> None:
        """Update an existing span.

        Args:
            span: Span to update
        """
        spans = self._spans.get(span.trace_id, [])
        for i, s in enumerate(spans):
            if s.span_id == span.span_id:
                spans[i] = span
                break

    async def get_spans(self, trace_id: str) -> list[Span]:
        """Get all spans for a trace.

        Args:
            trace_id: Trace identifier

        Returns:
            List of spans
        """
        return self._spans.get(trace_id, [])

    # Batch operations

    async def batch_create_spans(self, spans: list[Span]) -> None:
        """Batch create spans.

        Args:
            spans: List of spans to create
        """
        if not spans:
            return
        for span in spans:
            if span.trace_id not in self._spans:
                self._spans[span.trace_id] = []
            self._spans[span.trace_id].append(span)

    # Flush operation

    async def flush(self) -> None:
        """Flush current data to file."""
        await self._save_to_file()

    # Query operations

    async def get_overview_stats(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> OverviewStats:
        """Get overview statistics.

        Args:
            start_date: Start date filter
            end_date: End date filter

        Returns:
            Overview statistics
        """
        if start_date is None:
            start_date = datetime.now() - timedelta(days=7)
        if end_date is None:
            end_date = datetime.now()

        # Filter traces by date
        traces = [
            t
            for t in self._traces.values()
            if start_date <= t.start_time <= end_date
        ]

        # Count unique users and online users
        users = set(t.user_id for t in traces if t.user_id)
        online_users = self._count_online_users()

        # Token stats
        total_input = sum(t.total_input_tokens for t in traces)
        total_output = sum(t.total_output_tokens for t in traces)
        durations = [t.duration_ms for t in traces if t.duration_ms]
        avg_duration = sum(durations) // len(durations) if durations else 0

        # Model distribution
        model_distribution = self._build_model_distribution(traces)

        # Collect span stats
        tool_counts, skill_counts, mcp_tool_counts = self._collect_span_stats(
            start_date,
            end_date,
        )

        # Build result lists
        top_tools = self._build_top_tools(tool_counts)
        top_skills = self._build_top_skills(skill_counts)
        top_mcp_tools, mcp_servers = self._build_mcp_stats(mcp_tool_counts)

        return OverviewStats(
            online_users=online_users,
            total_users=len(users),
            model_distribution=model_distribution,
            total_tokens=total_input + total_output,
            input_tokens=total_input,
            output_tokens=total_output,
            total_sessions=len(traces),
            total_conversations=len(set(t.session_id for t in traces)),
            avg_duration_ms=avg_duration,
            top_tools=top_tools,
            top_skills=top_skills,
            top_mcp_tools=top_mcp_tools,
            mcp_servers=mcp_servers,
            daily_trend=[],
        )

    def _count_online_users(self) -> int:
        """Count users active in last 5 minutes."""
        online_threshold = datetime.now() - timedelta(minutes=5)
        online_users = set()
        for spans in self._spans.values():
            for s in spans:
                if s.start_time >= online_threshold and s.user_id:
                    online_users.add(s.user_id)
        return len(online_users)

    def _collect_span_stats(
        self,
        start_date: datetime,
        end_date: datetime,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> tuple[dict, dict, dict]:
        """Collect tool, skill, and MCP stats from spans."""
        tool_counts: dict[str, dict] = {}
        skill_counts: dict[str, dict] = {}
        mcp_tool_counts: dict[str, dict] = {}

        for trace_id, spans in self._spans.items():
            trace = self._traces.get(trace_id)
            if trace:
                if user_id and trace.user_id != user_id:
                    continue
                if session_id and trace.session_id != session_id:
                    continue

            for s in spans:
                if not start_date <= s.start_time <= end_date:
                    continue

                self._update_span_counts(
                    s,
                    tool_counts,
                    skill_counts,
                    mcp_tool_counts,
                )

        return tool_counts, skill_counts, mcp_tool_counts

    def _update_span_counts(
        self,
        span: Span,
        tool_counts: dict,
        skill_counts: dict,
        mcp_tool_counts: dict,
    ) -> None:
        """Update count dictionaries based on span type."""
        event_type_str = (
            span.event_type.value
            if hasattr(span.event_type, "value")
            else str(span.event_type)
        )

        if (
            event_type_str == EventType.SKILL_INVOCATION.value
            and span.skill_name
        ):
            self._increment_count(skill_counts, span.skill_name, span)
        elif (
            event_type_str == EventType.TOOL_CALL_END.value and span.tool_name
        ):
            if span.mcp_server:
                key = f"{span.mcp_server}:{span.tool_name}"
                if key not in mcp_tool_counts:
                    mcp_tool_counts[key] = {
                        "tool_name": span.tool_name,
                        "mcp_server": span.mcp_server,
                        "count": 0,
                        "duration": 0,
                        "errors": 0,
                    }
                mcp_tool_counts[key]["count"] += 1
                mcp_tool_counts[key]["duration"] += span.duration_ms or 0
                if span.error:
                    mcp_tool_counts[key]["errors"] += 1
            else:
                self._increment_count(tool_counts, span.tool_name, span)

    def _increment_count(
        self,
        counts: dict,
        key: str,
        span: Span,
    ) -> None:
        """Increment count for a key in counts dict."""
        if key not in counts:
            counts[key] = {"count": 0, "duration": 0, "errors": 0}
        counts[key]["count"] += 1
        counts[key]["duration"] += span.duration_ms or 0
        if span.error:
            counts[key]["errors"] += 1

    def _build_top_tools(
        self,
        tool_counts: dict,
        limit: int = 10,
    ) -> list[ToolUsage]:
        """Build ToolUsage list from counts."""
        return [
            ToolUsage(
                tool_name=name,
                count=data["count"],
                avg_duration_ms=data["duration"] // max(data["count"], 1),
                error_count=data["errors"],
            )
            for name, data in sorted(
                tool_counts.items(),
                key=lambda x: -x[1]["count"],
            )[:limit]
        ]

    def _build_top_skills(
        self,
        skill_counts: dict,
        limit: int = 10,
    ) -> list[SkillUsage]:
        """Build SkillUsage list from counts."""
        return [
            SkillUsage(
                skill_name=name,
                count=data["count"],
                avg_duration_ms=data["duration"] // max(data["count"], 1),
            )
            for name, data in sorted(
                skill_counts.items(),
                key=lambda x: -x[1]["count"],
            )[:limit]
        ]

    def _build_mcp_stats(
        self,
        mcp_tool_counts: dict,
        limit: int = 10,
    ) -> tuple[list[MCPToolUsage], list[MCPServerUsage]]:
        """Build MCP tool and server stats from counts."""
        # Top MCP tools
        top_mcp_tools = [
            MCPToolUsage(
                tool_name=data["tool_name"],
                mcp_server=data["mcp_server"],
                count=data["count"],
                avg_duration_ms=data["duration"] // max(data["count"], 1),
                error_count=data["errors"],
            )
            for key, data in sorted(
                mcp_tool_counts.items(),
                key=lambda x: -x[1]["count"],
            )[:limit]
        ]

        # Group by server
        server_data: dict[str, dict] = {}
        for key, data in mcp_tool_counts.items():
            server = data["mcp_server"]
            if server not in server_data:
                server_data[server] = {
                    "total_calls": 0,
                    "duration": 0,
                    "errors": 0,
                    "tools": [],
                }
            server_data[server]["total_calls"] += data["count"]
            server_data[server]["duration"] += data["duration"]
            server_data[server]["errors"] += data["errors"]
            server_data[server]["tools"].append(data)

        mcp_servers = [
            MCPServerUsage(
                server_name=server,
                tool_count=len(data["tools"]),
                total_calls=data["total_calls"],
                avg_duration_ms=data["duration"]
                // max(data["total_calls"], 1),
                error_count=data["errors"],
                tools=[
                    MCPToolUsage(
                        tool_name=t["tool_name"],
                        mcp_server=t["mcp_server"],
                        count=t["count"],
                        avg_duration_ms=t["duration"] // max(t["count"], 1),
                        error_count=t["errors"],
                    )
                    for t in sorted(data["tools"], key=lambda x: -x["count"])
                ],
            )
            for server, data in sorted(
                server_data.items(),
                key=lambda x: -x[1]["total_calls"],
            )
        ]

        return top_mcp_tools, mcp_servers

    def _build_model_distribution(
        self,
        traces: list[Trace],
    ) -> list[ModelUsage]:
        """Build model distribution from traces."""
        model_counts: dict[str, dict] = {}
        for t in traces:
            if t.model_name:
                if t.model_name not in model_counts:
                    model_counts[t.model_name] = {
                        "count": 0,
                        "tokens": 0,
                        "input": 0,
                        "output": 0,
                    }
                model_counts[t.model_name]["count"] += 1
                model_counts[t.model_name]["tokens"] += (
                    t.total_input_tokens + t.total_output_tokens
                )
                model_counts[t.model_name]["input"] += t.total_input_tokens
                model_counts[t.model_name]["output"] += t.total_output_tokens

        return [
            ModelUsage(
                model_name=name,
                count=data["count"],
                total_tokens=data["tokens"],
                input_tokens=data["input"],
                output_tokens=data["output"],
            )
            for name, data in sorted(
                model_counts.items(),
                key=lambda x: -x[1]["count"],
            )
        ]

    async def get_users(
        self,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> tuple[list[UserListItem], int]:
        """Get list of users with stats."""
        # Aggregate by user
        user_data: dict[str, dict] = {}
        for t in self._traces.values():
            uid = t.user_id
            if not uid:
                continue
            if user_id and user_id not in uid:
                continue
            if start_date and t.start_time < start_date:
                continue
            if end_date and t.start_time > end_date:
                continue
            if uid not in user_data:
                user_data[uid] = {
                    "sessions": 0,
                    "conversations": set(),
                    "tokens": 0,
                    "skills": 0,
                    "last_active": t.start_time,
                }
            user_data[uid]["sessions"] += 1
            user_data[uid]["conversations"].add(t.session_id)
            user_data[uid]["tokens"] += (
                t.total_input_tokens + t.total_output_tokens
            )
            if t.start_time > user_data[uid]["last_active"]:
                user_data[uid]["last_active"] = t.start_time

        # Count skills per user from spans
        for trace_id, spans in self._spans.items():
            trace = self._traces.get(trace_id)
            if trace and trace.user_id in user_data:
                for s in spans:
                    event_type_str = (
                        s.event_type.value
                        if hasattr(s.event_type, "value")
                        else str(s.event_type)
                    )
                    if event_type_str == EventType.SKILL_INVOCATION.value:
                        user_data[trace.user_id]["skills"] += 1

        # Sort and paginate
        sorted_users = sorted(
            user_data.items(),
            key=lambda x: x[1]["last_active"],
            reverse=True,
        )
        total = len(sorted_users)
        offset = (page - 1) * page_size
        items = [
            UserListItem(
                user_id=uid,
                total_sessions=data["sessions"],
                total_conversations=len(data["conversations"]),
                total_tokens=data["tokens"],
                total_skills=data["skills"],
                last_active=data["last_active"],
            )
            for uid, data in sorted_users[offset : offset + page_size]
        ]
        return items, total

    async def get_user_stats(
        self,
        user_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> UserStats:
        """Get statistics for a specific user."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now()

        traces = [
            t
            for t in self._traces.values()
            if t.user_id == user_id and start_date <= t.start_time <= end_date
        ]

        if not traces:
            return UserStats(user_id=user_id)

        total_input = sum(t.total_input_tokens for t in traces)
        total_output = sum(t.total_output_tokens for t in traces)
        durations = [t.duration_ms for t in traces if t.duration_ms]
        avg_duration = sum(durations) // len(durations) if durations else 0

        # Model usage
        model_usage = self._build_model_distribution(traces)

        # Collect span stats for this user
        tool_counts, skill_counts, _ = self._collect_span_stats(
            start_date,
            end_date,
            user_id=user_id,
        )

        tools_used = self._build_top_tools(tool_counts)
        skills_used = self._build_top_skills(skill_counts)

        return UserStats(
            user_id=user_id,
            model_usage=model_usage,
            total_tokens=total_input + total_output,
            input_tokens=total_input,
            output_tokens=total_output,
            total_sessions=len(traces),
            total_conversations=len(set(t.session_id for t in traces)),
            avg_duration_ms=avg_duration,
            tools_used=tools_used,
            skills_used=skills_used,
        )

    async def get_traces(
        self,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> tuple[list[TraceListItem], int]:
        """Get list of traces."""
        traces = list(self._traces.values())

        # Apply filters
        if user_id:
            traces = [t for t in traces if t.user_id == user_id]
        if session_id:
            traces = [t for t in traces if t.session_id == session_id]
        if status:
            traces = [t for t in traces if t.status == status]
        if start_date:
            traces = [t for t in traces if t.start_time >= start_date]
        if end_date:
            traces = [t for t in traces if t.start_time <= end_date]

        # Sort by start time descending
        traces.sort(key=lambda t: t.start_time, reverse=True)

        total = len(traces)
        offset = (page - 1) * page_size
        items = [
            TraceListItem(
                trace_id=t.trace_id,
                user_id=t.user_id,
                session_id=t.session_id,
                channel=t.channel,
                start_time=t.start_time,
                duration_ms=t.duration_ms,
                total_tokens=t.total_input_tokens + t.total_output_tokens,
                model_name=t.model_name,
                status=t.status.value
                if isinstance(t.status, TraceStatus)
                else t.status,
                skills_count=len(t.skills_used),
            )
            for t in traces[offset : offset + page_size]
        ]
        return items, total

    async def get_trace_detail(self, trace_id: str) -> Optional[TraceDetail]:
        """Get detailed trace with spans."""
        trace = await self.get_trace(trace_id)
        if trace is None:
            return None

        spans = await self.get_spans(trace_id)

        # Calculate durations by type
        llm_duration = sum(
            s.duration_ms or 0
            for s in spans
            if s.event_type in (EventType.LLM_INPUT, EventType.LLM_OUTPUT)
        )
        tool_duration = sum(
            s.duration_ms or 0
            for s in spans
            if s.event_type
            in (EventType.TOOL_CALL_START, EventType.TOOL_CALL_END)
        )

        # Extract tool calls
        tools_called = []
        tool_spans = [
            s for s in spans if s.event_type == EventType.TOOL_CALL_END
        ]
        for span in tool_spans:
            tools_called.append(
                ToolCall(
                    tool_name=span.tool_name or span.name,
                    tool_input=span.tool_input,
                    tool_output=span.tool_output,
                    duration_ms=span.duration_ms,
                    error=span.error,
                ),
            )

        return TraceDetail(
            trace=trace,
            spans=spans,
            llm_duration_ms=llm_duration,
            tool_duration_ms=tool_duration,
            tools_called=tools_called,
        )

    async def get_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> tuple[list[SessionListItem], int]:
        """Get list of sessions with stats."""
        session_data = self._aggregate_sessions_by_trace(
            user_id,
            session_id,
            start_date,
            end_date,
        )
        self._count_session_skills(session_data)

        sorted_sessions = sorted(
            session_data.items(),
            key=lambda x: x[1]["last_active"],
            reverse=True,
        )
        total = len(sorted_sessions)
        offset = (page - 1) * page_size
        items = [
            SessionListItem(
                session_id=sid,
                user_id=data["user_id"],
                channel=data["channel"],
                total_traces=data["traces"],
                total_tokens=data["tokens"],
                total_skills=data["skills"],
                first_active=data["first_active"],
                last_active=data["last_active"],
            )
            for sid, data in sorted_sessions[offset : offset + page_size]
        ]
        return items, total

    def _aggregate_sessions_by_trace(
        self,
        user_id: Optional[str],
        session_id: Optional[str],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> dict[str, dict]:
        """Aggregate session data from traces."""
        session_data: dict[str, dict] = {}
        for t in self._traces.values():
            if not self._trace_matches_filters(
                t,
                user_id,
                session_id,
                start_date,
                end_date,
            ):
                continue

            sid = t.session_id
            if sid not in session_data:
                session_data[sid] = {
                    "user_id": t.user_id,
                    "channel": t.channel,
                    "traces": 0,
                    "tokens": 0,
                    "skills": 0,
                    "first_active": t.start_time,
                    "last_active": t.start_time,
                }

            entry = session_data[sid]
            entry["traces"] += 1
            entry["tokens"] += t.total_input_tokens + t.total_output_tokens
            entry["first_active"] = min(entry["first_active"], t.start_time)
            entry["last_active"] = max(entry["last_active"], t.start_time)

        return session_data

    def _trace_matches_filters(
        self,
        trace: Trace,
        user_id: Optional[str],
        session_id: Optional[str],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> bool:
        """Check if trace matches all filter criteria."""
        if user_id and trace.user_id != user_id:
            return False
        if session_id and session_id not in trace.session_id:
            return False
        if start_date and trace.start_time < start_date:
            return False
        if end_date and trace.start_time > end_date:
            return False
        return True

    def _count_session_skills(self, session_data: dict[str, dict]) -> None:
        """Count skills per session from spans (in-place update)."""
        for trace_id, spans in self._spans.items():
            trace = self._traces.get(trace_id)
            if not trace:
                continue
            session_entry = session_data.get(trace.session_id)
            if not session_entry:
                continue
            session_entry["skills"] += sum(
                1 for s in spans if self._is_skill_invocation(s)
            )

    @staticmethod
    def _is_skill_invocation(span: Span) -> bool:
        """Check if a span is a skill invocation."""
        event_type = (
            span.event_type.value
            if hasattr(span.event_type, "value")
            else str(span.event_type)
        )
        return event_type == EventType.SKILL_INVOCATION.value

    async def get_session_stats(
        self,
        session_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> SessionStats:
        """Get statistics for a specific session."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now()

        traces = self._filter_traces_by_session(
            session_id,
            start_date,
            end_date,
        )

        if not traces:
            return SessionStats(session_id=session_id, user_id="", channel="")

        # Calculate basic stats
        first_trace = traces[0]
        totals = self._calculate_trace_totals(traces)

        # Collect span stats
        tool_counts, skill_counts, mcp_tool_counts = self._collect_span_stats(
            start_date,
            end_date,
            session_id=session_id,
        )

        # Build MCP tools list
        mcp_tools_used = [
            MCPToolUsage(
                tool_name=data["tool_name"],
                mcp_server=data["mcp_server"],
                count=data["count"],
                avg_duration_ms=data["duration"] // max(data["count"], 1),
                error_count=data["errors"],
            )
            for data in mcp_tool_counts.values()
        ]

        return SessionStats(
            session_id=session_id,
            user_id=first_trace.user_id,
            channel=first_trace.channel,
            model_usage=self._build_model_distribution(traces),
            total_tokens=totals["total_tokens"],
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            total_traces=len(traces),
            avg_duration_ms=totals["avg_duration"],
            tools_used=self._build_top_tools(tool_counts),
            skills_used=self._build_top_skills(skill_counts),
            mcp_tools_used=mcp_tools_used,
            first_active=totals["first_active"],
            last_active=totals["last_active"],
        )

    def _filter_traces_by_session(
        self,
        session_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Trace]:
        """Filter traces by session and date range."""
        return [
            t
            for t in self._traces.values()
            if t.session_id == session_id
            and start_date <= t.start_time <= end_date
        ]

    def _calculate_trace_totals(self, traces: list[Trace]) -> dict:
        """Calculate totals from a list of traces."""
        input_tokens = sum(t.total_input_tokens for t in traces)
        output_tokens = sum(t.total_output_tokens for t in traces)
        durations = [t.duration_ms for t in traces if t.duration_ms]

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "avg_duration": sum(durations) // len(durations)
            if durations
            else 0,
            "first_active": min(t.start_time for t in traces),
            "last_active": max(t.start_time for t in traces),
        }

    async def get_user_messages(
        self,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        query: Optional[str] = None,
        export: bool = False,
    ) -> tuple[list[UserMessageItem], int]:
        """Get user messages with token info for cost analysis."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=7)
        if end_date is None:
            end_date = datetime.now()

        traces = list(self._traces.values())

        # Apply filters
        traces = [t for t in traces if start_date <= t.start_time <= end_date]
        if user_id:
            traces = [t for t in traces if t.user_id == user_id]
        if session_id:
            traces = [t for t in traces if t.session_id == session_id]
        if query:
            traces = [
                t
                for t in traces
                if t.user_message and query.lower() in t.user_message.lower()
            ]

        # Sort by start time descending
        traces.sort(key=lambda t: t.start_time, reverse=True)

        total = len(traces)

        if export:
            items = traces
        else:
            offset = (page - 1) * page_size
            items = traces[offset : offset + page_size]

        messages = [
            UserMessageItem(
                trace_id=t.trace_id,
                user_id=t.user_id,
                session_id=t.session_id,
                channel=t.channel,
                user_message=t.user_message,
                input_tokens=t.total_input_tokens,
                output_tokens=t.total_output_tokens,
                model_name=t.model_name,
                start_time=t.start_time,
                duration_ms=t.duration_ms,
            )
            for t in items
        ]
        return messages, total

    def _count_llm_calls(self, trace_id: str) -> int:
        """Count LLM calls in a trace from spans."""
        llm_calls = 0
        for span in self._spans.get(trace_id, []):
            event_type_str = (
                span.event_type.value
                if hasattr(span.event_type, "value")
                else str(span.event_type)
            )
            if event_type_str == EventType.LLM_INPUT.value:
                llm_calls += 1
        return max(llm_calls, 1)

    def _parse_model_info(
        self,
        model_name: Optional[str],
    ) -> tuple[str, str]:
        """Parse model name into (provider_id, model)."""
        model = model_name or "unknown"
        prov = ""
        if ":" in model:
            prov, model = model.split(":", 1)
            prov = prov.strip()
        return prov, model

    def _aggregate_token_usage(
        self,
        start_date: datetime,
        end_date: datetime,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
    ) -> tuple[int, int, int, dict, dict, dict]:
        """Aggregate token usage from traces.

        Returns:
            Tuple of (total_prompt, total_completion, total_calls,
                     by_model_raw, by_provider_raw, by_date_raw)
        """
        total_prompt = 0
        total_completion = 0
        total_calls = 0
        by_model_raw: dict[str, dict] = {}
        by_provider_raw: dict[str, dict] = {}
        by_date_raw: dict[str, dict] = {}

        for t in self._traces.values():
            if t.start_time < start_date or t.start_time > end_date:
                continue

            prov, model = self._parse_model_info(t.model_name)

            # Apply filters
            if model_name and model != model_name:
                continue
            if provider_id and prov != provider_id:
                continue

            pt = t.total_input_tokens
            ct = t.total_output_tokens
            calls = self._count_llm_calls(t.trace_id)

            total_prompt += pt
            total_completion += ct
            total_calls += calls

            # Aggregate by model
            composite = f"{prov}:{model}" if prov else model
            if composite not in by_model_raw:
                by_model_raw[composite] = {
                    "provider_id": prov,
                    "model": model,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "call_count": 0,
                }
            by_model_raw[composite]["prompt_tokens"] += pt
            by_model_raw[composite]["completion_tokens"] += ct
            by_model_raw[composite]["call_count"] += calls

            # Aggregate by provider
            if prov not in by_provider_raw:
                by_provider_raw[prov] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "call_count": 0,
                }
            by_provider_raw[prov]["prompt_tokens"] += pt
            by_provider_raw[prov]["completion_tokens"] += ct
            by_provider_raw[prov]["call_count"] += calls

            # Aggregate by date
            dt = t.start_time.strftime("%Y-%m-%d")
            if dt not in by_date_raw:
                by_date_raw[dt] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "call_count": 0,
                }
            by_date_raw[dt]["prompt_tokens"] += pt
            by_date_raw[dt]["completion_tokens"] += ct
            by_date_raw[dt]["call_count"] += calls

        return (
            total_prompt,
            total_completion,
            total_calls,
            by_model_raw,
            by_provider_raw,
            by_date_raw,
        )

    async def get_token_summary(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
    ):
        """Get token usage summary compatible with TokenUsageManager.

        This provides backward-compatible token usage data from tracing.

        Args:
            start_date: Optional start date filter
            end_date: Optional end date filter
            model_name: Optional model name filter
            provider_id: Optional provider ID filter

        Returns:
            TokenUsageSummary compatible object
        """
        from swe.token_usage.manager import (
            TokenUsageSummary,
            TokenUsageStats,
            TokenUsageByModel,
        )

        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now()

        (
            total_prompt,
            total_completion,
            total_calls,
            by_model_raw,
            by_provider_raw,
            by_date_raw,
        ) = self._aggregate_token_usage(
            start_date,
            end_date,
            model_name,
            provider_id,
        )

        # Build result objects
        by_model = {
            k: TokenUsageByModel.model_validate(v)
            for k, v in by_model_raw.items()
        }
        by_provider = {
            k: TokenUsageStats.model_validate(v)
            for k, v in by_provider_raw.items()
        }
        by_date = {
            k: TokenUsageStats.model_validate(v)
            for k, v in sorted(by_date_raw.items())
        }

        return TokenUsageSummary(
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_calls=total_calls,
            by_model=by_model,
            by_provider=by_provider,
            by_date=by_date,
        )

    # Historical data loading for date range queries

    async def load_historical_data(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> tuple[dict[str, Trace], dict[str, list[Span]]]:
        """Load historical data from daily files for a date range.

        Args:
            start_date: Start date
            end_date: End date

        Returns:
            Tuple of (traces dict, spans dict)
        """
        traces: dict[str, Trace] = {}
        spans: dict[str, list[Span]] = {}

        current_date = start_date.date()
        end = end_date.date()

        while current_date <= end:
            file_path = self._get_daily_file_path(
                datetime.combine(current_date, datetime.min.time()),
            )
            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    for trace_data in data.get("traces", []):
                        trace = Trace(**trace_data)
                        traces[trace.trace_id] = trace

                    for span_data in data.get("spans", []):
                        span = Span(**span_data)
                        if span.trace_id not in spans:
                            spans[span.trace_id] = []
                        spans[span.trace_id].append(span)

                except Exception as e:
                    logger.warning(
                        "Failed to load historical data from %s: %s",
                        file_path,
                        e,
                    )

            current_date += timedelta(days=1)

        return traces, spans

    async def cleanup_old_data(self, cutoff_date: datetime) -> None:
        """Clean up in-memory data older than the cutoff date.

        This is called by the TraceManager's cleanup loop to remove
        old traces and spans from memory.

        Args:
            cutoff_date: Remove data older than this date
        """
        # Clean up old traces from memory
        trace_ids_to_remove = [
            trace_id
            for trace_id, trace in self._traces.items()
            if trace.start_time < cutoff_date
        ]

        for trace_id in trace_ids_to_remove:
            self._traces.pop(trace_id, None)
            self._spans.pop(trace_id, None)

        if trace_ids_to_remove:
            logger.info(
                "Cleaned up %d old traces from memory (older than %s)",
                len(trace_ids_to_remove),
                cutoff_date.strftime("%Y-%m-%d"),
            )
