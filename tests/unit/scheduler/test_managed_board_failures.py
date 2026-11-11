# -*- coding: utf-8 -*-
"""全局失败悬停统计必须与原看板口径一致，不重复计历史尝试。"""

import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)
from monitor.app.services.cron.managed_board import (
    ManagedBoardQuery,
    ManagedBoardUnavailable,
)


@pytest.fixture(autouse=True)
def mysql_json_type_semantics(callback_setup):
    db = callback_setup[0]
    fetch = db.fetch_all

    async def compatible(query, params=None):
        # SQLite提取字符串时已去掉JSON引号；改用其带路径的类型函数，仍执行真实分组SQL。
        query = query.replace(
            "JSON_TYPE(JSON_EXTRACT(safe_result,'$.error_detail'))",
            "CASE WHEN JSON_TYPE(safe_result,'$.error_detail')='text' THEN 'STRING' ELSE 'OTHER' END",
        )
        return await fetch(query, params)

    db.fetch_all = compatible


def duplicate_batch(
    db,
    number,
    *,
    message,
    category="configuration",
    status="error",
    settled=None,
    error_type=None,
):
    def copy(table):
        return dict(
            db.conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone(),
        )

    def insert(table, row):
        return db.conn.execute(
            f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        ).lastrowid

    history = copy("swe_cron_executions")
    history.pop("id")
    history["error_message"] = message
    history["error_type"] = error_type
    history_id = insert("swe_cron_executions", history)
    run = copy("swe_cron_execution_runs")
    run.pop("queue_order")
    run.update(
        execution_id=f"exec-{number}",
        execution_key=f"key-{number}",
        status=status,
        settled_status=settled,
        history_execution_id=history_id,
        settled_error_code=(
            "cron_async_subtasks_failed" if settled == "error" else None
        ),
        result_json=json.dumps({"failure_type": category}),
    )
    insert("swe_cron_execution_runs", run)
    receipt = copy("swe_cron_managed_trigger_receipts")
    manifest = json.loads(receipt["members_json"])
    manifest[0]["execution_id"] = run["execution_id"]
    receipt.update(
        receipt_id=f"receipt-{number}",
        callback_key=f"callback-{number}",
        batch_id=f"batch-{number}",
        members_json=json.dumps(manifest),
    )
    insert("swe_cron_managed_trigger_receipts", receipt)
    member = copy("swe_cron_managed_batch_members")
    member.update(
        batch_id=receipt["batch_id"],
        member_id=run["execution_id"],
        initial_execution_id=run["execution_id"],
        current_execution_id=run["execution_id"],
    )
    insert("swe_cron_managed_batch_members", member)


@pytest.mark.asyncio
async def test_subtask_error_types_keep_member_and_detail_totals(
    callback_setup,
):
    db = callback_setup[0]
    await failed_batch(callback_setup, category="configuration")
    for number, error_type in enumerate(("Prompt", "Prompt", ""), start=2):
        duplicate_batch(
            db,
            number,
            message="子任务失败",
            status="success",
            settled="error",
            error_type=error_type,
        )
    summary = await ManagedBoardQuery(db).failure_summary("bank")
    groups = {g["failure_type"]: g for g in summary["groups"]}
    failure = groups["subtask_failed"]
    assert summary["total"] == 4
    assert failure["count"] == 3
    assert failure["details"] == [{"message": "子任务失败", "count": 3}]
    assert failure["error_types"] == [
        {"error_type": "Prompt", "count": 2},
        {"error_type": None, "count": 1},
    ]
    assert "error_types" not in groups["configuration"]


@pytest.mark.asyncio
async def test_global_failure_groups_include_exact_details_and_current_members(
    callback_setup,
):
    db = callback_setup[0]
    await failed_batch(callback_setup, category="configuration")
    db.conn.execute(
        "UPDATE swe_cron_executions SET error_message='缺少用户信息'",
    )
    duplicate_batch(db, 2, message="缺少用户信息")
    duplicate_batch(db, 3, message="绑定配置不存在")
    duplicate_batch(
        db,
        4,
        message="报告子任务失败",
        status="success",
        settled="error",
    )
    duplicate_batch(db, 5, message="投递结果未知", status="dispatch_failed")
    service = ManagedBoardQuery(db)
    summary = await service.failure_summary("bank")
    assert summary["total"] == 4
    groups = {g["failure_type"]: g for g in summary["groups"]}
    assert groups["configuration"]["count"] == 3
    assert groups["configuration"]["details"] == [
        {"message": "缺少用户信息", "count": 2},
        {"message": "绑定配置不存在", "count": 1},
    ]
    assert groups["subtask_failed"]["count"] == 1
    assert (await service.batches("bank", page_size=1))["stats"][
        "failed_members"
    ] == summary["total"]
    assert len((await service.batches("bank", page_size=1))["items"]) == 1


@pytest.mark.asyncio
async def test_summary_filters_and_history_identity_are_strict(callback_setup):
    db = callback_setup[0]
    await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_executions SET tenant_id='ALICE',error_message='foreign-private'",
    )
    service = ManagedBoardQuery(db)
    result = await service.failure_summary(
        "bank",
        start=datetime(2026, 10, 16, 9),
        end=datetime(2026, 10, 16, 9),
        status="failed",
        query="report",
    )
    assert result["total"] == 1
    assert "foreign-private" not in str(result)
    assert result["groups"][0]["details"] == [
        {"message": "cron_execution_failed", "count": 1},
    ]
    for filters in (
        {"status": "completed"},
        {"query": "%"},
        {"start": datetime(2026, 10, 17)},
    ):
        assert (await service.failure_summary("bank", **filters))["total"] == 0
    assert (await service.failure_summary("BANK"))["total"] == 0


@pytest.mark.asyncio
async def test_retried_member_no_longer_counts_its_previous_failure(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    retry = BatchRetryService(db)
    await retry.retry(
        caller,
        batch,
        retry_body(await retry.preview(caller, batch)),
    )
    result = await ManagedBoardQuery(db).failure_summary("bank")
    assert result["total"] == 0
    assert result["groups"] == []


@pytest.mark.asyncio
async def test_invalid_relation_is_not_hidden_by_failure_status_filter(
    callback_setup,
):
    db = callback_setup[0]
    await failed_batch(callback_setup)
    db.conn.execute("DELETE FROM swe_cron_managed_batch_members")
    with pytest.raises(ManagedBoardUnavailable):
        await ManagedBoardQuery(db).failure_summary("bank", status="completed")


@pytest.mark.asyncio
async def test_failure_summary_http_is_manager_only_and_read_only(
    callback_setup,
):
    from monitor.app.routers import managed_cron_board as api

    db = callback_setup[0]
    await failed_batch(callback_setup)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.board_service] = lambda: ManagedBoardQuery(db)
    headers = {
        "X-User-Id": "alice",
        "X-Source-Id": "bank",
        "X-User-Role": "manager",
    }
    path = "/api/monitor/cron/managed-dispatch/failure-summary"
    before = db.conn.total_changes
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (
            await client.get(path, headers={**headers, "X-User-Role": "user"})
        ).status_code == 403
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert (
            await client.get(
                path,
                headers=headers,
                params={"start_time": "2026-11-05", "end_time": "2026-11-04"},
            )
        ).status_code == 400
    assert db.conn.total_changes == before
