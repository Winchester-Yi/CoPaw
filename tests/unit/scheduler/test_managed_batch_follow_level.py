# -*- coding: utf-8 -*-
"""关注等级：写入、排队刷新、派发顺序与接口校验。"""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fakes.managed_board_schema import add_board_tables
from tests.unit.scheduler.test_managed_cron_management import (
    ManagementDatabase,
)

PROTOCOL = "scheduler_managed_v1"


class FollowLevelDatabase(ManagementDatabase):
    """在真实 SQLite 上补齐批看板与派发工作查询需要的列。"""

    def __init__(self):
        super().__init__()
        add_board_tables(self.conn)
        self.conn.create_function("JSON_UNQUOTE", 1, lambda value: value)
        for column, definition in (
            ("dispatch_meta_json", "TEXT"),
            ("spec_json", "TEXT"),
            ("queue_order", "INTEGER"),
            ("dispatch_order", "INTEGER DEFAULT 0"),
            ("dispatch_lane", "TEXT DEFAULT 'normal'"),
            ("dispatch_lease_until", "TEXT"),
            ("not_before_at", "TEXT"),
            ("scheduled_fire_at", "TEXT"),
            ("provider_id", "TEXT"),
            ("model_id", "TEXT"),
            ("accepted_at", "TEXT"),
            ("started_at", "TEXT"),
            ("finished_at", "TEXT"),
            ("settled_at", "TEXT"),
            ("error_code", "TEXT"),
            ("settled_error_code", "TEXT"),
            ("history_execution_id", "TEXT"),
            ("dispatch_attempt", "INTEGER DEFAULT 0"),
            ("enqueued_state_version", "INTEGER DEFAULT 1"),
            ("kind", "TEXT DEFAULT 'batch'"),
            ("updated_at", "TEXT"),
            ("created_at", "TEXT"),
        ):
            self.conn.execute(
                f"ALTER TABLE swe_cron_execution_runs ADD COLUMN {column} {definition}",
            )

    async def fetch_all(self, query, params=None):
        # SQLite 没有 JSON 列类型；只替换类型转换，仍执行真实聚合。
        query = query.replace("CAST(c.members_json AS JSON)", "c.members_json")
        return await super().fetch_all(query, params)


def manager(actor="manager", source="bank"):
    from scheduler.app.services.cron.managed.identity import CallerIdentity

    return CallerIdentity("alice", source, "default", actor, True)


def seed_batch(
    db,
    batch_id,
    level,
    *,
    members=1,
    batch_order=1,
    lane="normal",
    status="queued",
):
    """建一个父任务 + 受理回执 + 指定数量的成员，可指定成员执行状态。"""
    parent = f"parent-{batch_id}"
    db.add(parent, owner="scheduler")
    db.conn.execute(
        "UPDATE swe_cron_job_states SET batch_follow_level=? WHERE job_id=?",
        (level, parent),
    )
    identity = ("alice", "bank", "default", parent)
    manifest = [
        {
            "execution_id": f"{batch_id}-e{index}",
            "identity": ["alice", "bank", "default", f"{batch_id}-m{index}"],
        }
        for index in range(members)
    ]
    db.conn.execute(
        "INSERT INTO swe_cron_managed_trigger_receipts ("
        "receipt_id,batch_id,tenant_id,source_id,agent_id,job_id,kind,status,"
        "external_job_id,operation_id,logical_fire_at,physical_fire_at,"
        "members_json,created_at) VALUES (?,?,?,?,?,?,'batch','accepted',"
        "'ext','op','2026-10-27 01:00:00','2026-10-27 01:00:00',?,?)",
        (
            f"receipt-{batch_id}",
            batch_id,
            *identity,
            json.dumps(manifest),
            "2026-10-27 01:00:00",
        ),
    )
    executions = []
    for index in range(members):
        member_job = f"{batch_id}-m{index}"
        execution_id = f"{batch_id}-e{index}"
        db.add(member_job, owner="scheduler")
        meta = {
            "protocol": PROTOCOL,
            "batch_id": batch_id,
            "batch_created_at": "2026-10-27 01:00:00",
            "batch_queue_order": batch_order,
            "batch_follow_level": level,
            "dispatch_lane": lane,
        }
        spec = {"task_type": "agent", "runtime": {"max_concurrency": 10}}
        db.conn.execute(
            "INSERT INTO swe_cron_execution_runs ("
            "execution_id,tenant_id,source_id,agent_id,job_id,status,"
            "settled_status,dispatch_meta_json,spec_json,queue_order,"
            "dispatch_order,dispatch_lane,enqueued_state_version,kind,"
            "scheduled_fire_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'batch',?,?,?)",
            (
                execution_id,
                "alice",
                "bank",
                "default",
                member_job,
                status,
                "success" if status == "success" else None,
                json.dumps(meta),
                json.dumps(spec),
                index,
                index,
                lane,
                "2026-10-27 01:00:00",
                "2026-10-27 01:00:00",
                "2026-10-27 01:00:00",
            ),
        )
        db.conn.execute(
            "INSERT INTO swe_cron_managed_batch_members ("
            "batch_id,member_id,tenant_id,source_id,agent_id,job_id,"
            "initial_execution_id,current_execution_id,business_attempt,"
            "member_role,dispatch_order,version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,'child',?,1,?,?)",
            (
                batch_id,
                execution_id,
                "alice",
                "bank",
                "default",
                member_job,
                execution_id,
                execution_id,
                index,
                "2026-10-27 01:00:00",
                "2026-10-27 01:00:00",
            ),
        )
        executions.append(execution_id)
    return parent, executions


