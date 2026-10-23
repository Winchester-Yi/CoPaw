# -*- coding: utf-8 -*-
"""Tests for TraceStore."""

# pylint: disable=protected-access,redefined-outer-name,unused-variable

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe.tracing.config import TracingConfig
from swe.tracing.models import (
    EventType,
    OverviewStats,
    Span,
    Trace,
    TraceStatus,
)
from swe.tracing.store import TraceStore
from swe.database.config import DatabaseConfig


@pytest.fixture
def config():
    """Create tracing config for tests."""
    return TracingConfig(enabled=True)


@pytest.fixture
def mock_db():
    """Create mock database connection."""
    db = MagicMock()
    db.is_connected = True
    db.config = DatabaseConfig(host="localhost", port=3306, database="test")
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=1)
    db.prepared_rows = []

    async def insert_prepared(_template, rows, **_limits):
        db.prepared_rows = list(rows)
        return len(db.prepared_rows)

    db.insert_prepared = AsyncMock(side_effect=insert_prepared)
    db.close = AsyncMock()
    return db


class TestTraceStoreCreation:
    """Tests for TraceStore initialization."""

    def test_creates_with_db(self, config, mock_db):
        """Test that store is created with database connection."""
        store = TraceStore(config, mock_db)
        assert store.db is mock_db
        assert store._owns_db is False

    def test_owns_db_flag(self, config, mock_db):
        """Test owns_db flag is set correctly."""
        store_with_ownership = TraceStore(config, mock_db, owns_db=True)
        assert store_with_ownership._owns_db is True

        store_without_ownership = TraceStore(config, mock_db, owns_db=False)
        assert store_without_ownership._owns_db is False

    @pytest.mark.asyncio
    async def test_initialize(self, config, mock_db):
        """Test store initialization."""
        store = TraceStore(config, mock_db)
        await store.initialize()
        # Should not raise any errors

    @pytest.mark.asyncio
    async def test_close_with_ownership(self, config, mock_db):
        """Test close closes DB when store owns it."""
        store = TraceStore(config, mock_db, owns_db=True)
        await store.close()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_without_ownership(self, config, mock_db):
        """Test close does not close DB when store doesn't own it."""
        store = TraceStore(config, mock_db, owns_db=False)
        await store.close()
        mock_db.close.assert_not_called()


