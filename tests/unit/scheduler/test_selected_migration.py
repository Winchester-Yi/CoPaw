# -*- coding: utf-8 -*-
"""Real SQL tests for selected-group publication and restart-safe retries."""

import copy
import pytest

from tests.unit.scheduler.test_managed_cron_executions import ExecutionDatabase
from tests.unit.scheduler.test_managed_cron_management import PlatformClient
from scheduler.app.services.cron.managed.identity import (
    CallerIdentity,
    encode_scope,
)


def member(job, tenant="alice", parent=None):
    key = (tenant, "bank", "default", job)
    spec = {
        "id": job,
        "tenant_id": tenant,
        "source_id": "bank",
        "name": job,
        "scope_id": encode_scope(tenant, "bank"),
        "enabled": True,
        "task_type": "text",
        "text": "hello",
        "schedule": {
            "type": "cron",
            "cron": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "run_on_non_workday": False,
        },
        "meta": {
            "creator_user_id": tenant,
            "task_chat_id": f"chat-{job}",
            "task_session_id": f"session-{job}",
        },
    }
    definition = dict(
        zip(("tenant_id", "source_id", "agent_id", "job_id"), key),
    )
    definition.update(
        spec_json=spec,
        creator_user_id=tenant,
        job_origin="manual",
    )
    if parent:
        definition.update(
            dict(
                zip(
                    (
                        "parent_tenant_id",
                        "parent_source_id",
                        "parent_agent_id",
                        "parent_job_id",
                    ),
                    parent,
                ),
            ),
        )
    return {
        "definition": definition,
        "state": {
            "task_chat_id": f"chat-{job}",
            "task_session_id": f"session-{job}",
            "unread_execution_count": 7,
            "last_status": "success",
            "dispatch_state": "ordinary",
        },
        "history_max_id": 0,
        "warnings": [],
        "memory_available": True,
        "legacy_runtime": {},
    }


class Source:
    def __init__(self, groups):
        self.groups = groups
        self.stopped = []

    async def migration_stop_legacy(self, operation, caller):
        self.stopped.append((caller.tenant_id, operation["job_id"]))
        return {"stopped": True}

    async def migration_snapshot(self, caller, job_id):
        return copy.deepcopy(
            {
                "root": list(caller.job_key(job_id)),
                "members": self.groups[job_id],
            },
        )


def setup():
    from scheduler.app.services.cron.managed.selected_migration import (
        SelectedMigrationService,
    )

    db, platform = ExecutionDatabase(), PlatformClient()
    db.conn.executescript("""
        CREATE TABLE swe_cron_dispatch_batches (
            batch_id TEXT, tenant_id TEXT, source_id TEXT, agent_id TEXT,
            parent_job_id TEXT
        );
        CREATE TABLE swe_cron_dispatch_intents (batch_id TEXT, status TEXT);
    """)
    caller = CallerIdentity("alice", "bank", "default", "alice")
    source = Source(
        {
            "parent": [
                member("parent"),
                member("child", "bob", caller.job_key("parent")),
            ],
        },
    )
    service = SelectedMigrationService(db, source, platform)
    return db, platform, caller, service


