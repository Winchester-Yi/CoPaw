# -*- coding: utf-8 -*-
"""Tests for TracingQueryService bbk_ids filtering.

Tests for:
- build_bbk_in_filter helper function
- _build_traces_where_clause bbk_ids parameter handling
- _build_users_query subquery bbk_id filtering
"""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from monitor.app.services.tracing.query_service import (
    build_bbk_in_filter,
    build_cron_bbk_in_filter,
    TracingQueryService,
)
from monitor.app.models.tracing import OverviewBranchBreakdown


class TestBuildBbkInFilter:
    """Tests for build_bbk_in_filter helper function."""

    def test_returns_empty_when_no_bbk_ids(self):
        """Empty bbk_ids should return empty SQL and params."""
        sql, params = build_bbk_in_filter(None)
        assert sql == ""
        assert params == []

    def test_returns_empty_when_empty_string(self):
        """Empty string bbk_ids should return empty SQL and params."""
        sql, params = build_bbk_in_filter("")
        assert sql == ""
        assert params == []

    def test_single_bbk_id(self):
        """Single bbk_id should return correct IN clause."""
        sql, params = build_bbk_in_filter("201")
        assert sql == " AND bbk_id IN (%s)"
        assert params == ["201"]

    def test_multiple_bbk_ids(self):
        """Multiple bbk_ids should return correct IN clause."""
        sql, params = build_bbk_in_filter("201,202,203")
        assert sql == " AND bbk_id IN (%s, %s, %s)"
        assert params == ["201", "202", "203"]

    def test_bbk_ids_with_whitespace(self):
        """bbk_ids with whitespace should be trimmed."""
        sql, params = build_bbk_in_filter(" 201 , 202 , 203 ")
        assert sql == " AND bbk_id IN (%s, %s, %s)"
        assert params == ["201", "202", "203"]

    def test_bbk_100_includes_v00(self):
        """bbk_id 100 (总行) should automatically include V00."""
        sql, params = build_bbk_in_filter("100")
        assert sql == " AND bbk_id IN (%s, %s)"
        assert "100" in params
        assert "V00" in params

    def test_bbk_100_with_other_ids_includes_v00(self):
        """bbk_id 100 with other ids should include V00."""
        sql, params = build_bbk_in_filter("100,201")
        assert len(params) == 3  # 100, V00, 201
        assert "100" in params
        assert "V00" in params
        assert "201" in params


class TestBuildCronBbkInFilter:
    """Tests for build_cron_bbk_in_filter helper function."""

    def test_returns_empty_when_no_bbk_ids(self):
        """Empty bbk_ids should return empty SQL and params."""
        sql, params = build_cron_bbk_in_filter(None)
        assert sql == ""
        assert params == []

    def test_single_bbk_id(self):
        """Single bbk_id should return correct IN clause for cron tables."""
        sql, params = build_cron_bbk_in_filter("201")
        assert sql == " AND j.bbk_id IN (%s)"
        assert params == ["201"]

    def test_bbk_100_includes_v00(self):
        """bbk_id 100 should include V00 for cron tables."""
        sql, params = build_cron_bbk_in_filter("100")
        assert len(params) == 2
        assert "100" in params
        assert "V00" in params


