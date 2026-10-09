# -*- coding: utf-8 -*-
"""Market 异步任务写入器测试。"""

from __future__ import annotations

import pytest

from market.app.async_tasks import AsyncTaskStore


class FakeDb:
    """记录 SQL 调用的数据库替身。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.executed_many: list[tuple[str, list[tuple]]] = []

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        self.executed.append((sql, params))
        return 1

    async def execute_many(self, sql: str, params_list: list[tuple]) -> int:
        self.executed_many.append((sql, params_list))
        return len(params_list)


@pytest.mark.asyncio
async def test_start_task_inserts_master_and_items() -> None:
    """开始任务时写入主任务和批量目标明细。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        batch_id="batch-1",
        service="market",
        task_type="market.skill.distribute",
        title="分发技能",
        target_ids=["u1", "u2"],
    )

    assert "INSERT INTO swe_async_tasks" in db.executed[0][0]
    assert "tenant_id" not in db.executed[0][0]
    assert db.executed[0][1] is not None
    assert len(db.executed[0][1]) == 12
    assert db.executed[0][1][1] == "batch-1"
    assert db.executed[0][1][8] is None
    assert db.executed[0][1][9] is None
    assert "INSERT IGNORE INTO swe_async_task_items" in db.executed_many[0][0]
    assert db.executed_many[0][1] == [
        ("task-1", "u1", None, "queued", None, None),
        ("task-1", "u2", None, "queued", None, None),
    ]


@pytest.mark.asyncio
async def test_start_task_generates_title_from_task_type() -> None:
    """未显式传标题时，应按任务类型生成主任务标题。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="market",
        task_type="market.mcp.distribute",
        target_ids=["u1"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[5] == "MCP 分发"


@pytest.mark.asyncio
async def test_start_task_generates_summary_from_task_type() -> None:
    """未显式传摘要时，应按任务类型生成默认摘要。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="market",
        task_type="market.skill.distribute",
        target_ids=["u1", "u2"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[6] == "向 2 个用户分发技能"


@pytest.mark.asyncio
async def test_start_task_writes_target_names() -> None:
    """开始任务时应将目标名称写入明细表。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="market",
        task_type="market.skill.distribute",
        target_ids=["u1", "u2"],
        target_names={"u1": "用户一"},
    )

    assert db.executed_many[0][1] == [
        ("task-1", "u1", "用户一", "queued", None, None),
        ("task-1", "u2", None, "queued", None, None),
    ]


@pytest.mark.asyncio
async def test_start_task_keeps_explicit_summary() -> None:
    """显式传摘要时，应保留调用方提供的业务上下文。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="market",
        task_type="market.mcp.distribute",
        summary="向指定用户分发 MCP",
        target_ids=["u1"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[6] == "向指定用户分发 MCP"


@pytest.mark.asyncio
async def test_start_task_keeps_empty_actor_fields() -> None:
    """操作人为空时应保持空值，由页面展示为占位符。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="market",
        task_type="market.mcp.distribute",
        actor_user_id="",
        actor_user_name="",
        target_ids=["u1"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[8] == ""
    assert params[9] == ""


@pytest.mark.asyncio
async def test_record_item_result_updates_item() -> None:
    """单个目标完成时更新明细。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.record_item_result(
        task_id="task-1",
        target_id="u1",
        success=False,
        error_message="failed",
    )

    sql, params = db.executed[0]
    assert "UPDATE swe_async_task_items" in sql
    assert params is not None
    assert params[0] == "failed"
    assert params[1] is None
    assert params[-2:] == ("task-1", "u1")


@pytest.mark.asyncio
async def test_record_item_result_backfills_target_name() -> None:
    """记录目标结果时应从结果中反填目标名称。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.record_item_result(
        task_id="task-1",
        target_id="u1",
        success=True,
        result={"user_name": "用户一"},
    )

    sql, params = db.executed[0]
    assert "target_name = COALESCE(%s, target_name)" in sql
    assert params is not None
    assert params[1] == "用户一"


@pytest.mark.asyncio
async def test_record_item_result_refreshes_master_progress() -> None:
    """单个目标完成后应刷新主任务进度，供任务中心列表展示。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.record_item_result(
        task_id="task-1",
        target_id="u1",
        success=True,
        result={"user_name": "用户一"},
    )

    assert len(db.executed) == 2
    sql, params = db.executed[1]
    assert "UPDATE swe_async_tasks" in sql
    assert "swe_async_task_items" in sql
    assert params == ("task-1", "task-1", "task-1")
