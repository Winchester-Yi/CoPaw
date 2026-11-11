# -*- coding: utf-8 -*-
"""真实 MySQL 大批次查询；仅使用显式选择的隔离测试库。"""

import asyncio
import json
from datetime import datetime, timedelta
from time import perf_counter
from uuid import uuid4

import pytest

from tests.integrated.scheduler.test_managed_board_mysql import (
    failed_mysql_batch,
    mysql_pair,
)


async def _insert_rows(db, table, rows):
    if not rows:
        return
    columns = tuple(rows[0])
    await db.execute_many(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ("
        + ",".join("%s" for _ in columns)
        + ")",
        [tuple(row[name] for name in columns) for row in rows],
    )


async def _large_batches(pair, largest):
    caller, first_batch = await failed_mysql_batch(pair)
    db = pair.scheduler
    key = (caller.tenant_id, caller.source_id)
    templates = {}
    for table in (
        "swe_cron_jobs_v2",
        "swe_cron_job_states",
        "swe_cron_execution_runs",
        "swe_cron_managed_trigger_receipts",
        "swe_cron_managed_batch_members",
    ):
        templates[table] = await db.fetch_one(
            f"SELECT * FROM {table} WHERE tenant_id=%s AND source_id=%s LIMIT 1",
            key,
        )
    jobs = [templates["swe_cron_jobs_v2"]["job_id"]]
    definitions, states = [], []
    for index in range(1, largest):
        job_id = str(uuid4())
        jobs.append(job_id)
        spec = json.loads(templates["swe_cron_jobs_v2"]["spec_json"])
        spec.update(id=job_id, name=f"规模验证成员 {index}")
        definitions.append(
            {
                **templates["swe_cron_jobs_v2"],
                "job_id": job_id,
                "name": spec["name"],
                "spec_json": json.dumps(spec, ensure_ascii=False),
            },
        )
        states.append({**templates["swe_cron_job_states"], "job_id": job_id})
    await _insert_rows(db, "swe_cron_jobs_v2", definitions)
    await _insert_rows(db, "swe_cron_job_states", states)

    start = datetime(2026, 11, 2, 16)
    # 原有一个成员加 40*3+8，复刻大批次之外的 42 批、129 成员。
    await db.execute(
        "UPDATE swe_cron_managed_trigger_receipts SET logical_fire_at=%s WHERE batch_id=%s",
        (start, first_batch),
    )
    await db.execute(
        "UPDATE swe_cron_execution_runs SET scheduled_fire_at=%s WHERE execution_id=%s",
        (start, templates["swe_cron_execution_runs"]["execution_id"]),
    )
    receipts, runs, members = [], [], []
    largest_batch = None
    for number, count in enumerate([*([3] * 40), 8, largest], start=1):
        batch_id = str(uuid4())
        logical = start + timedelta(minutes=number)
        manifest = []
        for order, job_id in enumerate(jobs[:count]):
            execution_id = str(uuid4())
            identity = [
                caller.tenant_id,
                caller.source_id,
                caller.agent_id,
                job_id,
            ]
            manifest.append(
                {
                    "execution_id": execution_id,
                    "definition_version": 1,
                    "status": "queued",
                    "reused": False,
                    "identity": identity,
                    "priority": {"priority": 0},
                },
            )
            run = {
                **templates["swe_cron_execution_runs"],
                "execution_id": execution_id,
                "execution_key": uuid4().hex + uuid4().hex,
                "job_id": job_id,
                "scheduled_fire_at": logical,
                "not_before_at": logical,
                "dispatch_order": order,
                "status": "queued",
                "settled_status": None,
                "settled_at": None,
                "settled_error_code": None,
                "error_code": None,
                "history_execution_id": None,
                "dispatch_meta_json": json.dumps(
                    {"protocol": "scheduler_managed_v1", "batch_id": batch_id},
                ),
            }
            run.pop("queue_order")
            runs.append(run)
            members.append(
                {
                    **templates["swe_cron_managed_batch_members"],
                    "batch_id": batch_id,
                    "member_id": execution_id,
                    "initial_execution_id": execution_id,
                    "current_execution_id": execution_id,
                    "job_id": job_id,
                    "dispatch_order": order,
                    "member_role": "parent" if order == 0 else "child",
                },
            )
        receipts.append(
            {
                **templates["swe_cron_managed_trigger_receipts"],
                "receipt_id": str(uuid4()),
                "batch_id": batch_id,
                "logical_fire_at": logical,
                "members_json": json.dumps(manifest, ensure_ascii=False),
            },
        )
        largest_batch = batch_id
    await _insert_rows(db, "swe_cron_execution_runs", runs)
    await _insert_rows(db, "swe_cron_managed_trigger_receipts", receipts)
    await _insert_rows(db, "swe_cron_managed_batch_members", members)
    return caller.source_id, largest_batch


@pytest.mark.parametrize("largest", [933, 5000])
@pytest.mark.asyncio
async def test_mysql_large_batch_list_detail_and_empty_page(
    mysql_pair,
    largest,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    source, batch = await _large_batches(mysql_pair, largest)
    service = ManagedBoardQuery(mysql_pair.scheduler)
    started = perf_counter()
    listed = await asyncio.wait_for(
        service.batches(source, page_size=4),
        timeout=30,
    )
    elapsed = perf_counter() - started
    assert listed["total"] == 43
    assert listed["stats"]["total_members"] == largest + 129
    assert listed["stats"]["failed_members"] == 1
    assert len(listed["items"]) == 4
    assert listed["items"][0]["batch_id"] == batch
    assert listed["items"][0]["member_count"] == largest
    assert listed["items"][0]["status"] == "pending"
    detail = await asyncio.wait_for(service.detail(source, batch), timeout=30)
    assert detail["member_total"] == largest
    assert detail["batch"] == listed["items"][0]
    empty = await asyncio.wait_for(
        service.batches(source, page=100),
        timeout=30,
    )
    assert empty["items"] == []
    assert empty["total"] == 43
    assert empty["stats"] == listed["stats"]
    print(
        json.dumps(
            {"largest_batch": largest, "list_seconds": round(elapsed, 6)},
        ),
    )


@pytest.mark.asyncio
async def test_mysql_large_manifest_keeps_exact_identity_validation(
    mysql_pair,
):
    from monitor.app.services.cron.managed_board import (
        ManagedBoardQuery,
        ManagedBoardUnavailable,
    )

    source, batch = await _large_batches(mysql_pair, 933)
    db = mysql_pair.scheduler
    receipt = await db.fetch_one(
        "SELECT members_json FROM swe_cron_managed_trigger_receipts WHERE batch_id=%s",
        (batch,),
    )
    manifest = json.loads(receipt["members_json"])
    # 保留真实身份前缀，确认解析不能因 VARCHAR 截断而误接受不一致身份。
    manifest[-1]["identity"][0] += "x" * 200
    await db.execute(
        "UPDATE swe_cron_managed_trigger_receipts SET members_json=%s WHERE batch_id=%s",
        (json.dumps(manifest), batch),
    )
    with pytest.raises(ManagedBoardUnavailable):
        await ManagedBoardQuery(db).batches(source, status="completed")
