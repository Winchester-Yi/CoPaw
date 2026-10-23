# -*- coding: utf-8 -*-
"""Regression coverage for the backup execution boundary."""

import asyncio
import importlib
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

from swe.app.backup import batch_service, service, shell_service
from swe.app.backup import shell_router, s3_client
from swe.app.backup import worker as worker_module, shell_worker
from swe.app.backup.config import BackupEnvironmentConfig
from swe.app.backup.models import BackupTask, BackupTaskStatus, BackupTaskType
from swe.app.backup.worker import BackupWorker
from swe.app.backup.shell_worker import ShellBackupWorker

router = importlib.import_module("swe.app.backup.router")


class OffLoopDict(dict):
    """Assert result selection remains inside the execution boundary."""

    def get(self, key, default=None):
        assert threading.current_thread() is not threading.main_thread()
        return super().get(key, default)


@pytest.fixture
def paginated_s3(monkeypatch):
    loop_thread = threading.get_ident()
    calls = []
    config = BackupEnvironmentConfig(
        aws_access_key_id="key",
        aws_secret_access_key="secret",
        s3_bucket="bucket",
    )

    def page(**kwargs):
        assert threading.get_ident() != loop_thread
        calls.append(kwargs)
        second = "ContinuationToken" in kwargs
        tenant = "bob" if second else "alice"
        return {
            "Contents": [
                {
                    "Key": f"swe_backup/default/2026-10-13/14/{tenant}.zip",
                    "Size": 12,
                    "LastModified": datetime(
                        2026,
                        10,
                        13,
                        tzinfo=timezone.utc,
                    ),
                },
            ],
            "IsTruncated": not second,
            "NextContinuationToken": "page-two",
        }

    def client(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        return SimpleNamespace(list_objects_v2=page)

    monkeypatch.setattr(s3_client.boto3, "client", client)
    return config, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("shell", [False, True])
async def test_backup_routes_construct_and_paginate_off_loop(
    monkeypatch,
    paginated_s3,
    shell,
):
    config, calls = paginated_s3
    cls = shell_service.ShellBackupService if shell else service.BackupService
    backup_service = object.__new__(cls)
    backup_service._get_backup_config = lambda: SimpleNamespace(
        get_active_config=lambda: config,
    )
    route_module = shell_router if shell else router
    monkeypatch.setattr(
        route_module,
        "get_shell_backup_service" if shell else "get_backup_service",
        lambda: backup_service,
    )
    list_route = (
        shell_router.list_shell_backups if shell else router.list_backups
    )
    result = await list_route(
        instance_id="default",
        date="2026-10-13",
        hour=14,
        tenant_id=None,
    )
    backups = result["backups"] if shell else result.backups
    assert set(backups["default"]["2026-10-13"][14]) == {"alice", "bob"}
    assert len(calls) == 2
    assert calls[1]["ContinuationToken"] == "page-two"


@pytest.mark.asyncio
async def test_latest_backup_processing_uses_worker(monkeypatch, paginated_s3):
    config, calls = paginated_s3
    monkeypatch.setattr(
        "swe.app.backup.config.load_backup_config",
        lambda: SimpleNamespace(get_active_config=lambda: config),
    )
    result = await batch_service.BatchBackupService().get_latest_backups()
    assert result == {
        "default": {"date": "2026-10-13", "hour": 14, "tenant_count": 2},
    }
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls",
    [service.BackupService, shell_service.ShellBackupService],
)
@pytest.mark.parametrize("restore", [False, True])
async def test_service_worker_construction_uses_worker(cls, restore):
    loop_thread = threading.get_ident()
    backup_service = object.__new__(cls)
    backup_service._lock = asyncio.Lock()
    backup_service.task_store = SimpleNamespace(
        has_running_task=lambda: False,
        save=lambda task: None,
    )

    async def execute(task):
        pass

    def get_worker():
        assert threading.get_ident() != loop_thread
        return SimpleNamespace(
            run_backup_task=execute,
            run_restore_task=execute,
        )

    backup_service._get_worker = get_worker
    method = (
        backup_service.create_restore_task
        if restore
        else backup_service.create_backup_task
    )
    await method()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [BackupWorker, ShellBackupWorker])
