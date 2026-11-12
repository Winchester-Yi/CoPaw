# -*- coding: utf-8 -*-
# pylint: disable=use-dict-literal
from pathlib import Path
from types import SimpleNamespace
import pytest


def test_runtime_receipt_uses_original_workspace_and_includes_share_only_services():
    from swe.cli.conversation_storage_v3_cmd import authority_receipt

    manifest = SimpleNamespace(
        migration_id="job",
        data={
            "services": [
                {"service_unit_id": "svc", "nas_root": "/nas"},
                {"service_unit_id": "shares", "nas_root": "/shares"},
            ],
            "workspaces": [
                dict(
                    service_unit_id="svc",
                    tenant_id="t",
                    source_id="s",
                    agent_id="a",
                    root="/frozen/a",
                    runtime_root="/nas/a",
                ),
            ],
        },
    )
    receipt = authority_receipt(manifest)
    assert receipt["services"]["svc"]["workspaces"][0]["root"] == "/nas/a"
    assert receipt["services"]["shares"]["workspaces"] == []


def test_output_refuses_source_and_nas_overlap(tmp_path):
    from swe.cli.conversation_storage_v3_cmd import validate_output

    source = tmp_path / "source"
    source.mkdir()
    data = {
        "services": [
            {
                "source_root": str(source),
                "config_root": str(source),
                "nas_root": str(tmp_path / "nas"),
            },
        ],
    }
    with pytest.raises(ValueError, match="overlap"):
        validate_output(source / "receipts", data)
    assert validate_output(tmp_path / "audit", data) == tmp_path / "audit"


async def test_original_shares_compared_as_complete_set():
    from unittest.mock import AsyncMock
    from swe.cli.conversation_storage_v3_cmd import compare_original_shares

    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="share"):
        await compare_original_shares(db, {"shares": [{"token": "missing"}]})


def test_output_names_support_long_valid_service_id(tmp_path):
    from hashlib import sha256
    from swe.cli.conversation_storage_v3_cmd import _write_outputs

    service = "x" * 256
    receipt = {
        "version": 1,
        "migration_id": "job",
        "services": {service: {"nas_root": "/nas", "workspaces": []}},
    }
    _write_outputs(tmp_path, receipt)
    expected = tmp_path / (
        sha256(service.encode()).hexdigest() + ".runtime.json"
    )
    assert expected.is_file()
    _write_outputs(tmp_path, receipt)


