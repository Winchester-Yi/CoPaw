# -*- coding: utf-8 -*-
"""真实事务验证迁入豁免不会变成客户端可用的旧表覆盖开关。"""

import json
import pytest

from tests.unit.scheduler.test_managed_cron_executions import ExecutionDatabase
from tests.unit.scheduler.test_managed_cron_management import (
    ResourceClient,
    PlatformClient,
    create_payload,
)
from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.management import ManagementService
from scheduler.app.services.cron.managed.controls import ControlService


async def fixture(imported=True):
    db = ExecutionDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    resources, platform = ResourceClient(), PlatformClient()
    service = ManagementService(db, resources, platform)
    created = await service.create(caller, create_payload())
    job = created["job_id"]
    state = dict(
        db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone(),
    )
    if imported:
        key = list(caller.job_key(job))
        payload = {
            "migration": {
                "version": 1,
                "imported": True,
                "identity": key,
                "chat_id": state["task_chat_id"],
                "session_id": state["task_session_id"],
                "history_max_id": 1,
                "history_read_ids": [1],
            },
        }
        db.conn.execute(
            "INSERT INTO swe_cron_registration_journal(operation_id,tenant_id,source_id,agent_id,job_id,actor_id,action,status,payload_json,progress_json) VALUES (?,?,?,?,?,?,'migrate','completed',?,'{}')",
            ("migration-fact", *key, "alice", json.dumps(payload)),
        )
        db.conn.execute("UPDATE swe_cron_jobs SET meta='{}'")
    db.conn.execute("UPDATE swe_cron_job_states SET unread_execution_count=7")
    return db, caller, resources, platform, service, job


@pytest.mark.asyncio
async def test_migrated_control_does_not_write_legacy_row():
    db, caller, _, platform, _, job = await fixture()
    try:
        before = dict(
            db.conn.execute("SELECT * FROM swe_cron_jobs").fetchone(),
        )
        version = db.conn.execute(
            "SELECT management_version FROM swe_cron_job_states",
        ).fetchone()[0]
        result = await ControlService(db, platform).set_enabled(
            caller,
            job,
            {
                "request_id": "pause",
                "enabled": False,
                "expected_management_version": version,
            },
        )
        assert result["status"] == "completed"
        assert not db.conn.execute(
            "SELECT enabled FROM swe_cron_jobs_v2",
        ).fetchone()[0]
        assert (
            dict(db.conn.execute("SELECT * FROM swe_cron_jobs").fetchone())
            == before
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_mark_read_includes_old_watermark_but_not_later_legacy_result():
    db, caller, _, _, _, job = await fixture()
    try:
        for _ in range(2):
            db.conn.execute(
                "INSERT INTO swe_cron_executions(job_id,tenant_id,is_read,status,meta) VALUES (?, 'alice',0,'success','{}')",
                (job,),
            )
        await ControlService(db).mark_read(caller, job, {"request_id": "read"})
        assert [
            r[0]
            for r in db.conn.execute(
                "SELECT is_read FROM swe_cron_executions ORDER BY id",
            )
        ] == [1, 0]
        assert (
            db.conn.execute(
                "SELECT unread_execution_count FROM swe_cron_job_states",
            ).fetchone()[0]
            == 0
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_migrated_delete_preserves_original_resources_and_old_definition():
    db, caller, resources, platform, service, job = await fixture()
    try:
        before = dict(
            db.conn.execute("SELECT * FROM swe_cron_jobs").fetchone(),
        )
        result = await service.delete(
            caller,
            job,
            {"request_id": "delete", "expected_definition_version": 1},
        )
        assert result["status"] == "completed"
        assert resources.cleaned == []
        assert "stop" in platform.calls
        assert db.conn.execute(
            "SELECT deleted_at FROM swe_cron_jobs_v2",
        ).fetchone()[0]
        assert (
            dict(db.conn.execute("SELECT * FROM swe_cron_jobs").fetchone())
            == before
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_native_task_keeps_projection_and_normal_cleanup():
    db, caller, resources, _, service, job = await fixture(False)
    try:
        result = await service.delete(
            caller,
            job,
            {"request_id": "delete", "expected_definition_version": 1},
        )
        assert result["status"] == "completed"
        assert resources.cleaned == [job]
        assert (
            db.conn.execute("SELECT status FROM swe_cron_jobs").fetchone()[0]
            == "deleted"
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_untrusted_spec_metadata_cannot_bypass_projection_guard():
    from scheduler.app.services.cron.managed.projection import write_projection

    db, caller, _, _, _, job = await fixture(False)
    try:
        db.conn.execute("UPDATE swe_cron_jobs SET meta='{}'")
        spec = json.loads(
            db.conn.execute(
                "SELECT spec_json FROM swe_cron_jobs_v2",
            ).fetchone()[0],
        )
        spec["meta"]["_cron_migration"] = {"imported": True}
        with pytest.raises(
            ValueError,
            match="legacy_projection_identity_conflict",
        ):
            await write_projection(db, spec, {}, "default", None)
    finally:
        db.conn.close()
