# -*- coding: utf-8 -*-
"""Local CLI enumeration regression; preflight SQL itself is not exercised."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from swe.app.runner.models import ChatSpec, ChatsFile
from swe.cli.main import cli


def _write_json(path, data):
    path.write_text(json.dumps(data))
    return str(path)


def _source(tmp_path, request):
    source = tmp_path / "source"
    tenant = source / "default_SRC"
    workspace = tenant / "workspaces/agent"
    (workspace / "sessions").mkdir(parents=True)
    (workspace / "dialog").mkdir()
    nas = tmp_path / "nas"
    runtime = nas / "default_SRC/workspaces/agent"
    runtime.mkdir(parents=True)
    _write_json(
        tenant / "config.json",
        {
            "agents": {
                "profiles": {
                    "agent": {
                        "id": "agent",
                        "workspace_dir": str(runtime),
                        "enabled": False,
                    },
                },
            },
        },
    )
    _write_json(workspace / "agent.json", {"id": "agent"})
    chat = ChatSpec(
        id="16374f34-c02e-4bb9-87bf-eb5e64348cef",
        session_id="session",
        user_id="user",
        channel="console",
    )
    registry = workspace / "chats.json"
    registry.write_text(ChatsFile(chats=[chat]).model_dump_json())
    session = workspace / "sessions/user_session.json"
    _write_json(session, {"schema_version": 1, "future_module": {"x": True}})
    attachments = workspace / "attachments"
    attachments.mkdir()
    with (attachments / "large.bin").open("wb") as handle:
        handle.truncate(20 * 1024 * 1024)
    code = workspace / "code"
    deep = code.joinpath(*["deep"] * 20)
    deep.mkdir(parents=True)
    (deep / "unrelated.py").write_text("business source")
    (code / "outside-link").symlink_to("/unrelated-missing-target")
    for path in source.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    _restore_after_test(request, source)
    return source, workspace, nas, (attachments, code), registry, session


def _restore_after_test(request, source):
    fixture_directories = [
        path
        for path in source.rglob("*")
        if not path.is_symlink() and path.is_dir()
    ]

    def restore_permissions():
        source.chmod(0o755)
        for path in fixture_directories:
            path.chmod(0o755)

    request.addfinalizer(restore_permissions)


def test_cli_semantic_spools_ignore_unrelated_business_inputs(
    tmp_path,
    monkeypatch,
    request,
):
    from swe.cli import conversation_storage_v3_cmd as command
    from swe.database.connection import DatabaseConnection

    source, workspace, nas, ignored, registry, session = _source(
        tmp_path,
        request,
    )
    expected_bytes = registry.stat().st_size + session.stat().st_size
    services = _write_json(
        tmp_path / "services.json",
        {
            "services": [
                {
                    "service_unit_id": "SERVICE",
                    "source_root": str(source),
                    "config_root": str(source),
                    "original_root": str(nas),
                    "nas_root": str(nas),
                },
            ],
        },
    )
    shares = _write_json(tmp_path / "shares.json", [])
    boundary = _write_json(
        tmp_path / "boundary.json",
        {
            "snapshot_id": "snapshot",
            "captured_at": "2026-11-02T00:00:00Z",
            "old_image_digest": "old@sha256:" + "a" * 64,
            "evidence_refs": ["writers-stopped", "readonly"],
        },
    )
    original_scan, original_open = os.scandir, Path.open

    def guard(value):
        path = Path(value)
        assert not any(
            path == parent or parent in path.parents for parent in ignored
        ), "unrelated business input was traversed or opened"

    def scan(path):
        if not isinstance(path, int):
            guard(path)
        return original_scan(path)

    def open_path(path, *args, **kwargs):
        guard(path)
        return original_open(path, *args, **kwargs)

    async def no_database(*_args, **_kwargs):
        pytest.fail("local CLI boundary regression must not connect to MySQL")

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(DatabaseConnection, "connect", no_database)

    def invoke(operation, *options):
        result = CliRunner().invoke(
            cli,
            ["conversation-storage", operation, *options],
        )
        assert result.exit_code == 0, (result.output, result.exception)
        return json.loads(result.stdout)

    report_path, manifest = (
        tmp_path / "discovery.json",
        tmp_path / "manifest.json",
    )
    resources = [
        "--workers",
        "2",
        "--io-workers",
        "2",
        "--memory-budget-mib",
        "128",
    ]
    discovered = invoke(
        "discover",
        "--services",
        services,
        "--shares-file",
        shares,
        "--output",
        str(report_path),
        *resources,
    )
    assert discovered["ready"]
    discovery = json.loads(report_path.read_text())
    assert len(discovery["workspaces"]) == 1
    assert discovery["fingerprints"] == []
    invoke(
        "freeze",
        "--discovery",
        str(report_path),
        "--boundary",
        boundary,
        "--application-schema",
        "business",
        "--manifest",
        str(manifest),
        *resources,
    )
    frozen = manifest.read_bytes()
    for operation in ("inventory", "plan"):
        report = invoke(operation, "--manifest", str(manifest), *resources)
        assert report["ready"] and report["blockers"] == []
        assert report["files"] == 2
        assert report["workspaces"] == 1
        assert report["source_scopes"][0]["sessions"] == 1
        assert report["source_scopes"][0]["chats"] == 1
    inspected = []

    async def sql_boundary(
        operation,
        _manifest,
        inventory,
        _output,
        **_options,
    ):
        # Only this final SQL phase is replaced: parsing and disk spooling above
        # remain the production CLI path, including preflight without --apply.
        assert operation == "preflight"
        assert inventory["ready"] and inventory["blockers"] == []
        files = list(inventory["files"])
        assert {item["path"] for item in files} == {
            "chats.json",
            "sessions/user_session.json",
        }
        assert sum(item["bytes"] for item in files) == expected_bytes
        workspaces = list(inventory["workspaces"])
        assert len(workspaces) == 1
        assert workspaces[0]["root"] == str(workspace)
        assert (
            len(workspaces[0]["sessions"]) == len(workspaces[0]["chats"]) == 1
        )
        inspected.append(True)
        return {"local_input_boundary_checked": True}

    monkeypatch.setattr(command, "_execute", sql_boundary)
    assert invoke("preflight", "--manifest", str(manifest), *resources) == {
        "local_input_boundary_checked": True,
    }
    assert inspected == [True]
    assert manifest.read_bytes() == frozen
