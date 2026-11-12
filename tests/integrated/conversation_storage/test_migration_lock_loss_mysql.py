# -*- coding: utf-8 -*-
"""Kill only this test's pinned migration connection during real local IO."""

import asyncio
import json
import threading
from uuid import uuid4

import pytest
from agentscope.message import Msg

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.importer import (
    ParallelWorkspaceImporter,
)
from swe.app.conversation_storage.migration.lock_guard import (
    MigrationLockGuard,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.cli.conversation_storage_v3_cmd import run_v3_command
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


def _source(tmp_path):
    source, nas = tmp_path / "source", tmp_path / "nas"
    profiles = {}
    for index in range(2):
        agent, session = "agent" + str(index), "session" + str(index)
        workspace = source / "default_SRC/workspaces" / agent
        (workspace / "sessions").mkdir(parents=True)
        runtime = nas / "default_SRC/workspaces" / agent
        runtime.mkdir(parents=True)
        profiles[agent] = {
            "id": agent,
            "workspace_dir": str(runtime),
            "enabled": True,
        }
        (workspace / "agent.json").write_text(json.dumps({"id": agent}))
        (workspace / "chats.json").write_text(
            json.dumps(
                {
                    "chats": [
                        {
                            "id": str(uuid4()),
                            "session_id": session,
                            "user_id": "alice",
                            "channel": "console",
                            "created_at": "2026-11-01T00:00:00Z",
                            "updated_at": "2026-11-01T00:00:00Z",
                        },
                    ],
                },
            ),
        )
        (workspace / "sessions" / ("alice_" + session + ".json")).write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "revision": 1,
                    "agent": {
                        "memory": {
                            "content": [
                                Msg("user", "question", "user").to_dict(),
                            ],
                        },
                    },
                },
            ),
        )
    (source / "default_SRC/config.json").write_text(
        json.dumps({"agents": {"profiles": profiles}}),
    )
    for path in source.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    return source, nas


async def _wait_event(event):
    assert await asyncio.to_thread(
        event.wait,
        30,
    ), "migration fault hook not reached"


def _tickets(rows):
    return {
        (row["entry_kind"], row["source_key"]): (
            row["target_id"],
            row["operation_key"],
        )
        for row in rows
    }


async def _resume_and_verify(db, manifest, work, tmp_path):
    before = _tickets(
        await db.fetch_all(
            "SELECT entry_kind,source_key,target_id,operation_key FROM swe_conversation_migration_entries WHERE target_id IS NOT NULL",
        ),
    )
    assert before
    await asyncio.to_thread(
        run_v3_command,
        "resume",
        manifest.path,
        apply=True,
        workers=1,
        work_dir=work,
    )
    after = _tickets(
        await db.fetch_all(
            "SELECT entry_kind,source_key,target_id,operation_key FROM swe_conversation_migration_entries WHERE target_id IS NOT NULL",
        ),
    )
    assert all(after[key] == value for key, value in before.items())
    assert (
        await asyncio.to_thread(
            run_v3_command,
            "verify",
            manifest.path,
            work_dir=work,
        )
    )["passed"]
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    result = await asyncio.to_thread(
        run_v3_command,
        "finalize",
        manifest.path,
        apply=True,
        work_dir=work,
        output_dir=tmp_path / "completion",
    )
    assert result["passed"] and result["authority"]
    job = await db.fetch_one(
        "SELECT state FROM swe_conversation_migration_jobs WHERE migration_id=%s",
        (manifest.migration_id,),
    )
    assert job["state"] == "ready"


