# -*- coding: utf-8 -*-
"""系统任务按稳定身份受理刷新，同身份同步串行且旧水位不能覆盖新水位。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.system.identity import SystemIdentity
from scheduler.app.services.cron.system.management_store import (
    SystemManagementStore,
)
from tests.fakes.system_cron_database import SystemDatabase

ADMIN = CallerIdentity("alice", "bank", "default", "alice", is_manager=True)
USER = CallerIdentity("alice", "bank", "default", "alice")
CONFIG = {
    "enabled": True,
    "cron": "0 9 * * *",
    "timezone": "Asia/Shanghai",
    "timeout_seconds": 300,
}


@pytest.fixture
def system_setup():
    db = SystemDatabase()
    now = [datetime(2026, 10, 20, tzinfo=timezone.utc)]
    yield db, SystemManagementStore(db, clock=lambda: now[0]), now
    db.conn.close()


def test_system_identity_source_normalizes_admin_and_agent_dimensions():
    first = SystemIdentity.from_caller(ADMIN, "cleanup")
    other = CallerIdentity("bob", "bank", "research", "bob", is_manager=True)
    second = SystemIdentity.from_caller(other, "task_session_cleanup")
    assert first == second
    assert (
        first.scope_kind == "source"
        and first.tenant_id == first.agent_id == ""
    )
    assert first.job_id == second.job_id
    assert (
        first.job_id
        != SystemIdentity.from_caller(ADMIN, "archive_maintenance").job_id
    )
    assert (
        SystemIdentity.from_caller(USER, "heartbeat").job_id
        != SystemIdentity.from_caller(other, "heartbeat").job_id
    )


def test_source_system_refresh_requires_manager_and_cannot_forge_agent_owner():
    with pytest.raises(PermissionError):
        SystemIdentity.from_caller(USER, "archive_maintenance")
    with pytest.raises(PermissionError):
        SystemIdentity.from_caller(
            CallerIdentity("bob", "bank", "default", "alice"),
            "dream",
        )
    with pytest.raises(ValueError):
        SystemIdentity.from_caller(ADMIN, "arbitrary_command")


@pytest.mark.asyncio
async def test_refresh_is_stable_idempotent_and_never_reads_legacy_bindings(
    system_setup,
):
    db, store, _ = system_setup
    first = await store.refresh(ADMIN, "task_session_cleanup", "save-a")
    duplicate = await store.refresh(ADMIN, "cleanup", "save-a")
    assert first["job_id"] == duplicate["job_id"]
    assert first["generation"] == duplicate["generation"] == 1
    assert first["sync_status"] == "pending" and not first["enabled"]
    other = CallerIdentity("bob", "bank", "research", "bob", is_manager=True)
    changed = await store.refresh(other, "cleanup", "save-b")
    assert changed["generation"] == 2 and changed["job_id"] == first["job_id"]
    assert changed["route_tenant_id"] == "bob"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_system_jobs",
        ).fetchone()[0]
        == 1
    )
    assert all(
        "swe_source_system_task_binding" not in query
        and "swe_cron_jobs_v2" not in query
        for query in db.statements
    )


@pytest.mark.asyncio
async def test_concurrent_refreshes_keep_one_logical_source_task(system_setup):
    db, store, _ = system_setup
    first, second = await asyncio.gather(
        store.refresh(ADMIN, "cleanup", "save-a"),
        store.refresh(ADMIN, "cleanup", "save-b"),
    )
    assert first["job_id"] == second["job_id"]
    assert (
        db.conn.execute(
            "SELECT generation FROM swe_cron_system_jobs",
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
async def test_sync_lease_serializes_and_retains_newer_refresh_watermark(
    system_setup,
):
    db, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save-a")
    claimed = await store.claim(row["job_id"], "worker-a")
    assert await store.claim(row["job_id"], "worker-b") is None
    assert await store.prepare(claimed, CONFIG)
    await store.refresh(ADMIN, "heartbeat", "save-b")
    assert await store.finish(claimed, "123", CONFIG)
    current = await store.get(row["job_id"])
    assert current["generation"] == 2 and current["synced_generation"] == 1
    assert (
        current["sync_status"] == "pending"
        and current["external_job_id"] == "123"
    )
    newer = await store.claim(row["job_id"], "worker-b")
    assert newer["sync_generation"] == 2 and newer["external_job_id"] == "123"
    updated = {**CONFIG, "cron": "0 10 * * *"}
    assert await store.prepare(newer, updated)
    assert await store.finish(newer, "123", updated)
    assert not await store.finish(claimed, "old-id", CONFIG)
    final = await store.get(row["job_id"])
    assert final["sync_status"] == "synced" and final["synced_generation"] == 2
    assert json.loads(final["effective_config_json"])["cron"] == "0 10 * * *"


@pytest.mark.asyncio
async def test_late_config_fetch_releases_lease_without_using_stale_snapshot(
    system_setup,
):
    _, store, _ = system_setup
    row = await store.refresh(ADMIN, "dream", "save-a")
    claimed = await store.claim(row["job_id"], "worker-a")
    await store.refresh(ADMIN, "dream", "save-b")
    assert not await store.prepare(claimed, CONFIG)
    current = await store.get(row["job_id"])
    assert current["sync_status"] == "pending" and not current["sync_owner"]
    assert current["external_job_id"] is None


@pytest.mark.asyncio
async def test_expired_registration_without_id_requires_manual_retry(
    system_setup,
):
    _, store, now = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save-a")
    claimed = await store.claim(row["job_id"], "worker-a")
    assert await store.prepare(claimed, CONFIG)
    assert await store.stage(claimed, "registering")
    now[0] += timedelta(seconds=90)
    assert await store.claim(row["job_id"], "worker-b") is None
    current = await store.get(row["job_id"])
    assert current["sync_status"] == "failed"
    assert current["sync_error_code"] == "system_platform_registration_unknown"
    assert not await store.finish(claimed, "late-id", CONFIG)


@pytest.mark.asyncio
async def test_failed_known_binding_keeps_effective_state_and_retry_is_versioned(
    system_setup,
):
    _, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save-a")
    first = await store.claim(row["job_id"], "worker-a")
    assert await store.prepare(first, CONFIG)
    assert await store.finish(first, "123", CONFIG)
    await store.refresh(ADMIN, "heartbeat", "save-b")
    second = await store.claim(row["job_id"], "worker-b")
    disabled = {**CONFIG, "enabled": False}
    assert await store.prepare(second, disabled)
    assert await store.fail(second, "system_platform_update_failed")
    failed = await store.get(row["job_id"])
    assert failed["external_job_id"] == "123" and not failed["enabled"]
    assert json.loads(failed["effective_config_json"])["enabled"]
    with pytest.raises(ValueError, match="system_version_conflict"):
        await store.retry(
            ADMIN,
            row["job_id"],
            failed["version"] - 1,
            "retry-a",
        )
    pending = await store.retry(
        ADMIN,
        row["job_id"],
        failed["version"],
        "retry-a",
    )
    duplicate = await store.retry(
        ADMIN,
        row["job_id"],
        failed["version"],
        "retry-a",
    )
    assert (
        pending["version"] == duplicate["version"]
        and pending["sync_status"] == "pending"
    )
    assert pending["generation"] == failed["generation"]
    assert pending["external_job_id"] == "123"


@pytest.mark.asyncio
async def test_retry_cannot_cross_source_or_interrupt_active_sync(
    system_setup,
):
    _, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save-a")
    foreign = CallerIdentity(
        "alice",
        "other",
        "default",
        "alice",
        is_manager=True,
    )
    with pytest.raises(LookupError):
        await store.retry(foreign, row["job_id"], row["version"], "retry")
    claimed = await store.claim(row["job_id"], "worker")
    with pytest.raises(ValueError, match="system_sync_busy"):
        await store.retry(ADMIN, row["job_id"], claimed["version"], "retry")


@pytest.mark.asyncio
async def test_successful_retry_preserves_most_recent_possible_platform_reference(
    system_setup,
):
    """更新响应丢失后，下一次成功不能丢掉最近可能已生效的旧操作引用。"""
    _, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save-a")
    first = await store.claim(row["job_id"], "one")
    await store.prepare(first, CONFIG)
    await store.finish(first, "123", CONFIG)
    await store.refresh(ADMIN, "heartbeat", "save-b")
    second = await store.claim(row["job_id"], "two")
    await store.prepare(second, {**CONFIG, "cron": "0 10 * * *"})
    await store.stage(second, "updating")
    await store.fail(second, "system_platform_update_failed")
    failed = await store.get(row["job_id"])
    await store.retry(ADMIN, row["job_id"], failed["version"], "retry")
    third = await store.claim(row["job_id"], "three")
    await store.prepare(third, CONFIG)
    await store.finish(third, "123", CONFIG)
    final = await store.get(row["job_id"])
    assert final["previous_operation_id"] == second["operation_id"]
    assert (
        json.loads(final["previous_config_json"])["_generation"]
        == second["sync_generation"]
    )
