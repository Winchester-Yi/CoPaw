# -*- coding: utf-8 -*-
"""Tests for Tracing data isolation via source_id.

Verifies that data belonging to different source_id tenants
is properly scoped and never leaks across boundaries.
"""

# pylint: disable=protected-access,redefined-outer-name,unused-variable

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe.tracing.config import TracingConfig
from swe.tracing.manager import TraceManager
from swe.tracing.models import EventType, Span, Trace
from swe.tracing.store import TraceStore, _matches_trace_filters
from swe.database.config import DatabaseConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trace(source_id: str, trace_id: str = "t-1", **kw) -> Trace:
    """Create a Trace with the given source_id."""
    defaults = {
        "trace_id": trace_id,
        "source_id": source_id,
        "user_id": "user-1",
        "session_id": "session-1",
        "channel": "console",
        "start_time": datetime.now(),
    }
    defaults.update(kw)
    return Trace(**defaults)


def _make_span(source_id: str, span_id: str = "s-1", **kw) -> Span:
    """Create a Span with the given source_id."""
    defaults = {
        "span_id": span_id,
        "trace_id": "t-1",
        "source_id": source_id,
        "name": "test_span",
        "event_type": EventType.LLM_INPUT,
        "start_time": datetime.now(),
    }
    defaults.update(kw)
    return Span(**defaults)


def _insert_values_by_column(execute_mock: MagicMock) -> dict[str, object]:
    """Map an INSERT's explicit column list to its bound parameters."""
    query, params = execute_mock.call_args.args
    column_list = query.split("(", maxsplit=1)[1].split(")", maxsplit=1)[0]
    columns = [column.strip().strip("`") for column in column_list.split(",")]
    return dict(zip(columns, params, strict=True))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    return TracingConfig(enabled=True)


@pytest.fixture
def mock_db():
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


# ---------------------------------------------------------------------------
# _matches_trace_filters — unit-level source_id isn't part of the filter
# helper, but we can still verify it respects user/date boundaries which
# is the complementary axis of isolation.
# ---------------------------------------------------------------------------


class TestMatchesTraceFilters:
    """Tests for the _matches_trace_filters helper."""

    def test_rejects_empty_user_id(self):
        trace = _make_trace("src-A", user_id="")
        assert _matches_trace_filters(trace, None, None, None) is False

    def test_passes_when_no_filters(self):
        trace = _make_trace("src-A", user_id="alice")
        assert _matches_trace_filters(trace, None, None, None) is True

    def test_filters_by_user_id(self):
        trace = _make_trace("src-A", user_id="alice")
        assert _matches_trace_filters(trace, "alice", None, None) is True
        assert _matches_trace_filters(trace, "bob", None, None) is False

    def test_filters_by_date_range(self):
        now = datetime.now()
        trace = _make_trace("src-A", user_id="alice", start_time=now)
        past = datetime(2020, 1, 1)
        future = datetime(2030, 1, 1)
        assert _matches_trace_filters(trace, None, past, future) is True
        assert _matches_trace_filters(trace, None, future, None) is False
        assert _matches_trace_filters(trace, None, None, past) is False


# ---------------------------------------------------------------------------
# TraceStore — source_id scoping on query methods
# ---------------------------------------------------------------------------


