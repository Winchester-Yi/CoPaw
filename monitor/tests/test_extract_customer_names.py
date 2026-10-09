# -*- coding: utf-8 -*-
"""Tests for customer name extraction queries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.app.services.tracing.extract_service import (
    ExtractCustomerNamesService,
)


@pytest.mark.asyncio
async def test_query_traces_by_skill_does_not_filter_event_type():
    """客户姓名提取查询不应限制 span 的 event_type。"""
    db = SimpleNamespace(
        fetch_all=AsyncMock(return_value=[]),
    )
    service = ExtractCustomerNamesService()

    await service._query_traces_by_skill(
        db=db,
        skill_names=["skill-a"],
        user_ids=None,
        bbk_id=None,
        start_date=None,
        end_date=None,
    )

    query, params = db.fetch_all.await_args.args

    assert "event_type" not in query
    assert "s.skill_name IN (%s)" in query
    assert "e.trace_id = s.trace_id" in query
    assert params == ("skill-a",)