@pytest.fixture
def database():
    db = FollowLevelDatabase()
    yield db
    db.conn.close()


def meta_of(db, execution_id):
    row = db.conn.execute(
        "SELECT dispatch_meta_json FROM swe_cron_execution_runs WHERE execution_id=?",
        (execution_id,),
    ).fetchone()
    return json.loads(row[0])


@pytest.mark.asyncio
async def test_set_level_keeps_parent_scoped_audit_and_idempotent(database):
    from scheduler.app.services.cron.managed.batch_follow import (
        set_batch_follow_level,
    )

    db = database
    parent, _ = seed_batch(db, "b-level", 0)
    with pytest.raises(ValueError):
        await set_batch_follow_level(db, manager(), "b-level", 6)
    with pytest.raises(ValueError):
        await set_batch_follow_level(db, manager(), "b-level", True)
    with pytest.raises(PermissionError):
        from scheduler.app.services.cron.managed.identity import CallerIdentity

        await set_batch_follow_level(
            db,
            CallerIdentity("alice", "bank", "default", "reader"),
            "b-level",
            3,
        )
    assert await set_batch_follow_level(db, manager(), "b-level", 4) == {
        "batch_id": "b-level",
        "batch_follow_level": 4,
    }
    await set_batch_follow_level(db, manager("second"), "b-level", 4)
    rows = await db.fetch_all(
        "SELECT job_id,batch_follow_level,batch_follow_updated_by "
        "FROM swe_cron_job_states WHERE batch_follow_level>0",
    )
    assert rows == [
        {
            "job_id": parent,
            "batch_follow_level": 4,
            "batch_follow_updated_by": "manager",
        },
    ]


@pytest.mark.asyncio
async def test_level_change_refreshes_only_queued_members(database):
    from scheduler.app.services.cron.managed.batch_follow import (
        set_batch_follow_level,
        set_task_follow_level,
    )

    db = database
    parent, executions = seed_batch(db, "b-refresh", 0, members=2)
    db.conn.execute(
        "UPDATE swe_cron_job_states SET dispatch_state='batch_enabled' WHERE job_id=?",
        (parent,),
    )
    running = executions[1]
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='running' WHERE execution_id=?",
        (running,),
    )
    await set_batch_follow_level(db, manager(), "b-refresh", 5)
    assert meta_of(db, executions[0])["batch_follow_level"] == 5
    assert meta_of(db, running)["batch_follow_level"] == 0
    await set_task_follow_level(db, manager(), parent, 3)
    assert meta_of(db, executions[0])["batch_follow_level"] == 3
    assert meta_of(db, running)["batch_follow_level"] == 0
    await set_batch_follow_level(db, manager(), "b-refresh", 0)
    assert meta_of(db, executions[0])["batch_follow_level"] == 0


