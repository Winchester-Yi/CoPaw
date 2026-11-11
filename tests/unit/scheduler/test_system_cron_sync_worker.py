# -*- coding: utf-8 -*-
"""系统配置后台同步使用现有平台协议，所有HTTP都在任务事务外。"""

import json

import pytest

from scheduler.app.services.cron.system.sync_worker import SystemSyncWorker
from tests.unit.scheduler.test_system_cron_management import (
    ADMIN,
    CONFIG,
    system_setup,
)


class ConfigClient:
    """模拟只读SWE接口，记录是否误带旧请求配置或工作区启动指令。"""

    def __init__(self, db, config=None):
        self.db = db
        self.config = dict(CONFIG if config is None else config)
        self.observed = []
        self.after_fetch = None

    async def get_config(self, row):
        assert not self.db.conn.in_transaction
        self.observed.append(row["job_id"])
        result = dict(self.config)
        if self.after_fetch:
            await self.after_fetch()
        return result


class Platform:
    """只模拟平台边界，仓库、配置水位与租约使用真实SQL。"""

    def __init__(self, db):
        self.db = db
        self.calls = []
        self.fail_enable = False
        self.fail_register = False

    async def register(self, row, config):
        assert not self.db.conn.in_transaction
        self.calls.append(("register", row["job_id"]))
        if self.fail_register:
            raise TimeoutError("registration response lost")
        return "123"

    async def update(self, external_id, row, config):
        assert not self.db.conn.in_transaction
        self.calls.append(("update", external_id))

    async def set_enabled(self, external_id, enabled):
        assert not self.db.conn.in_transaction
        self.calls.append(("enabled", external_id, enabled))
        if self.fail_enable:
            raise TimeoutError("enable response lost")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["heartbeat", "dream", "cleanup", "archive_maintenance"],
)
async def test_all_system_types_sync_without_user_pool_or_legacy_bindings(
    system_setup,
    kind,
):
    db, store, _ = system_setup
    row = await store.refresh(ADMIN, kind, "save")
    client, platform = ConfigClient(db), Platform(db)
    worker = SystemSyncWorker(store, client, platform)
    assert await worker.process(row["job_id"])
    final = await store.get(row["job_id"])
    assert (
        final["sync_status"] == "synced" and final["external_job_id"] == "123"
    )
    assert final["synced_generation"] == 1
    assert platform.calls == [
        ("register", row["job_id"]),
        ("enabled", "123", True),
    ]
    assert all(
        "dispatch_intents" not in sql and "swe_cron_jobs_v2" not in sql
        for sql in db.statements
    )


@pytest.mark.asyncio
async def test_initial_disabled_config_never_registers_external_timer(
    system_setup,
):
    db, store, _ = system_setup
    row = await store.refresh(ADMIN, "dream", "save")
    client, platform = ConfigClient(
        db,
        {**CONFIG, "enabled": False, "cron": ""},
    ), Platform(db)
    assert await SystemSyncWorker(store, client, platform).process(
        row["job_id"],
    )
    final = await store.get(row["job_id"])
    assert (
        final["sync_status"] == "synced" and final["external_job_id"] is None
    )
    assert not final["enabled"] and not platform.calls


@pytest.mark.asyncio
async def test_enable_failure_keeps_known_id_for_explicit_retry(system_setup):
    db, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save")
    client, platform = ConfigClient(db), Platform(db)
    platform.fail_enable = True
    worker = SystemSyncWorker(store, client, platform)
    assert not await worker.process(row["job_id"])
    failed = await store.get(row["job_id"])
    assert (
        failed["sync_status"] == "failed"
        and failed["external_job_id"] == "123"
    )
    assert failed["sync_error_code"] == "system_platform_enable_failed"
    await store.retry(ADMIN, row["job_id"], failed["version"], "retry")
    platform.fail_enable = False
    assert await worker.process(row["job_id"])
    assert sum(call[0] == "register" for call in platform.calls) == 1
    assert ("update", "123") in platform.calls


@pytest.mark.asyncio
async def test_missing_registration_response_fails_without_automatic_re_registration(
    system_setup,
):
    db, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save")
    client, platform = ConfigClient(db), Platform(db)
    platform.fail_register = True
    worker = SystemSyncWorker(store, client, platform)
    assert not await worker.process(row["job_id"])
    assert not await worker.process(row["job_id"])
    failed = await store.get(row["job_id"])
    assert failed["sync_error_code"] == "system_platform_registration_unknown"
    assert failed["external_job_id"] is None
    assert len(platform.calls) == 1


@pytest.mark.asyncio
async def test_newer_save_during_config_read_does_not_sync_old_config(
    system_setup,
):
    db, store, _ = system_setup
    row = await store.refresh(ADMIN, "heartbeat", "save-a")
    client, platform = ConfigClient(db), Platform(db)
    client.after_fetch = lambda: store.refresh(ADMIN, "heartbeat", "save-b")
    worker = SystemSyncWorker(store, client, platform)
    assert not await worker.process(row["job_id"])
    assert not platform.calls
    client.after_fetch = None
    client.config = {
        **CONFIG,
        "cron": "0 10 * * *",
        "password": "must-not-persist",
    }
    assert await worker.process(row["job_id"])
    final = await store.get(row["job_id"])
    assert final["synced_generation"] == 2
    assert json.loads(final["effective_config_json"])["cron"] == "0 10 * * *"
    assert "password" not in final["effective_config_json"]


@pytest.mark.asyncio
async def test_clearing_existing_dream_cron_pauses_known_timer_without_invalid_update(
    system_setup,
):
    db, store, _ = system_setup
    row = await store.refresh(ADMIN, "dream", "save-a")
    client, platform = ConfigClient(db), Platform(db)
    worker = SystemSyncWorker(store, client, platform)
    assert await worker.process(row["job_id"])
    await store.refresh(ADMIN, "dream", "save-b")
    client.config = {**CONFIG, "enabled": False, "cron": ""}
    platform.calls.clear()
    assert await worker.process(row["job_id"])
    assert platform.calls == [("enabled", "123", False)]
    final = await store.get(row["job_id"])
    assert final["sync_status"] == "synced" and not final["enabled"]