class TestOverviewStatsDetail:
    """Tests for overview payload detail levels."""

    @pytest.fixture
    def service(self):
        """Create TracingQueryService instance with mocked overview readers."""
        service = TracingQueryService(MagicMock())
        service._get_total_users = AsyncMock(return_value=(10, 2, 8))
        service._get_online_users = AsyncMock(return_value=(1, ["u-1"]))
        service._get_token_stats = AsyncMock(
            return_value={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "total_traces": 7,
                "total_sessions": 3,
                "avg_duration": 1200,
            },
        )
        service._get_branch_breakdown = AsyncMock(
            return_value=OverviewBranchBreakdown(),
        )
        service._get_total_skill_calls = AsyncMock(return_value=4)
        service._get_customer_click_stats = AsyncMock(
            return_value={
                "plan_customers": 5,
                "insight_customers": 2,
                "phone_customers": 1,
            },
        )
        service._get_model_distribution = AsyncMock(return_value=[])
        service._get_top_tools = AsyncMock(return_value=[])
        service._get_top_skills = AsyncMock(return_value=[])
        service._get_mcp_stats = AsyncMock(return_value=([], []))
        service._get_growth_stats = AsyncMock(
            return_value={"userGrowth": 5, "planCustomersGrowth": 6},
        )
        return service

    @pytest.mark.asyncio
    async def test_summary_detail_skips_resource_breakdown_queries(
        self,
        service,
    ):
        """Summary overview should avoid resource ranking queries not used by cards."""
        result = await service.get_overview_stats(
            "source-a",
            datetime(2026, 6, 1),
            datetime(2026, 6, 2),
            "100",
            include_resource_breakdown=False,
        )

        assert result.total_users == 10
        assert result.total_tokens == 150
        assert result.plan_customers == 5
        assert result.branch_breakdown == OverviewBranchBreakdown()
        assert result.growth_stats == {
            "userGrowth": 5,
            "planCustomersGrowth": 6,
        }
        service._get_growth_stats.assert_awaited_once()
        service._get_model_distribution.assert_not_awaited()
        service._get_top_tools.assert_not_awaited()
        service._get_top_skills.assert_not_awaited()
        service._get_mcp_stats.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_detail_keeps_existing_resource_queries(
        self,
        service,
    ):
        """Default overview detail should remain backward compatible."""
        await service.get_overview_stats(
            "source-a",
            datetime(2026, 6, 1),
            datetime(2026, 6, 2),
            "100",
        )

        service._get_model_distribution.assert_awaited_once()
        service._get_top_tools.assert_awaited_once()
        service._get_top_skills.assert_awaited_once()
        service._get_mcp_stats.assert_awaited_once()


class TestOverviewBranchBreakdownQueries:
    """Tests for the branch breakdown queries used by overview cards."""

    @pytest.mark.asyncio
    async def test_only_queries_branch_metrics_rendered_by_overview_cards(
        self,
    ):
        db = MagicMock()
        db.fetch_all = AsyncMock(return_value=[])
        service = TracingQueryService(db)

        result = await service._get_branch_breakdown(
            "source-a",
            datetime(2026, 6, 1),
            datetime(2026, 6, 2),
        )

        assert result == OverviewBranchBreakdown()
        assert db.fetch_all.await_count == 5
        queries = [call.args[0] for call in db.fetch_all.await_args_list]
        assert not any("FROM swe_tracing_spans" in query for query in queries)
        assert not any(
            "COUNT(*) AS value" in query and "FROM swe_tracing_traces" in query
            for query in queries
        )

    @pytest.mark.asyncio
    async def test_branch_breakdown_queries_run_in_parallel(self):
        db = MagicMock()
        active_queries = 0
        max_active_queries = 0

        async def fetch_all(*_args):
            nonlocal active_queries, max_active_queries
            active_queries += 1
            max_active_queries = max(max_active_queries, active_queries)
            await asyncio.sleep(0)
            active_queries -= 1
            return []

        db.fetch_all = AsyncMock(side_effect=fetch_all)
        service = TracingQueryService(db)

        await service._get_branch_breakdown(
            "source-a",
            datetime(2026, 6, 1),
            datetime(2026, 6, 2),
        )

        assert max_active_queries == 5


