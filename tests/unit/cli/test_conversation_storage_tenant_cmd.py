# -*- coding: utf-8 -*-
"""Tenant administration option routing and partial-success exit contract."""

from contextlib import nullcontext
import json
from unittest.mock import Mock

import click
from click.testing import CliRunner
import pytest

from swe.cli import conversation_storage_v3_cmd as cli


@pytest.mark.parametrize(
    "operation,flag,option",
    [
        ("retry-review", "--reviewed-discovery", "reviewed_discovery"),
        ("rollback-tenants", "--rollback-inputs", "rollback_inputs"),
    ],
)
def test_management_commands_forward_explicit_evidence(
    tmp_path,
    monkeypatch,
    operation,
    flag,
    option,
):
    manifest = tmp_path / "manifest.json"
    evidence = tmp_path / "evidence.json"
    manifest.write_text("{}")
    evidence.write_text("{}")
    handler = Mock()
    monkeypatch.setattr(cli, "run_v3_and_echo", handler)

    @click.group()
    def group():
        pass

    cli.register_v3_commands(group)
    args = [
        operation,
        "--manifest",
        str(manifest),
        flag,
        str(evidence),
        "--apply",
    ]
    if operation == "rollback-tenants":
        args += ["--output-dir", str(tmp_path / "rollback")]
    result = CliRunner().invoke(group, args)
    assert result.exit_code == 0, result.output
    assert handler.call_args.args == (operation, str(manifest))
    assert handler.call_args.kwargs[option] == str(evidence)
    assert handler.call_args.kwargs["apply"] is True
    result = CliRunner().invoke(
        group,
        [operation, "--manifest", str(manifest)],
    )
    assert result.exit_code == 2
    assert flag in result.output
    assert handler.call_count == 1


def test_partial_success_outputs_report_and_exit_two_without_failure_save(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration import disk_manifest, progress

    monkeypatch.setattr(disk_manifest, "read_manifest_header", lambda _: {})
    monkeypatch.setattr(
        disk_manifest,
        "disk_index_policy",
        lambda *a, **kw: nullcontext(),
    )
    monkeypatch.setattr(cli, "V3Manifest", lambda *a: None)
    monkeypatch.setattr(cli, "validate_output", lambda *a: tmp_path)
    report = {"status": "partial_success", "published": 1, "failed": 1}
    monkeypatch.setattr(cli, "run_v3_command", lambda *a, **kw: report)
    failure = Mock()
    monkeypatch.setattr(progress.MigrationProgress, "save_failure", failure)

    @click.command()
    def command():
        cli.run_v3_and_echo("finalize", tmp_path / "manifest.json")

    result = CliRunner().invoke(command)
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout) == report
    failure.assert_not_called()