def _install_faults(fault):
    captured, armed, started, release, finished, health_failed = (
        threading.Event() for _ in range(6)
    )
    identity = {}
    capture, check = MigrationLockGuard.capture, MigrationLockGuard.check
    publish, run_workspace = (
        BundleStore.publish,
        ParallelWorkspaceImporter.run_workspace,
    )
    blocker_lock = threading.Lock()

    async def capture_owner(database, lock_name, **kwargs):
        guard = await capture(database, lock_name, **kwargs)
        identity.update(connection_id=guard.connection_id, lock_name=lock_name)
        captured.set()
        return guard

    async def check_owner(guard):
        try:
            await check(guard)
        except Exception:
            health_failed.set()
            raise

    async def arm_workspace(importer, workspace):
        identity["workspaces_started"] = (
            identity.get("workspaces_started", 0) + 1
        )
        armed.set()
        return await run_workspace(importer, workspace)

    def delayed_publish(store, *args, **kwargs):
        if armed.is_set():
            with blocker_lock:
                block = not started.is_set()
                if block:
                    started.set()
            if block:
                try:
                    assert release.wait(30), "test did not release NAS IO"
                    return publish(store, *args, **kwargs)
                finally:
                    finished.set()
        return publish(store, *args, **kwargs)

    fault.setattr(MigrationLockGuard, "capture", capture_owner)
    fault.setattr(MigrationLockGuard, "check", check_owner)
    fault.setattr(
        ParallelWorkspaceImporter,
        "run_workspace",
        arm_workspace,
    )
    fault.setattr(BundleStore, "publish", delayed_publish)
    fault.setattr(
        "swe.app.conversation_storage.migration.parallel.HEALTH_CHECK_INTERVAL",
        0.01,
    )
    return captured, started, release, finished, health_failed, identity


@pytest.mark.asyncio
async def test_killed_owner_connection_drains_io_then_resumes_same_tickets(
    conversation_test_database,
    tmp_path,
    monkeypatch,
    record_property,
):
    db = conversation_test_database
    record_property("owned_schema", db.config.database)
    await ChatShareStore(db).ensure_schema()
    for name in (
        "conversation_share_publication_extension.sql",
        "conversation_migration_control_tables.sql",
    ):
        await apply_sql_script(db, SQL_ROOT / name)
    source, nas = _source(tmp_path)
    report = discover_services(
        [
            {
                "service_unit_id": "KILL-TEST",
                "source_root": str(source),
                "config_root": str(source),
                "nas_root": str(nas),
                "original_root": str(nas),
            },
        ],
    )
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        SourceBoundary(
            "snapshot",
            "2026-11-02T00:00:00Z",
            "old@sha256:" + "a" * 64,
            ("stopped", "readonly"),
        ),
        db.config.database,
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    task = None
    release = threading.Event()
    try:
        with monkeypatch.context() as fault:
            captured, started, release, finished, health_failed, identity = (
                _install_faults(fault)
            )
            task = asyncio.create_task(
                asyncio.to_thread(
                    run_v3_command,
                    "import",
                    manifest.path,
                    apply=True,
                    workers=1,
                    io_workers=1,
                    work_dir=work,
                ),
            )
            await _wait_event(captured)
            await _wait_event(started)
            row = await db.fetch_one(
                "SELECT ID,DB FROM information_schema.processlist WHERE ID=%s",
                (identity["connection_id"],),
            )
            assert row is not None and row["DB"] == db.config.database
            lock = await db.fetch_one(
                "SELECT IS_USED_LOCK(%s) AS owner",
                (identity["lock_name"],),
            )
            assert lock["owner"] == identity["connection_id"]
            await db.execute(
                "KILL CONNECTION " + str(int(identity["connection_id"])),
            )
            await _wait_event(health_failed)
            assert not task.done() and not finished.is_set()
            release.set()
            with pytest.raises(Exception) as failure:
                await asyncio.wait_for(task, 30)
            assert not isinstance(failure.value, TimeoutError)
            assert finished.is_set() and identity["workspaces_started"] == 1
        await _resume_and_verify(db, manifest, work, tmp_path)
    finally:
        release.set()
        if task is not None and not task.done():
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True),
                35,
            )
        for path in source.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        source.chmod(0o755)
