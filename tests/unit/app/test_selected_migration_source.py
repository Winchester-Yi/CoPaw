# -*- coding: utf-8 -*-
"""Selected snapshots read existing files; they never bootstrap a workspace."""

import json
import pytest

from tests.unit.app.test_cron_migration_source import job, write_jobs


def source_files(root):
    value = job(
        meta={
            "creator_user_id": "u",
            "task_chat_id": "chat-old",
            "task_session_id": "session-old",
            "task_unread_execution_count": 7,
        },
    )
    value["dispatch"]["target"]["session_id"] = "session-old"
    path = write_jobs(root, [value])
    chat = {
        "id": "chat-old",
        "session_id": "session-old",
        "user_id": "u",
        "meta": {
            "session_kind": "task",
            "task_job_id": "job-1",
            "task_tenant_id": "tenant-a",
            "task_source_id": "source-a",
        },
    }
    path.with_name("chats.json").write_text(json.dumps({"chats": [chat]}))
    return path


def test_reads_only_selected_scope_and_keeps_bytes_and_original_binding(
    tmp_path,
):
    from swe.app.crons.selected_migration_source import read_member

    path = source_files(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")}
    result = read_member(
        tmp_path,
        ("tenant-a", "source-a", "default", "job-1"),
        {},
    )
    assert result["state"]["task_chat_id"] == "chat-old"
    assert result["state"]["task_session_id"] == "session-old"
    assert result["state"]["unread_execution_count"] == 7
    assert (
        result["definition"]["spec_json"]["schedule"]["run_on_non_workday"]
        is False
    )
    assert path.exists()
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")}


def test_missing_or_conflicting_chat_does_not_create_replacement(tmp_path):
    from swe.app.crons.selected_migration_source import read_member

    path = source_files(tmp_path)
    path.with_name("chats.json").write_text('{"chats": []}')
    with pytest.raises(ValueError, match="cron_migration_chat_missing"):
        read_member(tmp_path, ("tenant-a", "source-a", "default", "job-1"), {})
    assert json.loads(path.with_name("chats.json").read_text()) == {
        "chats": [],
    }


def test_conflicting_chat_warns_and_keeps_jobs_file_binding(tmp_path):
    from swe.app.crons.selected_migration_source import read_member

    path = source_files(tmp_path)
    chats = path.with_name("chats.json")
    data = json.loads(chats.read_text())
    data["chats"][0]["session_id"] = "stale-session"
    chats.write_text(json.dumps(data))
    result = read_member(
        tmp_path,
        ("tenant-a", "source-a", "default", "job-1"),
        {},
    )
    assert result["state"]["task_chat_id"] == "chat-old"
    assert result["state"]["task_session_id"] == "session-old"
    assert (
        "cron_migration_binding_conflict:job-1:chats.json:session_id"
        in result["warnings"]
    )
    assert json.loads(chats.read_text()) == data


@pytest.mark.asyncio
async def test_migration_stops_legacy_file_and_skips_workspace_restore(
    tmp_path,
):
    from unittest.mock import AsyncMock
    from swe.app.crons.manager import CronManager
    from swe.app.crons.repo.json_repo import JsonJobRepository
    from swe.app.crons.selected_migration_source import stop_legacy_member

    path = source_files(tmp_path)
    original_chat = path.with_name("chats.json").read_bytes()
    snapshot = {
        "spec": {"enabled": True, "meta": {}},
        "migration": {"chat_id": "chat-old", "session_id": "session-old"},
        "legacy_runtime": {},
    }
    key = ("tenant-a", "source-a", "default", "job-1")
    await stop_legacy_member(tmp_path, key, "migration-1", snapshot)
    await stop_legacy_member(tmp_path, key, "migration-1", snapshot)
    repo = JsonJobRepository(path)
    job = await repo.get_job("job-1")
    assert job.enabled is False
    assert job.meta["task_migration_stop"] == "migration-1"
    assert job.meta["task_chat_id"] == "chat-old"
    assert path.with_name("chats.json").read_bytes() == original_chat
    adapter = AsyncMock()
    manager = CronManager(
        repo=repo,
        runner=None,
        channel_manager=None,
        scheduler_adapter=adapter,
    )
    adapter.reset_mock()
    await manager._restore_external_job_id(job)
    assert adapter.mock_calls == []


def test_running_snapshot_does_not_become_new_inflight_execution(tmp_path):
    from swe.app.crons.selected_migration_source import read_member
    from swe.config.context import encode_scope_id

    source_files(tmp_path)
    loaded = {
        f"{encode_scope_id('tenant-a', 'source-a')}:default": {
            "job-1": {"last_status": "running"},
        },
    }
    result = read_member(
        tmp_path,
        ("tenant-a", "source-a", "default", "job-1"),
        loaded,
    )
    assert result["legacy_runtime"]["last_status"] == "running"
    assert result["state"]["last_status"] == "idle"


def test_identity_conflict_never_reads_another_tenant(tmp_path):
    from swe.app.crons.selected_migration_source import read_member

    source_files(tmp_path)
    with pytest.raises(ValueError, match="cron_migration_job_missing"):
        read_member(tmp_path, ("other", "source-a", "default", "job-1"), {})


@pytest.mark.asyncio
async def test_group_snapshot_authorizes_parent_and_keeps_inflight_out_of_read_watermark(
    tmp_path,
    monkeypatch,
):
    from swe.app.crons import selected_migration_api as api
    from tests.unit.scheduler.test_managed_cron_executions import (
        ExecutionDatabase,
    )
    from fastapi import HTTPException

    path = source_files(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(api.config_utils, "WORKING_DIR", str(tmp_path))
    db = ExecutionDatabase()
    try:
        db.conn.execute(
            "INSERT INTO swe_cron_jobs(id,tenant_id,source_id,creator_user_id,task_chat_id,task_session_id,meta) VALUES ('job-1','tenant-a','source-a','u','chat-old','session-old','{}')",
        )
        for status in ("success", "running"):
            db.conn.execute(
                "INSERT INTO swe_cron_executions(job_id,tenant_id,is_read,status) VALUES ('job-1','tenant-a',0,?)",
                (status,),
            )
        key = ("tenant-a", "source-a", "default", "job-1")
        with pytest.raises(HTTPException) as error:
            await api.snapshot_group(db, key, "intruder", False, {})
        assert error.value.status_code == 404
        db.conn.execute(
            "UPDATE swe_cron_executions SET status='error',error_message='子任务执行失败',actual_time='2026-11-05 09:00:00',output_preview='旧结果' WHERE id=1",
        )
        db.conn.execute(
            "UPDATE swe_cron_executions SET status='success',async_status='pending' WHERE id=2",
        )
        result = await api.snapshot_group(db, key, "u", False, {})
        assert len(result["members"]) == 1
        assert result["members"][0]["history_read_ids"] == [1]
        state = result["members"][0]["state"]
        assert state["last_error"] == "子任务执行失败"
        assert state["last_run_at"].isoformat() == "2026-11-05T01:00:00"
        assert path.read_bytes() == before
        assert (
            db.conn.execute(
                "SELECT count(*) FROM swe_cron_jobs_v2",
            ).fetchone()[0]
            == 0
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_group_discovers_child_from_old_table_and_rechecks_actual_parent(
    tmp_path,
    monkeypatch,
):
    from swe.app.crons import selected_migration_api as api
    from tests.unit.scheduler.test_managed_cron_executions import (
        ExecutionDatabase,
    )
    from swe.config.context import encode_scope_id

    source_files(tmp_path)
    meta = {
        "creator_user_id": "bob",
        "task_chat_id": "child-chat",
        "task_session_id": "child-session",
        "broadcast_source_job_id": "job-1",
        "broadcast_source_tenant_id": "tenant-a",
    }
    child = job(id="child", tenant_id="bob", meta=meta)
    path = write_jobs(
        tmp_path,
        [child],
        storage=encode_scope_id("bob", "source-a"),
    )
    path.with_name("chats.json").write_text(
        json.dumps(
            {
                "chats": [
                    {
                        "id": "child-chat",
                        "session_id": "child-session",
                        "user_id": "bob",
                        "meta": {
                            "session_kind": "task",
                            "task_job_id": "child",
                            "task_tenant_id": "bob",
                            "task_source_id": "source-a",
                        },
                    },
                ],
            },
        ),
    )
    monkeypatch.setattr(api.config_utils, "WORKING_DIR", str(tmp_path))
    db = ExecutionDatabase()
    try:
        db.conn.execute(
            "INSERT INTO swe_cron_jobs(id,tenant_id,source_id,creator_user_id,task_chat_id,task_session_id,meta) VALUES ('job-1','tenant-a','source-a','u','chat-old','session-old','{}')",
        )
        db.conn.execute(
            "INSERT INTO swe_cron_jobs(id,tenant_id,source_id,creator_user_id,task_chat_id,task_session_id,meta) VALUES ('child','bob','source-a','bob','child-chat','child-session',?)",
            (json.dumps(meta),),
        )
        result = await api.snapshot_group(
            db,
            ("tenant-a", "source-a", "default", "job-1"),
            "u",
            False,
            {},
        )
        assert len(result["members"]) == 2
        assert (
            result["members"][1]["definition"]["parent_agent_id"] == "default"
        )
        raw = json.loads(path.read_text())
        raw["jobs"][0]["meta"]["broadcast_source_tenant_id"] = "someone-else"
        path.write_text(json.dumps(raw))
        db.conn.execute(
            "UPDATE swe_cron_jobs SET task_chat_id='' WHERE id='child'",
        )
        corrected = await api.snapshot_group(
            db,
            ("tenant-a", "source-a", "default", "job-1"),
            "u",
            False,
            {},
        )
        actual = corrected["members"][1]
        assert actual["definition"]["parent_tenant_id"] == "tenant-a"
        assert (
            actual["definition"]["spec_json"]["meta"][
                "broadcast_source_tenant_id"
            ]
            == "tenant-a"
        )
        assert actual["state"]["task_chat_id"] == "child-chat"
        assert (
            "cron_migration_parent_conflict:child:jobs.json:broadcast_source_tenant_id"
            in actual["warnings"]
        )
        assert (
            "cron_migration_binding_conflict:child:swe_cron_jobs:task_chat_id"
            in actual["warnings"]
        )
    finally:
        db.conn.close()