@pytest.mark.asyncio
async def test_old_batch_must_drain_before_file_stop_and_platform_enable():
    db, platform, caller, service = setup()
    try:
        for member in service.source.groups["parent"]:
            member["state"]["dispatch_state"] = "batch_enabled"
        db.conn.execute(
            "INSERT INTO swe_cron_dispatch_batches VALUES "
            "('old-batch','alice','bank','default','parent')",
        )
        db.conn.execute(
            "INSERT INTO swe_cron_dispatch_intents VALUES ('old-batch','pending')",
        )
        batch = await service.submit(
            caller,
            {"request_id": "drain", "job_ids": ["parent"]},
        )
        op = batch["operation_id"]
        await service.run(caller, op)
        await service.confirm(caller, op)
        await service.run(caller, op)
        result = await service.get(caller, op)
        assert (
            result["groups"][0]["error_code"]
            == "cron_migration_legacy_batch_not_drained"
        )
        assert not service.source.stopped
        assert "enable" not in platform.calls
        db.conn.execute(
            "UPDATE swe_cron_dispatch_intents SET status='completed'",
        )
        await service.confirm(caller, op)
        await service.run(caller, op)
        assert (await service.get(caller, op))["status"] == "completed"
        assert set(service.source.stopped) == {
            ("alice", "parent"),
            ("bob", "child"),
        }
        assert platform.calls.count("register") == 1
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_channel_migration_lists_own_source_and_keeps_real_actor():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.routers import selected_cron_migration as api

    db, _, _, service = setup()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.management_database] = lambda: db
    app.dependency_overrides[api.migration_service] = lambda: service
    db.conn.executemany(
        "INSERT INTO swe_cron_jobs (id,tenant_id,source_id,name,meta) "
        "VALUES (?,?,?,?,?)",
        [
            ("parent", "alice", "bank", "父任务", '{"agent_id":"default"}'),
            (
                "independent",
                "bob",
                "bank",
                "独立任务",
                '{"agent_id":"default"}',
            ),
            ("outside", "eve", "other", "其他渠道", '{"agent_id":"default"}'),
        ],
    )
    path = "/api/scheduler/cron/migrations/channel"
    headers = {"X-User-Id": "admin-user", "X-Source-Id": "bank"}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            assert (
                await client.get(f"{path}/jobs", headers=headers)
            ).status_code == 403
            headers["X-User-Role"] = "manager"
            response = await client.get(f"{path}/jobs", headers=headers)
            assert response.status_code == 200
            assert {item["job_id"] for item in response.json()["items"]} == {
                "parent",
                "independent",
            }
            submitted = await client.post(
                f"{path}/operations",
                headers=headers,
                json={"request_id": "admin", "job_ids": ["parent"]},
            )
            assert submitted.status_code == 202
            operation_id = submitted.json()["items"][0]["operation_id"]
            row = db.conn.execute(
                "SELECT tenant_id,actor_id FROM swe_cron_registration_journal WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            assert tuple(row) == ("alice", "admin-user")
            headers["X-Source-Id"] = "other"
            response = await client.get(
                f"{path}/operations/{operation_id}",
                headers=headers,
            )
            assert response.status_code == 404
    finally:
        db.conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_pause", [False, True])
async def test_legacy_timers_pause_before_new_enable_and_retry(fail_pause):
    db, platform, caller, service = setup()
    root, child = service.source.groups["parent"]
    root["legacy_runtime"].update(
        ordinary_external_job_id="101",
        batch_external_job_id="102",
    )
    child["legacy_runtime"]["ordinary_external_job_id"] = "103"
    for item in (root, child):
        item["state"]["dispatch_state"] = "batch_enabled"
    calls = []
    should_fail = fail_pause

    async def set_enabled(timer, enabled):
        calls.append((timer, enabled))
        if timer == "102" and should_fail:
            raise RuntimeError("legacy timer pause rejected")

    platform.set_enabled = set_enabled
    try:
        batch = await service.submit(
            caller,
            {"request_id": "legacy", "job_ids": ["parent"]},
        )
        operation = batch["operation_id"]
        await service.run(caller, operation)
        assert not calls and not platform.calls
        await service.confirm(caller, operation)
        await service.run(caller, operation)
        result = await service.get(caller, operation)
        if fail_pause:
            assert result["status"] == "failed"
            assert result["groups"][0]["error_code"] == (
                "cron_migration_legacy_timer_pause_failed"
            )
            assert not any(enabled for _, enabled in calls)
            assert {
                row[0]
                for row in db.conn.execute(
                    "SELECT management_status FROM swe_cron_job_states",
                )
            } == {"migrating"}
            should_fail = False
            calls.clear()
            await service.confirm(caller, operation)
            await service.run(caller, operation)
            result = await service.get(caller, operation)

        assert result["status"] == "completed"
        assert calls[0] == ("timer-parent", False)
        assert set(calls[1:-1]) == {
            ("101", False),
            ("102", False),
            ("103", False),
        }
        assert len(calls) == 5
        assert calls[-1] == ("timer-parent", True)
        assert platform.calls.count("register") == 1
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_preview_then_publish_keeps_ids_and_does_not_write_old_table():
    db, platform, caller, service = setup()
    try:
        batch = await service.submit(
            caller,
            {"request_id": "one", "job_ids": ["parent"]},
        )
        await service.run(caller, batch["operation_id"])
        preview = await service.get(caller, batch["operation_id"])
        assert preview["status"] == "awaiting_confirmation"
        assert preview["groups"][0]["member_count"] == 2
        assert not platform.calls
        await service.confirm(caller, batch["operation_id"])
        await service.run(caller, batch["operation_id"])
        assert (await service.get(caller, batch["operation_id"]))[
            "status"
        ] == "completed"
        assert (
            db.conn.execute("SELECT count(*) FROM swe_cron_jobs").fetchone()[0]
            == 0
        )
        assert list(
            db.conn.execute(
                "SELECT job_id,task_chat_id,task_session_id FROM swe_cron_job_states ORDER BY job_id",
            ),
        )[0][:] == ("child", "chat-child", "session-child")
        assert all(
            r[0] == "ready"
            for r in db.conn.execute(
                "SELECT management_status FROM swe_cron_job_states",
            )
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_group_failure_never_publishes_and_retry_reuses_known_timers():
    db, platform, caller, service = setup()
    try:
        batch = await service.submit(
            caller,
            {"request_id": "one", "job_ids": ["parent"]},
        )
        await service.run(caller, batch["operation_id"])
        await service.confirm(caller, batch["operation_id"])
        platform.fail_enable = True
        await service.run(caller, batch["operation_id"])
        assert (await service.get(caller, batch["operation_id"]))[
            "status"
        ] == "failed"
        assert all(
            r[0] == "migrating"
            for r in db.conn.execute(
                "SELECT management_status FROM swe_cron_job_states",
            )
        )
        count = platform.calls.count("register")
        platform.fail_enable = False
        await service.confirm(caller, batch["operation_id"])
        await service.run(caller, batch["operation_id"])
        assert (await service.get(caller, batch["operation_id"]))[
            "status"
        ] == "completed"
        assert (
            platform.calls.count("register") == count + 1
        )  # only the second member, never the first again
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_binding_conflict_does_not_take_over_existing_job():
    db, _, caller, service = setup()
    try:
        db.add(
            "parent",
            owner="legacy",
            task_chat_id="wrong",
            task_session_id="wrong",
        )
        batch = await service.submit(
            caller,
            {"request_id": "one", "job_ids": ["parent"]},
        )
        await service.run(caller, batch["operation_id"])
        await service.confirm(caller, batch["operation_id"])
        await service.run(caller, batch["operation_id"])
        result = await service.get(caller, batch["operation_id"])
        assert result["status"] == "failed"
        assert (
            db.conn.execute(
                "SELECT management_owner FROM swe_cron_jobs_v2",
            ).fetchone()[0]
            == "legacy"
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_migration_callback_validates_new_binding_and_waits_for_group_ready():
    from scheduler.app.services.cron.managed.callback_contract import (
        binding_context,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )

    db, platform, caller, service = setup()
    try:
        batch = await service.submit(
            caller,
            {"request_id": "callback", "job_ids": ["parent"]},
        )
        await service.run(caller, batch["operation_id"])
        await service.confirm(caller, batch["operation_id"])
        platform.fail_enable = True
        await service.run(caller, batch["operation_id"])
        operation = await db.fetch_one(
            "SELECT * FROM swe_cron_registration_journal WHERE job_id='parent' AND action='migrate'",
        )
        # The first member is bound but the group is not published.
        row = await ManagedCronRepository(db)._get_row(
            caller.job_key("parent"),
            True,
        )
        body = {
            "operation_id": operation["operation_id"],
            "callback_protocol": "scheduler_managed_v1",
        }
        assert (await binding_context(db, row, body, "ordinary"))[
            "skip_reason"
        ] == "job_not_ready"
        platform.fail_enable = False
        await service.confirm(caller, batch["operation_id"])
        await service.run(caller, batch["operation_id"])
        row = await ManagedCronRepository(db)._get_row(
            caller.job_key("parent"),
            True,
        )
        assert (await binding_context(db, row, body, "ordinary"))[
            "skip_reason"
        ] is None
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_preview_failure_in_one_group_does_not_block_another_group():
    db, _, caller, service = setup()
    try:
        batch = await service.submit(
            caller,
            {"request_id": "partial", "job_ids": ["missing", "parent"]},
        )
        await service.run(caller, batch["operation_id"])
        assert (await service.get(caller, batch["operation_id"]))[
            "status"
        ] == "awaiting_confirmation"
        await service.confirm(caller, batch["operation_id"])
        await service.run(caller, batch["operation_id"])
        result = await service.get(caller, batch["operation_id"])
        assert {
            group["job_id"]: group["status"] for group in result["groups"]
        } == {"missing": "failed", "parent": "completed"}
    finally:
        db.conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["batch_enabled", "batch_paused"])
async def test_batch_registers_only_parent_early_timer_and_keeps_business_cron(
    mode,
):
    import json

    db, platform, caller, service = setup()
    try:
        for item in service.source.groups["parent"]:
            item["state"]["dispatch_state"] = mode
        service.source.groups["parent"][0]["definition"]["spec_json"]["meta"][
            "batch_dispatch_offset_minutes"
        ] = 60
        batch = await service.submit(
            caller,
            {"request_id": mode, "job_ids": ["parent"]},
        )
        await service.run(caller, batch["operation_id"])
        await service.confirm(caller, batch["operation_id"])
        await service.run(caller, batch["operation_id"])
        assert (await service.get(caller, batch["operation_id"]))[
            "status"
        ] == "completed"
        assert platform.calls.count("register") == 1
        row = await db.fetch_one(
            "SELECT * FROM swe_cron_registration_journal WHERE action='migrate' AND job_id='parent'",
        )
        progress = json.loads(row["progress_json"])
        assert progress["prepared"]["spec"]["schedule"]["cron"] == "0 8 * * *"
        assert progress["batch_advance_minutes"] == 60
        assert (
            db.conn.execute(
                "SELECT cron_expr FROM swe_cron_jobs_v2 WHERE job_id='parent'",
            ).fetchone()[0]
            == "0 9 * * *"
        )
        assert (
            db.conn.execute(
                "SELECT batch_external_job_id FROM swe_cron_job_states WHERE job_id='child'",
            ).fetchone()[0]
            is None
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_http_confirmation_ownership_tombstone_and_scope_protection():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.routers import selected_cron_migration as api

    db, _, caller, service = setup()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.migration_service] = lambda: service
    app.dependency_overrides[api.management_database] = lambda: db
    headers = {
        "X-User-Id": "alice",
        "X-Source-Id": "bank",
        "X-Agent-Id": "default",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            response = await client.post(
                "/api/scheduler/cron/migrations",
                json={"job_ids": ["parent"], "request_id": "http"},
            )
            assert response.status_code == 202
            operation_id = response.json()["operation_id"]
            path = "/api/scheduler/cron/migrations/" + operation_id
            preview = (await client.get(path)).json()
            assert preview["status"] == "awaiting_confirmation"
            assert "snapshot" not in preview["groups"][0]
            assert (await client.post(path + "/confirm")).status_code == 202
            fact = (
                await client.get("/api/scheduler/cron/migrations/ownership")
            ).json()["items"]
            assert (
                len(fact) == 1
                and fact[0]["job_id"] == "parent"
                and fact[0]["completed"]
            )
            db.conn.execute(
                "UPDATE swe_cron_jobs_v2 SET deleted_at='2026-11-01' WHERE job_id='parent'",
            )
            assert (
                await client.get("/api/scheduler/cron/migrations/ownership")
            ).json()["items"] == fact
            assert (
                await client.get(path, headers={"X-Source-Id": "other"})
            ).status_code == 404
            assert (
                await client.get(
                    "/api/scheduler/cron/migrations/ownership",
                    headers={"X-User-Id": "ALICE"},
                )
            ).json()["items"] == []
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_legacy_snapshot_is_claimed_once_and_repeated_migration_skips_it():
    db, platform, caller, service = setup()
    try:
        db.add(
            "parent",
            owner="legacy",
            task_chat_id="chat-parent",
            task_session_id="session-parent",
        )
        for request in ("first", "second"):
            batch = await service.submit(
                caller,
                {"request_id": request, "job_ids": ["parent"]},
            )
            await service.run(caller, batch["operation_id"])
            await service.confirm(caller, batch["operation_id"])
            await service.run(caller, batch["operation_id"])
            assert (await service.get(caller, batch["operation_id"]))[
                "status"
            ] == "completed"
        assert platform.calls.count("register") == 2
        assert (
            db.conn.execute(
                "SELECT management_owner FROM swe_cron_jobs_v2 WHERE job_id='parent'",
            ).fetchone()[0]
            == "scheduler"
        )
        assert (
            db.conn.execute(
                "SELECT count(*) FROM swe_cron_registration_journal WHERE action='migrate'",
            ).fetchone()[0]
            == 2
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_member_cannot_bypass_group_publication_through_crud_retry():
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
        ManagementConflict,
    )
    from tests.unit.scheduler.test_managed_cron_management import (
        ResourceClient,
    )

    db, platform, caller, service = setup()
    try:
        batch = await service.submit(
            caller,
            {"request_id": "guard", "job_ids": ["parent"]},
        )
        await service.run(caller, batch["operation_id"])
        await service.confirm(caller, batch["operation_id"])
        platform.fail_enable = True
        await service.run(caller, batch["operation_id"])
        row = await db.fetch_one(
            "SELECT * FROM swe_cron_registration_journal WHERE action='migrate' AND job_id='parent'",
        )
        resources = ResourceClient()
        with pytest.raises(
            ManagementConflict,
            match="cron_migration_requires_group_retry",
        ):
            await ManagementService(db, resources, platform).retry(
                caller,
                "parent",
                row["operation_id"],
            )
        assert resources.prepared == []
        assert (
            db.conn.execute(
                "SELECT management_status FROM swe_cron_job_states WHERE job_id='parent'",
            ).fetchone()[0]
            == "migrating"
        )
    finally:
        db.conn.close()