class TestTraceOperations:
    """Tests for trace CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_trace(self, config, mock_db):
        """Test creating a trace."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        trace = Trace(
            trace_id="trace-1",
            source_id="test-source",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            start_time=datetime.now(),
        )

        await store.create_trace(trace)
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_trace_inserts_b3_trace_id(self, config, mock_db):
        """The trace INSERT stores execution and upstream B3 identities."""
        store = TraceStore(config, mock_db)
        trace = Trace(
            trace_id="execution-trace-id",
            b3_trace_id="upstream-b3-trace-id",
            source_id="test-source",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            start_time=datetime.now(),
        )

        await store.create_trace(trace)

        query, params = mock_db.execute.call_args.args
        normalized_query = " ".join(query.split())
        assert "trace_id, b3_trace_id, source_id" in normalized_query
        assert params[:2] == (
            "execution-trace-id",
            "upstream-b3-trace-id",
        )

    def test_row_to_trace_maps_b3_trace_id(self, config, mock_db):
        """A populated B3 database field is exposed on the trace model."""
        store = TraceStore(config, mock_db)
        row = {
            "trace_id": "execution-trace-id",
            "b3_trace_id": "upstream-b3-trace-id",
            "source_id": "test-source",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "start_time": datetime.now(),
            "end_time": None,
            "duration_ms": None,
            "model_name": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "tools_used": "[]",
            "skills_used": "[]",
            "status": "running",
            "error": None,
        }

        trace = store._row_to_trace(row)

        assert trace.b3_trace_id == "upstream-b3-trace-id"

    def test_row_to_trace_allows_missing_legacy_b3_trace_id(
        self,
        config,
        mock_db,
    ):
        """Rows written before the B3 column was added remain readable."""
        store = TraceStore(config, mock_db)
        legacy_row = {
            "trace_id": "execution-trace-id",
            "source_id": "test-source",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "start_time": datetime.now(),
            "end_time": None,
            "duration_ms": None,
            "model_name": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "tools_used": "[]",
            "skills_used": "[]",
            "status": "running",
            "error": None,
        }

        trace = store._row_to_trace(legacy_row)

        assert trace.b3_trace_id is None

    @pytest.mark.asyncio
    async def test_update_trace(self, config, mock_db):
        """Test updating a trace."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        trace = Trace(
            trace_id="trace-1",
            source_id="test-source",
            user_id="user-1",
            session_id="session-1",
            channel="console",
            start_time=datetime.now(),
            end_time=datetime.now(),
            duration_ms=100,
            status=TraceStatus.COMPLETED,
        )

        await store.update_trace(trace)
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_session_name(self, config, mock_db):
        """更新会话标题时，应只按 trace_id 修改 session_name。"""
        store = TraceStore(config, mock_db)

        await store.update_session_name("trace-1", "销售复盘")

        mock_db.execute.assert_called_once()
        query, params = mock_db.execute.call_args.args
        assert "UPDATE swe_tracing_traces SET session_name = %s" in query
        assert "WHERE trace_id = %s" in query
        assert params == ("销售复盘", "trace-1")

    @pytest.mark.asyncio
    async def test_get_trace(self, config, mock_db):
        """Test getting a trace."""
        now = datetime.now()
        mock_db.fetch_one.return_value = {
            "trace_id": "trace-1",
            "source_id": "test-source",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "start_time": now,
            "end_time": None,
            "duration_ms": None,
            "status": "running",
            "user_message": None,
            "error": None,
            "model_name": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "tools_used": "[]",
            "skills_used": "[]",
        }

        store = TraceStore(config, mock_db)
        await store.initialize()

        trace = await store.get_trace("trace-1")
        assert trace is not None
        assert trace.trace_id == "trace-1"

    @pytest.mark.asyncio
    async def test_get_trace_not_found(self, config, mock_db):
        """Test getting a trace that doesn't exist."""
        mock_db.fetch_one.return_value = None

        store = TraceStore(config, mock_db)
        await store.initialize()

        trace = await store.get_trace("non-existent")
        assert trace is None


