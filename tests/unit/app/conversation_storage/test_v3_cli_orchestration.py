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
