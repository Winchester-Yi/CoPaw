# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from swe.cli.conversation_storage_cmd import conversation_storage_group
from swe.app.conversation_storage import runtime
from swe.config.context import encode_scope_id


def test_prepare_defaults_to_metadata_and_preview_is_read_only(
    tmp_path,
    monkeypatch,
):
    from swe.database import config

    monkeypatch.setattr(
        config,
        "get_database_config",
        lambda: SimpleNamespace(database="test"),
    )
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "chats.json").write_text('{"version": 1, "chats": []}')
    mapping = tmp_path / "workspaces.json"
    mapping.write_text(
        json.dumps(
            [
                {
                    "root": str(root),
                    "scope": ["svc", "tenant", "source", "agent"],
                },
            ],
        ),
    )
    manifest = tmp_path / "manifest.json"
    runner = CliRunner()
    result = runner.invoke(
        conversation_storage_group,
        ["prepare", "--workspaces", str(mapping), "--manifest", str(manifest)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(manifest.read_text())["mode"] == "mysql-metadata"
    result = runner.invoke(
        conversation_storage_group,
        ["migrate", "--manifest", str(manifest), "--full-audit"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "preview"


def test_prepare_scans_default_workspaces_and_migrate_reads_current_registry(
    tmp_path,
    monkeypatch,
):
    from swe.database import config

    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "svc")
    monkeypatch.setattr(
        config,
        "get_database_config",
        lambda: SimpleNamespace(database="test"),
    )
    nas = tmp_path / "nas"
    root = nas / encode_scope_id("tenant", "source") / "workspaces" / "default"
    root.mkdir(parents=True)
    registry = root / "chats.json"
    registry.write_text("broken JSON")
    other = root.parent / "other-agent"
    other.mkdir()
    (other / "chats.json").write_text("also broken")
    (nas / "current-backup").symlink_to(other, target_is_directory=True)
    manifest = tmp_path / "scan.json"
    runner = CliRunner()
    result = runner.invoke(
        conversation_storage_group,
        ["prepare", "--workspace-root", str(nas), "--manifest", str(manifest)],
    )
    assert result.exit_code == 2, result.output
    scan = json.loads(manifest.read_text())
    assert scan["scan_errors"] == []
    (item,) = scan["workspaces"]
    assert item["scope"] == ["svc", "tenant", "source", "default"]
    assert item["root"] == str(root)
    assert "scan_error" in item
    assert "registry_sha256" not in item

    registry.write_text('{"version": 1, "chats": []}')
    result = runner.invoke(
        conversation_storage_group,
        ["migrate", "--manifest", str(manifest)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["validated"] == [
        ["svc", "tenant", "source"],
    ]

    registry.write_text('{"version": 1, "chats": [{}]}')
    result = runner.invoke(
        conversation_storage_group,
        ["migrate", "--manifest", str(manifest)],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["failed"][0]["phase"] == "preflight"


def test_rejects_bundle_mode_and_manifest(tmp_path, monkeypatch):
    from swe.database import config

    monkeypatch.setattr(
        config,
        "get_database_config",
        lambda: SimpleNamespace(database="test"),
    )
    runner = CliRunner()
    result = runner.invoke(
        conversation_storage_group,
        [
            "prepare",
            "--mode",
            "bundle",
            "--workspaces",
            __file__,
            "--manifest",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    manifest = tmp_path / "old.json"
    manifest.write_text('{"mode":"bundle","version":1}')
    result = runner.invoke(
        conversation_storage_group,
        ["migrate", "--manifest", str(manifest)],
    )
    assert result.exit_code != 0
    assert "invalid MySQL metadata manifest" in result.output


@pytest.mark.asyncio
async def test_runtime_rejects_removed_authority(monkeypatch):
    monkeypatch.setenv(runtime.AUTHORITY_ENV, "mysql")
    with pytest.raises(ValueError, match="authority"):
        await runtime.configure_runtime(
            SimpleNamespace(is_connected=True, fetch_all=AsyncMock()),
        )


@pytest.mark.parametrize("document", ["[]", "null", '"text"'])
@pytest.mark.parametrize("command", ["migrate", "export-metadata"])
def test_non_object_manifest_has_a_clear_error(
    tmp_path,
    monkeypatch,
    document,
    command,
):
    from swe.database import config

    monkeypatch.setattr(
        config,
        "get_database_config",
        lambda: SimpleNamespace(database="test"),
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(document)
    args = [command, "--manifest", str(manifest)]
    if command == "export-metadata":
        args += ["--destination", str(tmp_path / "export")]
    result = CliRunner().invoke(conversation_storage_group, args)
    assert result.exit_code != 0
    assert "invalid MySQL metadata manifest" in result.output
    assert isinstance(result.exception, SystemExit)


def test_partial_migration_exits_with_code_two(tmp_path, monkeypatch):
    from swe.cli import conversation_storage_workflow_cmd as workflow

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    execute = AsyncMock(return_value={"status": "partial_success"})
    monkeypatch.setattr(workflow, "_execute_metadata", execute)
    result = CliRunner().invoke(
        conversation_storage_group,
        ["migrate", "--manifest", str(manifest), "--workers", "2"],
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["status"] == "partial_success"
    assert execute.await_args.kwargs["workers"] == 2