class TestStoreSourceIdIsolation:
    """Verify that every TraceStore query includes source_id in its SQL."""

    @pytest.mark.asyncio
    async def test_create_trace_carries_source_id(self, config, mock_db):
        """create_trace must persist the source_id column."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        trace = _make_trace("tenant-A")
        await store.create_trace(trace)

        inserted = _insert_values_by_column(mock_db.execute)
        assert inserted["source_id"] == "tenant-A"
        assert inserted["b3_trace_id"] is None

    @pytest.mark.asyncio
    async def test_create_span_carries_source_id(self, config, mock_db):
        """create_span must persist the source_id column."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        span = _make_span("tenant-A")
        await store.create_span(span)

        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        # source_id is the 3rd positional parameter
        assert params[2] == "tenant-A"

    @pytest.mark.asyncio
    async def test_batch_create_spans_carries_source_id(self, config, mock_db):
        """batch_create_spans must persist source_id per span."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        spans = [_make_span("tenant-A", span_id=f"s-{i}") for i in range(3)]
        await store.batch_create_spans(spans)

        call_args = mock_db.insert_prepared.call_args
        params_list = mock_db.prepared_rows
        for params in params_list:
            # source_id is the 3rd positional parameter in batch insert
            assert params[2] == "tenant-A"

    @pytest.mark.asyncio
    async def test_get_overview_stats_filters_by_source_id(
        self,
        config,
        mock_db,
    ):
        """get_overview_stats must pass source_id to every sub-query."""
        mock_db.fetch_one.return_value = {
            "total_users": 5,
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "total_traces": 10,
            "total_sessions": 3,
            "avg_duration": 200.0,
        }
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_overview_stats(source_id="tenant-A")

        # Every sub-query should have received "tenant-A" as first param
        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-A"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-A"

    @pytest.mark.asyncio
    async def test_get_users_filters_by_source_id(self, config, mock_db):
        """get_users must scope the query to the given source_id."""
        mock_db.fetch_one.return_value = {"total": 1}
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_users(source_id="tenant-B")

        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-B"
        for call in mock_db.fetch_all.call_args_list:
            # First source_id in the extended params list
            assert "tenant-B" in call[0][1]

    @pytest.mark.asyncio
    async def test_get_traces_filters_by_source_id(self, config, mock_db):
        """get_traces must scope the query to the given source_id."""
        now = datetime.now()
        mock_db.fetch_one.return_value = {"total": 0}
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_traces(source_id="tenant-C")

        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-C"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-C"

    @pytest.mark.asyncio
    async def test_get_sessions_filters_by_source_id(self, config, mock_db):
        """get_sessions must scope the query to the given source_id."""
        mock_db.fetch_one.return_value = {"total": 0}
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_sessions(source_id="tenant-D")

        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-D"
        for call in mock_db.fetch_all.call_args_list:
            assert "tenant-D" in call[0][1]

    @pytest.mark.asyncio
    async def test_get_user_stats_filters_by_source_id(self, config, mock_db):
        """get_user_stats must pass source_id to all sub-queries."""
        mock_db.fetch_one.return_value = {
            "total_sessions": 1,
            "total_conversations": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "avg_duration": 50.0,
        }

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_user_stats(source_id="tenant-E", user_id="alice")

        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-E"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-E"

    @pytest.mark.asyncio
    async def test_get_user_messages_filters_by_source_id(
        self,
        config,
        mock_db,
    ):
        """get_user_messages must scope the query to the given source_id."""
        mock_db.fetch_one.return_value = {"total": 0}
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_user_messages(source_id="tenant-F")

        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-F"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-F"

    @pytest.mark.asyncio
    async def test_get_session_stats_filters_by_source_id(
        self,
        config,
        mock_db,
    ):
        """get_session_stats must pass source_id to all sub-queries."""
        mock_db.fetch_one.return_value = {
            "user_id": "alice",
            "channel": "console",
            "total_traces": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "avg_duration": 50.0,
            "first_active": datetime.now(),
            "last_active": datetime.now(),
        }

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_session_stats(
            source_id="tenant-G",
            session_id="session-1",
        )

        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-G"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-G"

    @pytest.mark.asyncio
    async def test_mcp_stats_filters_by_source_id(self, config, mock_db):
        """MCP stat queries must scope by source_id."""
        mock_db.fetch_one.return_value = {
            "total_users": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "total_traces": 0,
            "total_sessions": 0,
            "avg_duration": 0,
        }
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_overview_stats(source_id="tenant-H")

        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-H"

    @pytest.mark.asyncio
    async def test_source_id_in_where_clause_sql(self, config, mock_db):
        """Verify source_id appears in the WHERE clause of key queries."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        # get_traces
        mock_db.fetch_one.return_value = {"total": 0}
        mock_db.fetch_all.return_value = []
        await store.get_traces(source_id="tenant-X")

        count_query = mock_db.fetch_one.call_args[0][0]
        list_query = mock_db.fetch_all.call_args[0][0]
        assert "source_id = %s" in count_query
        assert "source_id = %s" in list_query


# ---------------------------------------------------------------------------
# TraceManager — source_id propagation from lifecycle to storage
# ---------------------------------------------------------------------------


class TestManagerSourceIdPropagation:
    """Verify TraceManager propagates source_id through the full lifecycle."""

    @pytest.mark.asyncio
    async def test_start_trace_stores_source_id(self, config, mock_db):
        """start_trace must persist source_id on the Trace object."""
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="tenant-A",
        )

        trace = manager._active_traces[trace_id]
        assert trace.source_id == "tenant-A"

        # Also verify it was passed to the DB INSERT
        inserted = _insert_values_by_column(mock_db.execute)
        assert inserted["source_id"] == "tenant-A"
        assert inserted["b3_trace_id"] is None

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_span_stores_source_id(self, config, mock_db):
        """emit_span must propagate source_id to the Span object."""
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="tenant-B",
        )

        span_id = await manager.emit_span(
            trace_id=trace_id,
            event_type=EventType.LLM_INPUT,
            name="llm_call",
            source_id="tenant-B",
        )

        span = manager._pending_spans[span_id]
        assert span.source_id == "tenant-B"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_tool_call_start_propagates_source_id(
        self,
        config,
        mock_db,
    ):
        """emit_tool_call_start must pass source_id to the span."""
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="tenant-C",
        )

        span_id = await manager.emit_tool_call_start(
            trace_id=trace_id,
            tool_name="bash",
            tool_input={"cmd": "ls"},
            source_id="tenant-C",
        )

        span = manager._pending_spans[span_id]
        assert span.source_id == "tenant-C"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_skill_invocation_propagates_source_id(
        self,
        config,
        mock_db,
    ):
        """emit_skill_invocation must pass source_id to the span."""
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="tenant-D",
        )

        span_id = await manager.emit_skill_invocation(
            trace_id=trace_id,
            skill_name="pdf",
            source_id="tenant-D",
        )

        span = manager._pending_spans[span_id]
        assert span.source_id == "tenant-D"

        await manager.close()

    @pytest.mark.asyncio
    async def test_emit_llm_input_propagates_source_id(
        self,
        config,
        mock_db,
    ):
        """emit_llm_input must pass source_id to the span."""
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="tenant-E",
        )

        span_id = await manager.emit_llm_input(
            trace_id=trace_id,
            model_name="gpt-4",
            input_tokens=100,
            source_id="tenant-E",
        )

        span = manager._pending_spans[span_id]
        assert span.source_id == "tenant-E"

        await manager.close()

    @pytest.mark.asyncio
    async def test_trace_context_carries_source_id(self, config, mock_db):
        """TraceContext must store source_id for downstream consumers."""
        manager = TraceManager(config, mock_db)
        await manager.initialize()

        trace_id = await manager.start_trace(
            user_id="user-1",
            session_id="session-1",
            channel="console",
            source_id="tenant-F",
        )

        ctx = manager._active_traces  # verify via context
        assert ctx[trace_id].source_id == "tenant-F"

        await manager.close()