def test_v3_commands_are_registered_on_main_group():
    from click.testing import CliRunner
    from swe.cli.main import cli

    result = CliRunner().invoke(cli, ["conversation-storage", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("preflight", "resume", "finalize"):
        assert command in result.output


def test_finalize_rejects_missing_runtime_workspace_before_ready(tmp_path):
    from swe.cli.conversation_storage_v3_cmd import validate_runtime_roots

    with pytest.raises(ValueError, match="existing|runtime"):
        validate_runtime_roots(
            {
                "services": [{"nas_root": str(tmp_path)}],
                "workspaces": [{"runtime_root": str(tmp_path / "missing")}],
            },
        )


def test_finalize_rejects_runtime_symlink_ancestor(tmp_path):
    from swe.cli.conversation_storage_v3_cmd import validate_runtime_roots

    actual = tmp_path / "actual"
    (actual / "workspace").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|canonical"):
        validate_runtime_roots(
            {
                "services": [{"nas_root": str(tmp_path)}],
                "workspaces": [{"runtime_root": str(alias / "workspace")}],
            },
        )


def test_finalize_accepts_existing_canonical_runtime_roots(tmp_path):
    from swe.cli.conversation_storage_v3_cmd import validate_runtime_roots

    validate_runtime_roots(
        {
            "services": [{"nas_root": str(tmp_path)}],
            "workspaces": [{"runtime_root": str(tmp_path)}],
        },
    )


@pytest.mark.parametrize(
    "operation",
    (
        "inventory",
        "plan",
        "inspect",
        "preflight",
        "import",
        "resume",
        "verify",
        "finalize",
    ),
)
def test_direct_commands_expose_uniform_resource_options(operation):
    from click.testing import CliRunner
    from swe.cli.conversation_storage_cmd import conversation_storage_group

    result = CliRunner().invoke(
        conversation_storage_group,
        [operation, "--help"],
    )
    assert result.exit_code == 0
    for flag in (
        "--workers",
        "--io-workers",
        "--memory-budget-mib",
        "--work-dir",
    ):
        assert flag in result.output


@pytest.mark.parametrize(
    "operation",
    ("inventory", "plan", "inspect", "import", "resume", "finalize"),
)
async def test_preview_uses_spool_without_database_or_cutoff(
    tmp_path,
    monkeypatch,
    operation,
):
    from unittest.mock import AsyncMock
    from swe.cli import conversation_storage_v3_cmd as command

    source = tmp_path / "source"
    nas = tmp_path / "nas"
    work = tmp_path / "work"
    for root in (source, nas, work):
        root.mkdir()
    manifest = SimpleNamespace(
        migration_id="job",
        data={
            "services": [{"source_root": str(source), "nas_root": str(nas)}],
            "workspaces": [],
            "offline": {"application_schema": "application"},
        },
    )
    inventory = {
        "workspaces": [],
        "files": iter(()),
        "shares": [],
        "source_boundary": {},
        "blockers": [],
        "ready": True,
    }
    parse = AsyncMock(return_value=inventory)
    monkeypatch.setattr(command, "inventory_v3_spooled", parse)
    execute = AsyncMock(
        side_effect=AssertionError("preview connected to database"),
    )
    monkeypatch.setattr(command, "_execute", execute)
    result = await command._execute_spooled(
        operation,
        manifest,
        None,
        workers=8,
        io_workers=2,
        memory_budget_mib=128,
        work_dir=work,
        apply=False,
    )
    assert result["ready"]
    assert parse.call_args.kwargs["begin_sync"] is False
    assert parse.call_args.kwargs["workers"] == 8
    execute.assert_not_called()


async def test_execute_captures_exact_named_lock_and_passes_owner_health(
    monkeypatch,
):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    import asyncio
    from swe.cli import conversation_storage_v3_cmd as command
    from swe.database.connection import _named_lock_name
    from swe.app.conversation_storage.migration.lock_guard import (
        MigrationLockGuard,
    )

    owner = asyncio.current_task()
    lock_calls = []

    class Connection:
        def __init__(self, _config):
            pass

        connect = AsyncMock()
        close = AsyncMock()

        @asynccontextmanager
        async def named_lock(self, name, **_options):
            lock_calls.append(name)
            yield

    db = SimpleNamespace(
        fetch_one=AsyncMock(
            return_value={"schema_name": "app", "server_uuid": "server"},
        ),
    )
    capture_calls = []

    async def capture(database, lock_name, **identity):
        assert asyncio.current_task() is owner
        assert database is db
        capture_calls.append((lock_name, identity))
        return SimpleNamespace(check=health)

    async def health():
        assert asyncio.current_task() is owner

    class Importer:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run_parallel(self, _inventory, **options):
            assert options["health_check"] is health
            await options["health_check"]()
            raise RuntimeError("health passed to importer")

    monkeypatch.setattr(
        "swe.database.connection.DatabaseConnection",
        Connection,
    )
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: SimpleNamespace(database="app", model_copy=lambda **_: None),
    )
    monkeypatch.setattr(command, "DMLDatabase", lambda _control: db)
    monkeypatch.setattr(
        command,
        "BundleStore",
        lambda _path: SimpleNamespace(root=Path("/nas")),
    )
    monkeypatch.setattr(
        command,
        "MigrationJournal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(command, "check_schema", AsyncMock(return_value={}))
    monkeypatch.setattr(command, "compare_original_shares", AsyncMock())
    monkeypatch.setattr(command, "_inspect_job", AsyncMock(return_value=None))
    monkeypatch.setattr(command, "DirectImporter", Importer)
    monkeypatch.setattr(MigrationLockGuard, "capture", capture)
    binding = {
        "schema": "app",
        "migration_id": "job",
        "nas_roots": {"svc": "/nas"},
    }
    manifest = SimpleNamespace(
        migration_id="job",
        _validate_target=lambda _value: None,
        data={
            "offline": {"application_schema": "app"},
            "target": binding,
            "services": [{"service_unit_id": "svc", "nas_root": "/nas"}],
        },
    )
    with pytest.raises(RuntimeError, match="health passed to importer"):
        await command._execute("import", manifest, {}, None)
    assert lock_calls == ["conversation-migration:app"]
    assert capture_calls == [
        (
            _named_lock_name(lock_calls[0]),
            {"schema": "app", "server_uuid": "server"},
        ),
    ]


@pytest.mark.parametrize("operation", ("discover", "freeze"))
def test_discovery_commands_expose_resource_limits(operation):
    from click.testing import CliRunner
    from swe.cli.conversation_storage_cmd import conversation_storage_group

    result = CliRunner().invoke(
        conversation_storage_group,
        [operation, "--help"],
    )
    assert result.exit_code == 0
    for flag in (
        "--workers",
        "--io-workers",
        "--memory-budget-mib",
        "--work-dir",
    ):
        assert flag in result.stdout


async def test_preview_source_blocker_does_not_rewrite_manifest(
    tmp_path,
    monkeypatch,
):
    from unittest.mock import AsyncMock
    from swe.cli import conversation_storage_v3_cmd as command
    from swe.app.conversation_storage.migration import (
        inventory_v3 as inventory,
    )

    source = tmp_path / "source"
    nas = tmp_path / "nas"
    work = tmp_path / "work"
    for root in (source, nas, work):
        root.mkdir()
    path = tmp_path / "manifest.json"
    path.write_bytes(b'{"immutable":"source binding"}')
    original = path.read_bytes()

    def record_change(_blocker):
        path.write_bytes(b"unexpected mutation")

    manifest = SimpleNamespace(
        migration_id="job",
        data={
            "services": [{"source_root": str(source), "nas_root": str(nas)}],
            "workspaces": [],
            "shares": [],
            "fingerprints": [],
            "source_boundary": {
                "snapshot_id": "snapshot",
                "captured_at": "2026-11-01T00:00:00Z",
                "old_image_digest": "old@sha256:" + "a" * 64,
                "evidence_refs": ["stop"],
            },
            "offline": {"application_schema": "app"},
        },
        record_source_change=record_change,
    )
    blocker = {
        "code": "source_modified_after_sync_started",
        "position": "workspace",
    }
    monkeypatch.setattr(
        "swe.app.conversation_storage.migration.inventory_parallel.spool_workspaces",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        inventory,
        "_inventory_shares",
        lambda _data, report: report["blockers"].append(blocker),
    )
    result = await command._execute_spooled(
        "inventory",
        manifest,
        None,
        workers=4,
        io_workers=4,
        memory_budget_mib=128,
        work_dir=work,
        apply=False,
    )
    assert result["blockers"] == [blocker]
    assert path.read_bytes() == original