async def test_restore_discovery_uses_worker(cls):
    loop_thread = threading.get_ident()

    def list_backups(**kwargs):
        assert threading.get_ident() != loop_thread
        return {"backups": OffLoopDict()}

    worker = object.__new__(cls)
    worker.s3_client = SimpleNamespace(list_backups=list_backups)
    worker.task_store = SimpleNamespace(save=lambda task: None)
    task = BackupTask(
        task_id="test",
        task_type=BackupTaskType.RESTORE,
        backup_date="2026-10-13",
        status=BackupTaskStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    await worker.run_restore_task(task)
    assert task.status == BackupTaskStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("shell", [False, True])
async def test_restore_preview_filters_tenants_inside_worker(
    monkeypatch,
    paginated_s3,
    shell,
):
    config, calls = paginated_s3
    cls = shell_service.ShellBackupService if shell else service.BackupService
    backup_service = object.__new__(cls)
    backup_service._get_backup_config = lambda: SimpleNamespace(
        get_active_config=lambda: config,
    )
    backup_service.create_restore_task = AsyncMock(
        return_value=BackupTask(
            task_id="preview",
            task_type=BackupTaskType.RESTORE,
            status=BackupTaskStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        ),
    )
    module = shell_router if shell else router
    monkeypatch.setattr(
        module,
        "get_shell_backup_service" if shell else "get_backup_service",
        lambda: backup_service,
    )
    request_cls = (
        shell_router.CreateShellRestoreRequest
        if shell
        else router.CreateRestoreRequest
    )
    request = request_cls(
        instance_id="default",
        date="2026-10-13",
        hour=14,
        tenant_ids=["bob", "missing"],
    )
    route = (
        shell_router.create_shell_restore if shell else router.create_restore
    )
    response = await route(request)
    assert response.target_tenants == ["bob"]
    assert backup_service.create_restore_task.await_args.kwargs[
        "tenant_ids"
    ] == ["bob"]
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [BackupWorker, ShellBackupWorker])
async def test_restore_keeps_latest_hour_selection_off_loop(
    monkeypatch,
    tmp_path,
    cls,
):
    loop_thread = threading.get_ident()
    selected_hours = []

    def list_backups(**kwargs):
        assert threading.get_ident() != loop_thread
        return {
            "backups": OffLoopDict(
                {
                    "default": {
                        "2026-10-13": {14: {"alice": {}}, 20: {"bob": {}}},
                    },
                },
            ),
        }

    def key(instance_id, date, hour, tenant):
        selected_hours.append(hour)
        return "key"

    backup_worker = object.__new__(cls)
    backup_worker.s3_client = SimpleNamespace(
        list_backups=list_backups,
        get_backup_key=key,
        download=lambda *args: None,
    )
    backup_worker.task_store = SimpleNamespace(save=lambda task: None)
    backup_worker._extract_zip = AsyncMock()
    backup_worker.shell_executor = SimpleNamespace(
        run_decompress=AsyncMock(return_value=(["alice"], [])),
    )
    monkeypatch.setattr(
        worker_module,
        "get_tenant_working_dir",
        lambda tenant: tmp_path / "absent",
    )
    monkeypatch.setattr(worker_module, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(shell_worker, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(
        shell_worker.tempfile,
        "gettempdir",
        lambda: str(tmp_path),
    )
    task = BackupTask(
        task_id="latest",
        task_type=BackupTaskType.RESTORE,
        backup_date="2026-10-13",
        target_tenant_ids=["alice"],
        status=BackupTaskStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    await backup_worker.run_restore_task(task)
    assert task.status == BackupTaskStatus.COMPLETED
    assert selected_hours == [20]


@pytest.mark.asyncio
async def test_worker_retains_capacity_until_cancelled_thread_finishes():
    from swe.app.backup.io_worker import BackupIOWorker

    worker = BackupIOWorker(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    tenant = ContextVar("backup_test_tenant")
    tenant.set("alice")

    def first():
        assert tenant.get() == "alice"
        started.set()
        assert release.wait(5)

    first_task = asyncio.create_task(worker.run(first))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        second_task = asyncio.create_task(worker.run(second_started.set))
        await asyncio.sleep(0.03)
        assert not second_started.is_set()
        assert worker._executor._work_queue.qsize() == 0
        release.set()
        await asyncio.wait_for(second_task, 2)
        assert second_started.is_set()
    finally:
        release.set()
        worker._executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancellation_before_admission_does_not_submit():
    from swe.app.backup.io_worker import BackupIOWorker

    worker = BackupIOWorker(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    cancelled_work = threading.Event()

    def first():
        started.set()
        assert release.wait(5)

    task = asyncio.create_task(worker.run(first))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
        queued = asyncio.create_task(worker.run(cancelled_work.set))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await task
        assert await worker.run(lambda: "next") == "next"
        assert not cancelled_work.is_set()
    finally:
        release.set()
        worker._executor.shutdown(wait=True)


def test_worker_budget_survives_event_loop_replacement():
    from swe.app.backup.io_worker import BackupIOWorker

    worker = BackupIOWorker(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        assert release.wait(5)

    async def cancel_on_first_loop():
        task = asyncio.create_task(worker.run(blocking))
        await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def use_second_loop():
        task = asyncio.create_task(worker.run(lambda: "second"))
        await asyncio.sleep(0.03)
        assert worker._executor._work_queue.qsize() == 0
        release.set()
        assert await task == "second"

    try:
        asyncio.run(cancel_on_first_loop())
        asyncio.run(use_second_loop())
    finally:
        release.set()
        worker._executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("shell", [False, True])
async def test_concurrent_service_worker_initialization_is_singleton(
    monkeypatch,
    shell,
):
    from swe.app.backup.io_worker import run_backup_io

    module = shell_service if shell else service
    cls = module.ShellBackupService if shell else module.BackupService
    monkeypatch.setattr(module, "TaskStore", SimpleNamespace)
    backup_service = cls()
    backup_service._get_backup_config = lambda: SimpleNamespace(
        get_active_config=object,
        shell_script=object(),
    )
    started = threading.Event()
    release = threading.Event()
    constructed = []

    def construct(*args):
        instance = object()
        constructed.append(instance)
        started.set()
        assert release.wait(5)
        return instance

    monkeypatch.setattr(
        module,
        "ShellBackupWorker" if shell else "BackupWorker",
        construct,
    )
    tasks = [
        asyncio.create_task(run_backup_io(backup_service._get_worker))
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
        await asyncio.sleep(0.03)
        assert len(constructed) == 1
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert results[0] is results[1]


@pytest.mark.asyncio
async def test_submit_failure_releases_admission(monkeypatch):
    from swe.app.backup.io_worker import BackupIOWorker

    worker = BackupIOWorker(max_workers=1)
    submit = worker._executor.submit

    def reject(*args):
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(worker._executor, "submit", reject)
    with pytest.raises(RuntimeError, match="executor unavailable"):
        await worker.run(lambda: None)
    monkeypatch.setattr(worker._executor, "submit", submit)
    try:
        assert (
            await asyncio.wait_for(worker.run(lambda: "recovered"), 2)
            == "recovered"
        )
        assert worker._available == 1
    finally:
        worker._executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_operation_failure_propagates_and_releases_admission():
    from swe.app.backup.io_worker import BackupIOWorker

    worker = BackupIOWorker(max_workers=1)

    def fail():
        raise ValueError("S3 failed")

    try:
        with pytest.raises(ValueError, match="S3 failed"):
            await worker.run(fail)
        assert await asyncio.wait_for(worker.run(lambda: "next"), 2) == "next"
        assert worker._available == 1
    finally:
        worker._executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls",
    [service.BackupService, shell_service.ShellBackupService],
)
@pytest.mark.parametrize("restore", [False, True])
async def test_cancelled_initialization_does_not_leave_pending_task(
    cls,
    restore,
):
    backup_service = object.__new__(cls)
    backup_service._lock = asyncio.Lock()
    saved = []
    backup_service.task_store = SimpleNamespace(
        has_running_task=lambda: False,
        save=saved.append,
    )
    started = threading.Event()
    release = threading.Event()

    def initialize():
        started.set()
        assert release.wait(5)
        return object()

    backup_service._get_worker = initialize
    method = (
        backup_service.create_restore_task
        if restore
        else backup_service.create_backup_task
    )
    task = asyncio.create_task(method())
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert saved == []
        assert not backup_service._lock.locked()
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls",
    [service.BackupService, shell_service.ShellBackupService],
)
@pytest.mark.parametrize("restore", [False, True])
async def test_task_creation_serializes_initialization_with_conflict_check(
    cls,
    restore,
):
    backup_service = object.__new__(cls)
    backup_service._lock = asyncio.Lock()
    checks = []
    saved = []

    def has_running():
        checks.append(True)
        return False

    backup_service.task_store = SimpleNamespace(
        has_running_task=has_running,
        save=saved.append,
    )
    started = threading.Event()
    release = threading.Event()

    async def execute(task):
        pass

    def initialize():
        started.set()
        assert release.wait(5)
        return SimpleNamespace(
            run_backup_task=execute,
            run_restore_task=execute,
        )

    backup_service._get_worker = initialize
    method = (
        backup_service.create_restore_task
        if restore
        else backup_service.create_backup_task
    )
    tasks = [asyncio.create_task(method())]
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
        tasks.append(asyncio.create_task(method()))
        await asyncio.sleep(0.03)
        assert len(checks) == 1
        assert saved == []
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert len(checks) == 2
    assert not backup_service._lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls",
    [service.BackupService, shell_service.ShellBackupService],
)
@pytest.mark.parametrize("restore", [False, True])
async def test_running_task_conflict_precedes_sdk_initialization(cls, restore):
    backup_service = object.__new__(cls)
    backup_service._lock = asyncio.Lock()
    backup_service.task_store = SimpleNamespace(has_running_task=lambda: True)

    def initialize():
        pytest.fail("SDK initialization must not run on a conflict")

    backup_service._get_worker = initialize
    method = (
        backup_service.create_restore_task
        if restore
        else backup_service.create_backup_task
    )
    with pytest.raises(service.HTTPException) as exc:
        await method()
    assert exc.value.status_code == 409
    assert not backup_service._lock.locked()