class TestBuildTracesWhereClause:
    """Tests for _build_traces_where_clause bbk_ids handling."""

    @pytest.fixture
    def service(self):
        """Create TracingQueryService instance with mock db."""
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        return TracingQueryService(mock_db)

    def test_includes_bbk_filter_when_bbk_ids_provided(self, service):
        """bbk_ids should be included in WHERE clause."""
        where_sql, params = service._build_traces_where_clause(
            source_id="test-source",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids="201",
            start_date=None,
            end_date=None,
        )
        assert "bbk_id IN" in where_sql
        assert "201" in params

    def test_no_bbk_filter_when_no_bbk_ids(self, service):
        """No bbk_ids should not add bbk_id IN clause."""
        where_sql, params = service._build_traces_where_clause(
            source_id="test-source",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids=None,
            start_date=None,
            end_date=None,
        )
        assert "bbk_id IN" not in where_sql

    def test_bbk_params_in_correct_order(self, service):
        """bbk_params should be in correct order in params list."""
        where_sql, params = service._build_traces_where_clause(
            source_id="test-source",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids="201,202",
            start_date=datetime(2026, 6, 1),
            end_date=datetime(2026, 6, 9),
        )
        # 参数顺序：source_id, "default", 80%, IT%, bbk_params, start_date, end_date
        assert "201" in params
        assert "202" in params


