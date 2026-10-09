# -*- coding: utf-8 -*-
"""异步任务写入器测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

from swe.app.async_tasks import AsyncTaskStore


class FakeDb:
    """记录执行 SQL 的轻量数据库替身。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.executed_many: list[tuple[str, list[tuple]]] = []

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        self.executed.append((sql, params))
        return 1

    async def execute_many(self, sql: str, params_list: list[tuple]) -> int:
        self.executed_many.append((sql, params_list))
        return len(params_list)

    async def fetch_one(
        self,
        sql: str,
        params: tuple | None = None,
    ) -> dict | None:
        self.executed.append((sql, params))
        return {"total": 1}


@pytest.mark.asyncio
async def test_start_task_inserts_master_and_items() -> None:
    """开始任务时应同时写入主任务和批量明细。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="swe",
        task_type="provider.providers.distribute",
        title="分发供应商配置",
        target_ids=["tenant-a", "tenant-b"],
    )

    assert len(db.executed) == 1
    assert len(db.executed_many) == 1
    assert "INSERT INTO swe_async_tasks" in db.executed[0][0]
    assert "tenant_id" not in db.executed[0][0]
    assert db.executed[0][1] is not None
    assert len(db.executed[0][1]) == 12
    assert db.executed[0][1][7] is None
    assert db.executed[0][1][8] is None
    assert "INSERT IGNORE INTO swe_async_task_items" in db.executed_many[0][0]
    assert db.executed_many[0][1] == [
        ("task-1", "tenant-a", None, "queued", None, None),
        ("task-1", "tenant-b", None, "queued", None, None),
    ]


@pytest.mark.asyncio
async def test_start_task_generates_title_from_task_type() -> None:
    """未显式传标题时，应按任务类型生成主任务标题。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="swe",
        task_type="provider.active_model.distribute",
        target_ids=["tenant-a"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[5] == "模型分发"


@pytest.mark.asyncio
async def test_start_task_generates_summary_from_task_type() -> None:
    """未显式传摘要时，应按任务类型生成默认摘要。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="swe",
        task_type="provider.providers.distribute",
        target_ids=["tenant-a", "tenant-b"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[6] == "向 2 个用户分发供应商配置"


@pytest.mark.asyncio
async def test_start_task_writes_target_names() -> None:
    """开始任务时应将目标名称写入明细表。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="swe",
        task_type="provider.providers.distribute",
        target_ids=["user-a", "user-b"],
        target_names={"user-a": "用户A"},
    )

    assert db.executed_many[0][1] == [
        ("task-1", "user-a", "用户A", "queued", None, None),
        ("task-1", "user-b", None, "queued", None, None),
    ]


@pytest.mark.asyncio
async def test_start_task_keeps_explicit_summary() -> None:
    """显式传摘要时，应保留调用方提供的业务上下文。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="swe",
        task_type="cron.broadcast.distribute",
        summary="将任务 job-1 广播到 2 个用户",
        target_ids=["tenant-a", "tenant-b"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[6] == "将任务 job-1 广播到 2 个用户"


@pytest.mark.asyncio
async def test_start_task_keeps_empty_actor_fields() -> None:
    """操作人为空时应保持空值，由页面展示为占位符。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.start_task(
        task_id="task-1",
        service="swe",
        task_type="tenant.bootstrap",
        actor_user_id="",
        actor_user_name="",
        target_ids=["tenant-a"],
    )

    params = db.executed[0][1]
    assert params is not None
    assert params[8] == ""
    assert params[9] == ""


@pytest.mark.asyncio
async def test_record_item_result_updates_single_item() -> None:
    """单个目标完成时应只更新对应明细。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.record_item_result(
        task_id="task-1",
        target_id="tenant-a",
        success=True,
        result={"ok": True},
        error_message=None,
    )

    assert len(db.executed) == 2
    sql, params = db.executed[0]
    assert "UPDATE swe_async_task_items" in sql
    assert params is not None
    assert params[0] == "succeeded"
    assert params[1] is None
    assert params[2] == '{"ok": true}'
    assert params[-2:] == ("task-1", "tenant-a")


@pytest.mark.asyncio
async def test_record_item_result_accepts_custom_item_status() -> None:
    """特殊分发明细可写入比成功失败更细的状态。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.record_item_result(
        task_id="task-1",
        target_id="tenant-a",
        success=True,
        item_status="skipped",
        result={"status": "skipped"},
    )

    _sql, params = db.executed[0]
    assert params is not None
    assert params[0] == "skipped"
    assert params[2] == '{"status": "skipped"}'


@pytest.mark.asyncio
async def test_record_item_result_backfills_target_name() -> None:
    """记录目标结果时应从结果中反填目标名称。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.record_item_result(
        task_id="task-1",
        target_id="user-a",
        success=True,
        result={"tenant_name": "用户A"},
    )

    sql, params = db.executed[0]
    assert "target_name = COALESCE(%s, target_name)" in sql
    assert params is not None
    assert params[1] == "用户A"


@pytest.mark.asyncio
async def test_record_item_result_refreshes_master_progress() -> None:
    """单个目标完成后，主任务进度应立即反映明细状态。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.record_item_result(
        task_id="task-1",
        target_id="user-a",
        success=True,
        result={"tenant_name": "用户A"},
    )

    assert len(db.executed) == 2
    sql, params = db.executed[1]
    assert "UPDATE swe_async_tasks" in sql
    assert "swe_async_task_items" in sql
    assert params == ("task-1", "task-1", "task-1")


@pytest.mark.asyncio
async def test_finish_task_updates_summary() -> None:
    """完成任务时应汇总更新主任务状态。"""
    db = FakeDb()
    store = AsyncTaskStore(db)

    await store.finish_task(
        task_id="task-1",
        status="partial_failed",
        done_count=1,
        failed_count=1,
        error_message="部分目标失败",
        result={"done": 1, "failed": 1},
        finished_at=datetime(2026, 7, 17, 9, 30, 0),
    )

    assert len(db.executed) == 1
    sql, params = db.executed[0]
    assert "UPDATE swe_async_tasks" in sql
    assert params is not None
    assert params[0] == "partial_failed"
    assert params[1] == 1
    assert params[2] == 1
    assert params[3] == "部分目标失败"
    assert params[4] == '{"done": 1, "failed": 1}'