# ---------------------------------------------------------------------------
# Cross-tenant isolation scenario
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    """End-to-end scenario: two tenants must not see each other's data."""

    @pytest.mark.asyncio
    async def test_two_tenants_data_does_not_mix(self, config, mock_db):
        """Verify that queries for tenant-A never include tenant-B data.

        We simulate two tenants writing traces and then verify that
        every SQL query for tenant-A has source_id='tenant-A' in the
        WHERE clause params.
        """
        store = TraceStore(config, mock_db)
        await store.initialize()

        # Write trace for tenant-A
        trace_a = _make_trace("tenant-A", trace_id="trace-a")
        await store.create_trace(trace_a)

        # Write trace for tenant-B
        trace_b = _make_trace("tenant-B", trace_id="trace-b")
        await store.create_trace(trace_b)

        # Reset mock to inspect query params
        mock_db.fetch_one.reset_mock()
        mock_db.fetch_all.reset_mock()
        mock_db.fetch_one.return_value = {"total": 0}
        mock_db.fetch_all.return_value = []

        # Query for tenant-A
        await store.get_traces(source_id="tenant-A")

        # All queries should use tenant-A
        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-A"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-A"

    @pytest.mark.asyncio
    async def test_overview_stats_isolated_per_tenant(self, config, mock_db):
        """Overview stats for one tenant must not mix with another."""
        mock_db.fetch_one.return_value = {
            "total_users": 3,
            "input_tokens": 500,
            "output_tokens": 250,
            "total_tokens": 750,
            "total_traces": 50,
            "total_sessions": 10,
            "avg_duration": 100.0,
        }
        mock_db.fetch_all.return_value = []

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_overview_stats(source_id="tenant-X")

        # Verify every sub-query received tenant-X as source_id
        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-X"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-X"

    @pytest.mark.asyncio
    async def test_user_stats_isolated_per_tenant(self, config, mock_db):
        """User stats queries must be scoped to the correct source_id."""
        mock_db.fetch_one.return_value = {
            "total_sessions": 1,
            "total_conversations": 2,
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "avg_duration": 200.0,
        }

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_user_stats(
            source_id="tenant-Y",
            user_id="alice",
        )

        # All sub-queries must use tenant-Y
        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-Y"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-Y"

    @pytest.mark.asyncio
    async def test_spans_written_with_correct_source_id(
        self,
        config,
        mock_db,
    ):
        """Spans batch-written must carry the correct source_id each."""
        store = TraceStore(config, mock_db)
        await store.initialize()

        spans_a = [_make_span("tenant-A", span_id=f"sa-{i}") for i in range(2)]
        spans_b = [_make_span("tenant-B", span_id=f"sb-{i}") for i in range(2)]

        await store.batch_create_spans(spans_a + spans_b)

        call_args = mock_db.insert_prepared.call_args
        all_params = mock_db.prepared_rows

        tenant_a_spans = [p for p in all_params if p[2] == "tenant-A"]
        tenant_b_spans = [p for p in all_params if p[2] == "tenant-B"]

        assert len(tenant_a_spans) == 2
        assert len(tenant_b_spans) == 2

    @pytest.mark.asyncio
    async def test_session_stats_isolated_per_tenant(self, config, mock_db):
        """Session stats must not cross source_id boundaries."""
        mock_db.fetch_one.return_value = {
            "user_id": "alice",
            "channel": "console",
            "total_traces": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "avg_duration": 50.0,
            "first_active": datetime.now(),
            "last_active": datetime.now(),
        }

        store = TraceStore(config, mock_db)
        await store.initialize()

        await store.get_session_stats(
            source_id="tenant-Z",
            session_id="session-1",
        )

        for call in mock_db.fetch_one.call_args_list:
            assert call[0][1][0] == "tenant-Z"
        for call in mock_db.fetch_all.call_args_list:
            assert call[0][1][0] == "tenant-Z"