class TestBuildUsersQuerySubqueries:
    """Tests for _build_users_query subquery bbk_id filtering.

    This test class verifies that subqueries correctly filter by bbk_id
    when bbk_ids parameter is provided.
    """

    @pytest.fixture
    def service(self):
        """Create TracingQueryService instance with mock db."""
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        return TracingQueryService(mock_db)

    def test_subqueries_include_bbk_filter_when_source_id_all(self, service):
        """When source_id='all', subqueries should include bbk_id filter."""
        where_sql, params = service._build_traces_where_clause(
            source_id="all",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids="201",
            start_date=None,
            end_date=None,
        )
        cron_sql, cron_params = service._build_cron_subquery(
            source_id="all",
            start_date=None,
            end_date=None,
        )
        query, final_params = service._build_users_query(
            source_id="all",
            where_sql=where_sql,
            cron_subquery_sql=cron_sql,
            order_by="manual_calls DESC, user_id ASC",
            params=params,
            cron_params=cron_params,
            page_size=10,
            offset=0,
        )

        # 验证子查询中包含 bbk_id 过滤
        # total_skills 子查询应该有 bbk_id 过滤
        assert (
            "AND bbk_id IN" in query or "bbk_id IN" in query
        ), "total_skills subquery should filter by bbk_id"

    def test_subqueries_include_bbk_filter_when_source_id_specific(
        self,
        service,
    ):
        """When source_id is specific, subqueries should include bbk_id filter."""
        where_sql, params = service._build_traces_where_clause(
            source_id="test-source",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids="201",
            start_date=None,
            end_date=None,
        )
        cron_sql, cron_params = service._build_cron_subquery(
            source_id="test-source",
            start_date=None,
            end_date=None,
            bbk_ids="201",  # 传递 bbk_ids 参数
        )
        query, final_params = service._build_users_query(
            source_id="test-source",
            where_sql=where_sql,
            cron_subquery_sql=cron_sql,
            order_by="manual_calls DESC, user_id ASC",
            params=params,
            cron_params=cron_params,
            page_size=10,
            offset=0,
            bbk_ids="201",  # 传递 bbk_ids 参数
        )

        # 验证 user_name 子查询包含 bbk_id 过滤
        # bbk_id 子查询应该有 bbk_id 过滤
        assert "AND bbk_id IN" in query, "subqueries should filter by bbk_id"

    def test_cron_subquery_includes_bbk_filter(self, service):
        """_build_cron_subquery should include bbk_id filter when bbk_ids provided."""
        # 这个测试验证修复：_build_cron_subquery 应该接受 bbk_ids 参数
        # 当前实现不接受，所以这个测试会失败
        cron_sql, cron_params = service._build_cron_subquery(
            source_id="all",
            start_date=None,
            end_date=None,
            bbk_ids="201",  # 当前方法签名不支持这个参数
        )
        assert (
            "j.bbk_id IN" in cron_sql
        ), "cron subquery should filter by j.bbk_id"

    def test_user_name_subquery_filters_bbk(self, service):
        """user_name should use MAX aggregation instead of subquery."""
        where_sql, params = service._build_traces_where_clause(
            source_id="test-source",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids="201",
            start_date=None,
            end_date=None,
        )
        cron_sql, cron_params = service._build_cron_subquery(
            source_id="test-source",
            start_date=None,
            end_date=None,
            bbk_ids="201",
        )
        query, final_params = service._build_users_query(
            source_id="test-source",
            where_sql=where_sql,
            cron_subquery_sql=cron_sql,
            order_by="manual_calls DESC, user_id ASC",
            params=params,
            cron_params=cron_params,
            page_size=10,
            offset=0,
            bbk_ids="201",
        )

        # 简化后使用 MAX(t.user_name) 而不是子查询
        assert (
            "MAX(t.user_name)" in query
        ), "user_name should use MAX aggregation after data is complete"

    def test_bbk_id_subquery_filters_bbk(self, service):
        """bbk_id should use MAX aggregation instead of subquery."""
        where_sql, params = service._build_traces_where_clause(
            source_id="test-source",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids="201",
            start_date=None,
            end_date=None,
        )
        cron_sql, cron_params = service._build_cron_subquery(
            source_id="test-source",
            start_date=None,
            end_date=None,
            bbk_ids="201",
        )
        query, final_params = service._build_users_query(
            source_id="test-source",
            where_sql=where_sql,
            cron_subquery_sql=cron_sql,
            order_by="manual_calls DESC, user_id ASC",
            params=params,
            cron_params=cron_params,
            page_size=10,
            offset=0,
            bbk_ids="201",
        )

        # 简化后使用 MAX(t.bbk_id) 而不是子查询
        assert (
            "MAX(t.bbk_id)" in query
        ), "bbk_id should use MAX aggregation after data is complete"

    def test_total_skills_subquery_filters_bbk(self, service):
        """total_skills subquery should filter by bbk_id."""
        where_sql, params = service._build_traces_where_clause(
            source_id="test-source",
            filter_user_type="filtered",
            user_id=None,
            bbk_ids="201",
            start_date=None,
            end_date=None,
        )
        cron_sql, cron_params = service._build_cron_subquery(
            source_id="test-source",
            start_date=None,
            end_date=None,
            bbk_ids="201",
        )
        query, final_params = service._build_users_query(
            source_id="test-source",
            where_sql=where_sql,
            cron_subquery_sql=cron_sql,
            order_by="manual_calls DESC, user_id ASC",
            params=params,
            cron_params=cron_params,
            page_size=10,
            offset=0,
            bbk_ids="201",
        )

        # total_skills 子查询应该有 bbk_id 过滤
        # 格式: SELECT COUNT(*) FROM swe_tracing_spans s WHERE ...
        # 内嵌子查询：SELECT trace_id FROM swe_tracing_traces WHERE ...
        # 应该包含 bbk_id 过滤
        assert "AND bbk_id IN" in query, (
            "total_skills subquery must filter by bbk_id to prevent counting "
            "skills from other bbk branches"
        )


class TestBuildCronSubquerySignature:
    """Tests for _build_cron_subquery method signature.

    This test verifies that _build_cron_subquery accepts bbk_ids parameter.
    """

    @pytest.fixture
    def service(self):
        """Create TracingQueryService instance with mock db."""
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        return TracingQueryService(mock_db)

    def test_accepts_bbk_ids_parameter(self, service):
        """_build_cron_subquery should accept bbk_ids parameter."""
        # 这个测试验证方法签名是否支持 bbk_ids 参数
        # 当前实现不支持，测试会失败
        import inspect

        sig = inspect.signature(service._build_cron_subquery)
        params = sig.parameters

        assert "bbk_ids" in params, (
            "_build_cron_subquery must accept bbk_ids parameter to filter "
            "cron executions by branch"
        )

    def test_includes_bbk_filter_in_sql(self, service):
        """_build_cron_subquery should include bbk_id filter in SQL."""
        # 这个测试会在修复后通过
        cron_sql, cron_params = service._build_cron_subquery(
            source_id="test-source",
            start_date=None,
            end_date=None,
            bbk_ids="201",
        )
        assert (
            "j.bbk_id IN" in cron_sql
        ), "cron subquery must include j.bbk_id IN filter"
        assert "201" in cron_params