@pytest.mark.asyncio
async def test_dispatch_orders_urgent_then_level_descending(database):
    from scheduler.app.services.cron.managed.work_query import (
        ManagedWorkQuery,
    )

    db = database
    _, low = seed_batch(db, "b-low", 0, batch_order=1)
    _, mid = seed_batch(db, "b-mid", 3, batch_order=2)
    _, high = seed_batch(db, "b-high", 5, batch_order=3)
    _, urgent = seed_batch(db, "b-urgent", 0, batch_order=4, lane="urgent")
    rows = await ManagedWorkQuery(db, 10).rows(running=False)
    order = [row["execution_id"] for row in rows]
    assert order == [
        urgent[0],
        high[0],
        mid[0],
        low[0],
    ]


@pytest.mark.asyncio
async def test_level_change_reorders_already_queued_batches(database):
    from scheduler.app.services.cron.managed.batch_follow import (
        set_batch_follow_level,
    )
    from scheduler.app.services.cron.managed.work_query import (
        ManagedWorkQuery,
    )

    db = database
    _, first = seed_batch(db, "b-first", 1, batch_order=1)
    _, second = seed_batch(db, "b-second", 0, batch_order=2)
    before = [
        row["execution_id"]
        for row in await ManagedWorkQuery(db, 10).rows(running=False)
    ]
    assert before == [first[0], second[0]]
    await set_batch_follow_level(db, manager(), "b-second", 5)
    after = [
        row["execution_id"]
        for row in await ManagedWorkQuery(db, 10).rows(running=False)
    ]
    assert after == [second[0], first[0]]


@pytest.mark.asyncio
async def test_follow_api_accepts_level_only(database):
    from scheduler.app.routers import managed_batch_operations

    db = database
    _, _ = seed_batch(db, "b-api", 0)
    app = FastAPI()
    app.include_router(managed_batch_operations.router, prefix="/api")
    app.dependency_overrides[managed_batch_operations.management_database] = (
        lambda: db
    )
    path = "/api/scheduler/cron/managed-dispatch/batches/b-api/follow"
    identity = {"X-User-Id": "operator", "X-Source-Id": "bank"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(path, json={"level": 3}, headers=identity)
        assert response.status_code == 403
        headers = {**identity, "X-User-Role": "manager"}
        response = await client.put(path, json={"level": "3"}, headers=headers)
        assert response.status_code == 422
        response = await client.put(path, json={"level": 6}, headers=headers)
        assert response.status_code == 422
        response = await client.put(path, json={"level": 3}, headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "batch_id": "b-api",
            "batch_follow_level": 3,
        }


def test_retry_command_does_not_copy_previous_follow_level():
    from scheduler.app.services.cron.managed.daily_retry_enqueue import (
        retry_command,
    )

    row = {
        "execution_id": "e1",
        "scheduled_fire_at": "2026-10-27 01:00:00",
        "spec_json": json.dumps({"meta": {}}),
        "dispatch_meta_json": json.dumps(
            {
                "protocol": PROTOCOL,
                "batch_created_at": "2026-10-27 01:00:00",
                "batch_queue_order": 6,
                "batch_follow_level": 4,
            },
        ),
    }
    command = retry_command(
        {"call_id": "call-1", "cutoff_at": "2026-10-27 02:00:00"},
        row,
        {"definition_version": 1},
        None,
    )
    assert "batch_follow_level" not in command.dispatch_meta
    assert command.dispatch_meta["batch_queue_order"] == 6


@pytest.mark.asyncio
async def test_board_filter_by_follow_level(database):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = database
    seed_batch(db, "b-none", 0, status="success")
    seed_batch(db, "b-three", 3, status="success")
    seed_batch(db, "b-five", 5, status="success")
    service = ManagedBoardQuery(db)

    followed = await service.batches("bank", follow_min=1)
    assert {item["batch_follow_level"] for item in followed["items"]} == {
        3,
        5,
    }
    assert followed["total"] == 2

    unfollowed = await service.batches("bank", follow_max=0)
    assert [item["batch_follow_level"] for item in unfollowed["items"]] == [0]
    assert unfollowed["total"] == 1

    middle = await service.batches("bank", follow_min=3, follow_max=3)
    assert [item["batch_follow_level"] for item in middle["items"]] == [3]
    assert (await service.batches("bank", follow_min=4))["total"] == 1