class TestSpanOperations:
    """Tests for span CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_span(self, config, mock_db):
        """Test creating a span."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        span = Span(
            span_id="span-1",
            trace_id="trace-1",
            source_id="test-source",
            name="test_span",
            event_type=EventType.LLM_INPUT,
            start_time=datetime.now(),
        )

        await store.create_span(span)
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_multiple_spans(self, config, mock_db):
        """Test creating multiple spans."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        spans = [
            Span(
                span_id=f"span-{i}",
                trace_id="trace-1",
                source_id="test-source",
                name=f"span_{i}",
                event_type=EventType.LLM_INPUT,
                start_time=datetime.now(),
            )
            for i in range(3)
        ]

        await store.batch_create_spans(spans)
        mock_db.insert_prepared.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_span(self, config, mock_db):
        """Test updating a span."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        span = Span(
            span_id="span-1",
            trace_id="trace-1",
            source_id="test-source",
            name="test_span",
            event_type=EventType.LLM_INPUT,
            start_time=datetime.now(),
            end_time=datetime.now(),
            duration_ms=100,
            output_tokens=50,
        )

        await store.update_span(span)
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_spans(self, config, mock_db):
        """Test getting spans for a trace."""
        now = datetime.now()
        mock_db.fetch_all.return_value = [
            {
                "span_id": "span-1",
                "trace_id": "trace-1",
                "source_id": "test-source",
                "name": "test_span",
                "event_type": "llm_input",
                "start_time": now,
                "end_time": None,
                "duration_ms": None,
                "user_id": "user-1",
                "session_id": "session-1",
                "channel": "console",
                "model_name": "gpt-4",
                "input_tokens": 100,
                "output_tokens": None,
                "tool_name": None,
                "skill_name": None,
                "skill_id": None,
                "tool_input": None,
                "tool_output": None,
                "error": None,
                "mcp_server": None,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        spans = await store.get_spans("trace-1")
        assert len(spans) == 1
        assert spans[0].span_id == "span-1"

    @pytest.mark.asyncio
    async def test_get_spans_empty(self, config, mock_db):
        """Test getting spans for a trace with no spans."""
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        spans = await store.get_spans("trace-1")
        assert len(spans) == 0


class TestQueryOperations:
    """Tests for query operations."""

    @pytest.mark.asyncio
    async def test_get_overview_stats(self, config, mock_db):
        """Test getting overview statistics."""

        # Set up side effects for multiple fetch_one and fetch_all calls
        def fetch_one_side_effect(
            *args,
            **_kwargs,
        ):  # pylint: disable=unused-argument
            # Return different results based on query type
            return {
                "total_users": 10,
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "total_traces": 100,
                "total_sessions": 20,
                "avg_duration": 100.0,
            }

        def fetch_all_side_effect(
            *args,
            **_kwargs,
        ):  # pylint: disable=unused-argument
            # Return empty lists for distribution queries
            return []

        mock_db.fetch_one.side_effect = fetch_one_side_effect
        mock_db.fetch_all.side_effect = fetch_all_side_effect

        store = TraceStore(config, mock_db)
        await store.initialize()

        stats = await store.get_overview_stats("test-source")
        assert stats is not None
        assert isinstance(stats, OverviewStats)

    @pytest.mark.asyncio
    async def test_get_users(self, config, mock_db):
        """Test getting users list."""
        mock_db.fetch_all.return_value = [
            {
                "user_id": "user-1",
                "user_name": "User One",
                "bbk_id": "branch-001",
                "total_sessions": 10,
                "total_conversations": 15,
                "total_tokens": 1000,
                "last_active": datetime.now(),
                "total_skills": 5,
            },
            {
                "user_id": "user-2",
                "user_name": None,
                "bbk_id": None,
                "total_sessions": 5,
                "total_conversations": 8,
                "total_tokens": 500,
                "last_active": datetime.now(),
                "total_skills": 2,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        users, total = await store.get_users("test-source")
        assert len(users) == 2

    @pytest.mark.asyncio
    async def test_get_users_pagination(self, config, mock_db):
        """Test getting users with pagination."""
        mock_db.fetch_all.return_value = [
            {
                "user_id": "user-1",
                "user_name": None,
                "bbk_id": None,
                "total_sessions": 10,
                "total_conversations": 15,
                "total_tokens": 1000,
                "last_active": datetime.now(),
                "total_skills": 5,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        users, total = await store.get_users(
            "test-source",
            page=1,
            page_size=10,
        )
        assert len(users) == 1

    @pytest.mark.asyncio
    async def test_get_user_stats(self, config, mock_db):
        """Test getting user statistics."""
        mock_db.fetch_one.return_value = {
            "total_sessions": 10,
            "total_conversations": 15,
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "avg_duration": 100.0,
        }

        store = TraceStore(config, mock_db)
        await store.initialize()

        stats = await store.get_user_stats("test-source", "user-1")
        assert stats is not None

    @pytest.mark.asyncio
    async def test_get_traces(self, config, mock_db):
        """Test getting traces list."""
        now = datetime.now()
        mock_db.fetch_all.return_value = [
            {
                "trace_id": "trace-1",
                "source_id": "test-source",
                "user_id": "user-1",
                "user_name": None,
                "bbk_id": None,
                "session_id": "session-1",
                "channel": "console",
                "start_time": now,
                "duration_ms": None,
                "total_tokens": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "model_name": None,
                "status": "running",
                "skills_count": 0,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        traces, total = await store.get_traces("test-source")
        assert len(traces) == 1

    @pytest.mark.asyncio
    async def test_get_traces_filter_by_user(self, config, mock_db):
        """Test getting traces filtered by user."""
        now = datetime.now()
        mock_db.fetch_all.return_value = [
            {
                "trace_id": "trace-1",
                "source_id": "test-source",
                "user_id": "user-1",
                "user_name": None,
                "bbk_id": None,
                "session_id": "session-1",
                "channel": "console",
                "start_time": now,
                "duration_ms": None,
                "total_tokens": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "model_name": None,
                "status": "running",
                "skills_count": 0,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        traces, total = await store.get_traces("test-source", user_id="user-1")
        assert len(traces) == 1

    @pytest.mark.asyncio
    async def test_get_traces_filter_by_status(self, config, mock_db):
        """Test getting traces filtered by status."""
        now = datetime.now()
        mock_db.fetch_all.return_value = [
            {
                "trace_id": "trace-1",
                "source_id": "test-source",
                "user_id": "user-1",
                "user_name": None,
                "bbk_id": None,
                "session_id": "session-1",
                "channel": "console",
                "start_time": now,
                "duration_ms": 100,
                "total_tokens": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "model_name": None,
                "status": "completed",
                "skills_count": 0,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        traces, total = await store.get_traces(
            "test-source",
            status=TraceStatus.COMPLETED,
        )
        assert len(traces) == 1

    @pytest.mark.asyncio
    async def test_get_traces_filter_by_feedback(self, config, mock_db):
        now = datetime.now()
        mock_db.fetch_all.return_value = [
            {
                "trace_id": "trace-1",
                "source_id": "test-source",
                "user_id": "user-1",
                "user_name": None,
                "bbk_id": None,
                "session_id": "session-1",
                "channel": "console",
                "start_time": now,
                "duration_ms": 100,
                "total_tokens": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "model_name": None,
                "status": "completed",
                "skills_count": 0,
                "feedback_content": "需要优化回答",
                "feedback_updated_at": now,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        traces, total = await store.get_traces(
            "test-source",
            has_feedback=True,
        )

        count_sql = mock_db.fetch_one.call_args_list[-1][0][0]
        query_sql = mock_db.fetch_all.call_args_list[-1][0][0]
        assert "swe_response_feedback" in count_sql
        assert "NULLIF(TRIM(rf.feedback_content), '') IS NOT NULL" in query_sql
        assert traces[0].feedback_content == "需要优化回答"
        assert len(traces) == 1

    @pytest.mark.asyncio
    async def test_get_trace_detail(self, config, mock_db):
        """Test getting trace detail with spans."""
        now = datetime.now()
        mock_db.fetch_one.return_value = {
            "trace_id": "trace-1",
            "source_id": "test-source",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "start_time": now,
            "end_time": None,
            "duration_ms": None,
            "status": "running",
            "user_message": None,
            "error": None,
            "model_name": None,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "tools_used": "[]",
            "skills_used": "[]",
        }
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        detail = await store.get_trace_detail("trace-1")
        assert detail is not None
        assert detail.trace.trace_id == "trace-1"

    @pytest.mark.asyncio
    async def test_get_sessions(self, config, mock_db):
        """Test getting sessions list."""
        mock_db.fetch_all.return_value = [
            {
                "session_id": "session-1",
                "session_name": None,
                "user_id": "user-1",
                "user_name": None,
                "bbk_id": None,
                "channel": "console",
                "total_traces": 5,
                "total_tokens": 1000,
                "first_active": datetime.now(),
                "last_active": datetime.now(),
                "total_skills": 2,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        sessions, total = await store.get_sessions("test-source")
        assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_get_user_messages(self, config, mock_db):
        """Test getting user messages."""
        mock_db.fetch_all.return_value = [
            {
                "trace_id": "trace-1",
                "source_id": "test-source",
                "user_id": "user-1",
                "user_name": None,
                "bbk_id": None,
                "session_id": "session-1",
                "channel": "console",
                "user_message": "Hello",
                "total_input_tokens": 10,
                "total_output_tokens": 20,
                "model_name": None,
                "start_time": datetime.now(),
                "duration_ms": None,
            },
        ]

        store = TraceStore(config, mock_db)
        await store.initialize()

        messages, total = await store.get_user_messages("test-source")
        assert len(messages) == 1


class TestCleanup:
    """Tests for data cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_old_data(self, config, mock_db):
        """Test cleaning up old trace data."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        cutoff_date = datetime.now() - timedelta(days=30)
        await store.cleanup_old_data(cutoff_date)
        # Should execute delete queries
        assert mock_db.execute.called