class TestBuildUsersQuerySignature:
    """Tests for _build_users_query method signature."""

    @pytest.fixture
    def service(self):
        """Create TracingQueryService instance with mock db."""
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        return TracingQueryService(mock_db)

    def test_accepts_bbk_ids_parameter(self, service):
        """_build_users_query should accept bbk_ids parameter."""
        import inspect

        sig = inspect.signature(service._build_users_query)
        params = sig.parameters

        assert "bbk_ids" in params, (
            "_build_users_query must accept bbk_ids parameter to pass "
            "to subqueries for bbk filtering"
        )


def test_users_query_binds_datetime_values_to_cron_time_placeholders():
    """Source IDs must not shift into cron DATETIME placeholders."""
    from unittest.mock import MagicMock

    service = TracingQueryService(MagicMock())
    start_date = datetime(2026, 7, 14)
    end_date = datetime(2026, 7, 15)
    where_sql, params = service._build_traces_where_clause(
        source_id="RMASSIST",
        filter_user_type="filtered",
        user_id=None,
        bbk_ids="201",
        start_date=start_date,
        end_date=end_date,
    )
    cron_sql, cron_params = service._build_cron_subquery(
        source_id="RMASSIST",
        start_date=start_date,
        end_date=end_date,
        bbk_ids="201",
    )
    query, final_params = service._build_users_query(
        source_id="RMASSIST",
        where_sql=where_sql,
        cron_subquery_sql=cron_sql,
        order_by="manual_calls DESC, user_id ASC",
        params=params,
        cron_params=cron_params,
        page_size=20,
        offset=0,
        bbk_ids="201",
    )

    actual_time_start_index = query[
        : query.index("e.actual_time >= %s")
    ].count(
        "%s",
    )
    actual_time_end_index = query[: query.index("e.actual_time < %s")].count(
        "%s",
    )

    assert query.count("%s") == len(final_params)
    assert final_params[actual_time_start_index] == start_date
    assert final_params[actual_time_end_index] == end_date


@pytest.mark.asyncio
async def test_get_skills_paginated_excludes_internal_skills():
    """技能排行榜 SQL 应屏蔽内部技能，避免运营看板被系统能力刷榜。"""
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value={"total": 0})
    db.fetch_all = AsyncMock(return_value=[])
    service = TracingQueryService(db)

    await service.get_skills_paginated(
        source_id="RMASSIST",
        page=2,
        page_size=20,
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 9),
        bbk_ids="201",
    )

    count_query, count_params = db.fetch_one.await_args.args
    data_query, data_params = db.fetch_all.await_args.args

    assert "skill_name NOT IN (%s, %s)" in count_query
    assert "skill_name NOT IN (%s, %s)" in data_query
    assert "cron" in count_params
    assert "skill-creator" in count_params
    assert "cron" in data_params
    assert "skill-creator" in data_params
    assert data_params[-2:] == (20, 20)
    assert data_query.count("%s") == len(data_params)


@pytest.mark.asyncio
async def test_get_top_skills_excludes_internal_skills():
    """首页热门技能 SQL 应与分页排行榜保持同一屏蔽口径。"""
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    service = TracingQueryService(db)

    await service._get_top_skills(
        source_id="all",
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 9),
        bbk_ids="201",
    )

    query, params = db.fetch_all.await_args.args

    assert "skill_name NOT IN (%s, %s)" in query
    assert "cron" in params
    assert "skill-creator" in params
    assert query.count("%s") == len(params)
