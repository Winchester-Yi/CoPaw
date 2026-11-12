# -*- coding: utf-8 -*-
"""Discover/freeze CLI is read-only toward business storage."""

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner


def command():
    from swe.cli.conversation_storage_discovery_cmd import (
        register_discovery_commands,
    )

    @click.group()
    def group():
        pass

    register_discovery_commands(group)
    return group


def inputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.chmod(0o555)
    nas = tmp_path / "nas"
    nas.mkdir()
    services = tmp_path / "services.json"
    services.write_text(
        json.dumps(
            {
                "services": [
                    {
                        "service_unit_id": "SERVICE",
                        "source_root": str(source),
                        "config_root": str(source),
                        "nas_root": str(nas),
                    },
                ],
            },
        ),
    )
    shares = tmp_path / "shares.json"
    shares.write_text("[]")
    return services, shares, source


@pytest.mark.parametrize("marked", [True, False])
def test_discover_and_freeze_existing_roots(tmp_path, marked, monkeypatch):
    from swe.app.conversation_storage.migration import discovery

    def forbidden_fingerprints(_services):
        pytest.fail(
            "new discovery/freeze must not introduce full source hashing",
        )

    monkeypatch.setattr(discovery, "fingerprints", forbidden_fingerprints)
    services, shares, _ = inputs(tmp_path)
    output = tmp_path / "report.json"
    result = CliRunner().invoke(
        command(),
        [
            "discover",
            "--services",
            str(services),
            "--shares-file",
            str(shares),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text())["ready"]
    boundary = tmp_path / "boundary.json"
    boundary.write_text(
        json.dumps(
            {
                "snapshot_id": "snapshot",
                "captured_at": "2026-11-02T00:00:00Z",
                "old_image_digest": "old@sha256:" + "a" * 64,
                "evidence_refs": ["stop", "readonly"],
            },
        ),
    )
    manifest = tmp_path / "manifest.json"
    result = CliRunner().invoke(
        command(),
        [
            "freeze",
            "--discovery",
            str(output),
            "--boundary",
            str(boundary),
            "--application-schema",
            "business",
            "--manifest",
            str(manifest),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(manifest.read_text())
    assert data["version"] == 3
    assert data["workflow"] == "direct"
    if not marked:
        del data["workflow"]
        manifest.write_text(json.dumps(data))
    from swe.cli.conversation_storage_cmd import conversation_storage_group

    for operation in ("inventory", "plan", "inspect"):
        result = CliRunner().invoke(
            conversation_storage_group,
            [operation, "--manifest", str(manifest)],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["version"] == 3
        assert report["operation"] == operation
        assert report["blockers"] == []
        assert (
            report["migration_id"]
            == json.loads(manifest.read_text())["migration_id"]
        )


def test_discover_refuses_report_inside_frozen_source(tmp_path):
    services, shares, source = inputs(tmp_path)
    result = CliRunner().invoke(
        command(),
        [
            "discover",
            "--services",
            str(services),
            "--shares-file",
            str(shares),
            "--output",
            str(source / "report.json"),
        ],
    )
    assert result.exit_code != 0
    assert "frozen source" in result.output
    assert not (source / "report.json").exists()


def test_default_share_discovery_reads_database_without_ddl(
    tmp_path,
    monkeypatch,
):
    from swe.cli import conversation_storage_discovery_cmd as module

    services, _, _ = inputs(tmp_path)
    calls = []

    async def shares():
        calls.append("read")
        return []

    monkeypatch.setattr(module, "read_original_shares", shares)
    result = CliRunner().invoke(
        command(),
        [
            "discover",
            "--services",
            str(services),
            "--output",
            str(tmp_path / "report.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["read"]


def test_share_reader_issues_only_select_and_closes_connection(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from swe.cli.conversation_storage_discovery_cmd import read_original_shares
    import swe.database

    calls = []

    class Database:
        def __init__(self, _config):
            pass

        async def connect(self):
            calls.append("connect")

        async def close(self):
            calls.append("close")

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetch_all(self, sql, params):
            calls.append((sql, params))
            return []

    monkeypatch.setattr(swe.database, "DatabaseConnection", Database)
    monkeypatch.setattr(swe.database, "get_database_config", lambda: None)
    assert asyncio.run(read_original_shares()) == []
    assert calls[0] == "connect"
    assert calls[-1] == "close"
    assert calls[1][0].startswith("SELECT t.token,")
    assert calls[1][1] == (1000, 0)
