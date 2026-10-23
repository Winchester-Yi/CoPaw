# -*- coding: utf-8 -*-
"""Versioned memory roots cannot be merged with stale restore generations."""

import zipfile

import pytest


def test_restore_refuses_merge_of_versioned_memory(tmp_path):
    from swe.app.backup.memory_storage import validate_restore_target

    target = tmp_path / "tenant"
    target.mkdir()
    (target / "config.json").write_text("{}")
    archive = tmp_path / "new.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("sessions/.turn-ledger/generation/metadata.json", "{}")
    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(ValueError, match="empty restore target"):
            validate_restore_target(zf, target)


def test_backup_detects_changed_root_and_pending_ledger(tmp_path):
    from swe.app.backup.memory_storage import (
        capture_memory_roots,
        verify_memory_roots,
    )

    dialog = tmp_path / "dialog" / "chat"
    dialog.mkdir(parents=True)
    root = dialog / "state-v2.json"
    root.write_text('{"generation":"a"}')
    snapshot = capture_memory_roots(tmp_path)
    root.write_text('{"generation":"b"}')
    with pytest.raises(ValueError, match="changed during backup"):
        verify_memory_roots(tmp_path, snapshot)
    pending = tmp_path / "sessions" / ".turn-ledger" / "g" / "pending.json"
    pending.parent.mkdir(parents=True)
    pending.write_text("{}")
    with pytest.raises(ValueError, match="pending"):
        capture_memory_roots(tmp_path)


def test_restore_detects_root_level_checkpoint(tmp_path):
    from swe.app.backup.memory_storage import validate_restore_target

    target = tmp_path / "target"
    target.mkdir()
    (target / "stale").write_text("old")
    archive = tmp_path / "checkpoint.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("state-v2.json", "{}")
    with zipfile.ZipFile(archive) as zf:
        with pytest.raises(ValueError, match="empty restore target"):
            validate_restore_target(zf, target)


def test_versioned_backup_does_not_skip_unreadable_files(
    tmp_path,
    monkeypatch,
):
    from swe.app.backup.worker import _compress_directory

    source = tmp_path / "source"
    source.mkdir()
    (source / "state-v2.json").write_text("{}")
    payload = source / "segment.jsonl"
    payload.write_text("committed")
    archive = tmp_path / "out.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        original = zf.write

        def failing_write(path, *args, **kwargs):
            if path == payload:
                raise OSError("payload is unreadable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(zf, "write", failing_write)
        with pytest.raises(OSError, match="unreadable"):
            _compress_directory(zf, source, source)


@pytest.mark.parametrize("script", ["compress.sh", "decompress.sh"])
def test_shell_backup_refuses_versioned_storage(tmp_path, script):
    import subprocess
    from pathlib import Path

    working = tmp_path / "working"
    tenant = working / "alice" / "dialog" / "chat"
    tenant.mkdir(parents=True)
    (tenant / "state-v2.json").write_text("{}")
    scripts = Path(__file__).resolve().parents[4] / "scripts" / "backup"
    arguments = ["bash", str(scripts / script), "--working-dir", str(working)]
    if script == "decompress.sh":
        zipped = tmp_path / "zip"
        zipped.mkdir()
        with zipfile.ZipFile(zipped / "alice.zip", "w") as zf:
            zf.writestr("state-v2.json", "{}")
        arguments += ["--zip-dir", str(zipped)]
    else:
        arguments += ["--output-dir", str(tmp_path / "out")]
    result = subprocess.run(
        arguments,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Versioned memory storage" in result.stderr


def test_shell_restore_refuses_new_archive_into_empty_target(tmp_path):
    import subprocess
    from pathlib import Path

    zipped = tmp_path / "zips"
    zipped.mkdir()
    with zipfile.ZipFile(zipped / "alice.zip", "w") as zf:
        zf.writestr("sessions/.turn-ledger/g/metadata.json", "{}")
    working = tmp_path / "empty"
    script = (
        Path(__file__).resolve().parents[4] / "scripts/backup/decompress.sh"
    )
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--working-dir",
            str(working),
            "--zip-dir",
            str(zipped),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Versioned memory storage" in result.stderr
    assert not working.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_first", [False, True])
async def test_restore_rejection_only_rolls_back_modified_tenants(
    tmp_path,
    monkeypatch,
    restore_first,
):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from swe.app.backup import worker as module
    from swe.app.backup.models import (
        BackupTask,
        BackupTaskStatus,
        BackupTaskType,
    )

    tenant_ids = ["alice", "bob"] if restore_first else ["bob"]
    incoming = {}
    rollbacks = {}
    for tenant_id in tenant_ids:
        directory = tmp_path / "tenants" / tenant_id
        directory.mkdir(parents=True)
        (directory / "config.json").write_text("original")
        archive = tmp_path / f"incoming-{tenant_id}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("config.json", "replacement")
            if tenant_id == "bob":
                zf.writestr("dialog/chat/state-v2.json", "{}")
        incoming[tenant_id] = archive
        rollback = tmp_path / f"{tenant_id}.zip"
        with zipfile.ZipFile(rollback, "w") as zf:
            zf.writestr("config.json", "original")
        rollbacks[tenant_id] = str(rollback)
    protected = tmp_path / "tenants/bob/config.json"
    original_inode = protected.stat().st_ino
    worker = object.__new__(module.BackupWorker)
    worker.task_store = SimpleNamespace(save=Mock())
    worker.s3_client = SimpleNamespace(
        get_backup_key=lambda _instance, _date, _hour, tenant: tenant,
        download=lambda key, path: path.write_bytes(
            incoming[key].read_bytes(),
        ),
    )
    worker._create_rollback_backup = AsyncMock(
        side_effect=lambda _task, tenant, _directory: rollbacks[tenant],
    )
    worker._rollback_all = AsyncMock()
    monkeypatch.setattr(
        module,
        "get_tenant_working_dir",
        lambda tenant: tmp_path / "tenants" / tenant,
    )
    monkeypatch.setattr(
        module,
        "get_tenant_storage_secrets_dir",
        lambda tenant: tmp_path / "secrets" / tenant,
    )
    monkeypatch.setattr(
        module,
        "get_tenant_storage_providers_dir",
        lambda tenant: tmp_path / "providers" / tenant,
    )
    task = BackupTask(
        task_id="reject-preflight",
        task_type=BackupTaskType.RESTORE,
        status=BackupTaskStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        target_tenant_ids=tenant_ids,
        backup_date="2026-10-15",
        backup_hour=1,
    )
    await worker.run_restore_task(task)
    assert "empty restore target" in str(task.error_message)
    assert protected.read_text() == "original"
    assert protected.stat().st_ino == original_inode
    if restore_first:
        worker._rollback_all.assert_awaited_once_with([rollbacks["alice"]])
    else:
        worker._rollback_all.assert_not_awaited()
        assert task.status == BackupTaskStatus.FAILED
