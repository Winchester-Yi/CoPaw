# -*- coding: utf-8 -*-
"""Default Agent Profile Hook-management service tests."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from swe.app import hook_management
from swe.agents.hook_runtime.models import HookContext, HookHandlerResult
from swe.app.hook_management import (
    HookAuditActor,
    HookManagementConflict,
    HookManagementService,
    HookManagementValidationError,
    UploadFilePayload,
)


def _append_test_context(workspace_dir: Path, response="approved answer"):
    return HookContext(
        session_id="manual-only",
        transcript_path="",
        cwd=str(workspace_dir),
        hook_event_name="Stop",
        tenant_id="tenant-a",
        effective_tenant_id="tenant-a",
        user_id="user-a",
        agent_id="default",
        channel="test",
        assistant_response=response,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("specific_output", "failed", "expected_bytes"),
    [
        ({"appendText": "附加🙂"}, False, 10),
        ({}, False, 0),
        ({"replacementText": "private replacement"}, True, 0),
    ],
)
async def test_manual_append_runs_real_command_with_redacted_utf8_summary(
    tmp_path,
    monkeypatch,
    specific_output,
    failed,
    expected_bytes,
):
    workspace = tmp_path / "default"
    _write_default_agent(workspace)
    scripts = workspace / "hooks" / "scripts"
    scripts.mkdir(parents=True)
    output = {
        "decision": "allow",
        "reason": "private diagnostic",
        "hookSpecificOutput": specific_output,
    }
    (scripts / "append.py").write_text(
        "import json, pathlib, sys\n"
        "assert json.load(sys.stdin)['assistant_response'] == 'approved answer'\n"
        "p = pathlib.Path('execution-count')\n"
        "p.write_text(p.read_text() + '1' if p.exists() else '1')\n"
        "print('private stderr', file=sys.stderr)\n"
        f"print({json.dumps(output, ensure_ascii=False)!r})\n",
        encoding="utf-8",
    )
    before = (workspace / "agent.json").read_bytes()
    audit = Mock()
    monkeypatch.setattr(hook_management.logger, "info", audit)
    service = HookManagementService(workspace, tenant_id="tenant-a")

    result = await service.manual_test(
        handler={
            "id": "append",
            "type": "command",
            "argv": ["python", "hooks/scripts/append.py"],
            "outputAppend": True,
            "failPolicy": "block",
        },
        context=_append_test_context(workspace),
        actor=_actor(),
    )

    summary = result.redacted_summary
    assert summary["output_append"] is True
    assert summary["append_applied"] is (expected_bytes > 0)
    assert summary["append_bytes"] == expected_bytes
    assert summary["effective_fail_policy"] == "allow"
    assert summary["failed"] is failed
    assert summary["status"] == ("failed" if failed else "completed")
    assert bool(summary["failure_type"]) is failed
    assert isinstance(summary["duration_ms"], int)
    assert summary["duration_ms"] >= 0
    assert (workspace / "execution-count").read_text() == "1"
    assert (workspace / "agent.json").read_bytes() == before
    assert not (workspace / "sessions").exists()
    assert not (workspace / "memory").exists()
    exposed = json.dumps(summary, ensure_ascii=False) + str(
        audit.call_args_list,
    )
    for secret in (
        "附加🙂",
        "private diagnostic",
        "private stderr",
        "private replacement",
        "approved answer",
    ):
        assert secret not in exposed


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "", "  \n\t"])
async def test_manual_append_requires_nonempty_response_before_execution(
    tmp_path,
    monkeypatch,
    response,
):
    workspace = tmp_path / "default"
    _write_default_agent(workspace)
    execute = AsyncMock()
    monkeypatch.setattr(hook_management, "execute_handler", execute)
    service = HookManagementService(workspace, tenant_id="tenant-a")

    with pytest.raises(
        HookManagementValidationError,
        match="assistant_response",
    ):
        await service.manual_test(
            handler={
                "id": "append",
                "type": "command",
                "argv": ["echo"],
                "outputAppend": True,
            },
            context=_append_test_context(workspace, response),
            actor=_actor(),
        )
    execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("event", "extra"),
    [
        ("PreToolUse", {}),
        ("Stop", {"once": True}),
        ("Stop", {"outputTransform": True}),
    ],
)
def test_save_rejects_invalid_append_combinations(tmp_path, event, extra):
    workspace = tmp_path / "default"
    _write_default_agent(workspace)
    service = HookManagementService(workspace, tenant_id="tenant-a")
    with pytest.raises(HookManagementValidationError):
        service.save_configuration(
            hooks={
                "enabled": True,
                "events": {
                    event: [
                        {
                            "id": "append-group",
                            "hooks": [
                                {
                                    "id": "append",
                                    "type": "command",
                                    "argv": ["echo"],
                                    "outputAppend": True,
                                    **extra,
                                },
                            ],
                        },
                    ],
                },
            },
            expected_revision=service.get_configuration().revision,
            actor=_actor(),
        )


@pytest.mark.asyncio
async def test_save_and_distribution_preserve_output_append(tmp_path):
    source_workspace, target_workspace = (
        tmp_path / "source",
        tmp_path / "target",
    )
    _write_default_agent(source_workspace)
    _write_default_agent(target_workspace)
    source = HookManagementService(source_workspace, tenant_id="tenant-a")
    target = HookManagementService(target_workspace, tenant_id="tenant-b")
    source.save_configuration(
        hooks={
            "enabled": True,
            "events": {
                "Stop": [
                    {
                        "id": "append-group",
                        "hooks": [
                            {
                                "id": "append",
                                "type": "command",
                                "argv": ["echo"],
                                "outputAppend": True,
                                "failPolicy": "block",
                            },
                        ],
                    },
                ],
            },
        },
        expected_revision=source.get_configuration().revision,
        actor=_actor(),
    )
    await source.distribute_to_target(
        target=target,
        matcher_group_ids=["append-group"],
        actor=_actor(),
        activate=AsyncMock(),
    )
    for service in (source, target):
        handler = service.get_configuration().hooks["events"]["Stop"][0][
            "hooks"
        ][0]
        assert handler["outputAppend"] is True
        assert handler["failPolicy"] == "block"


def _write_default_agent(workspace_dir: Path) -> None:
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "agent.json").write_text(
        json.dumps({"id": "default", "name": "Default Agent"}),
        encoding="utf-8",
    )


def _actor() -> HookAuditActor:
    return HookAuditActor(user_id="user-a", tenant_id="tenant-a")


def _duplicate_handler_ids() -> dict[str, object]:
    return {
        "enabled": True,
        "events": {
            "PreToolUse": [
                {
                    "id": "shells",
                    "hooks": [
                        {
                            "id": "duplicate",
                            "type": "command",
                            "argv": ["echo"],
                        },
                    ],
                },
                {
                    "id": "network",
                    "hooks": [
                        {
                            "id": "duplicate",
                            "type": "command",
                            "argv": ["printf"],
                        },
                    ],
                },
            ],
        },
    }


def test_save_rejects_stale_revision(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    original = service.get_configuration()

    service.save_configuration(
        hooks={"enabled": True, "events": {}},
        expected_revision=original.revision,
        actor=_actor(),
    )

    with pytest.raises(HookManagementConflict):
        service.save_configuration(
            hooks={"enabled": False, "events": {}},
            expected_revision=original.revision,
            actor=_actor(),
        )


def test_configuration_lock_returns_conflict_when_file_lock_is_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")

    class _BusyFcntl:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 4

        @staticmethod
        def flock(_fd: int, flags: int) -> None:
            if flags & _BusyFcntl.LOCK_NB:
                raise BlockingIOError()

    monkeypatch.setattr(hook_management, "fcntl", _BusyFcntl)

    with pytest.raises(HookManagementConflict, match="busy"):
        with service._configuration_lock():
            pass


def test_save_rejects_duplicate_handler_ids_across_matcher_groups(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")

    with pytest.raises(
        HookManagementValidationError,
        match="duplicate handler id",
    ):
        service.save_configuration(
            hooks=_duplicate_handler_ids(),
            expected_revision=service.get_configuration().revision,
            actor=_actor(),
        )


def test_save_rejects_duplicate_matcher_group_ids_across_events(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    hooks = {
        "enabled": True,
        "events": {
            "SessionStart": [{"id": "duplicate-group", "hooks": []}],
            "Stop": [{"id": "duplicate-group", "hooks": []}],
        },
    }

    with pytest.raises(
        HookManagementValidationError,
        match="duplicate matcher group id",
    ):
        service.save_configuration(
            hooks=hooks,
            expected_revision=service.get_configuration().revision,
            actor=_actor(),
        )


def test_save_preserves_non_hook_agent_configuration(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    agent_path = workspace_dir / "agent.json"
    agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
    agent_data["language"] = "zh"
    agent_path.write_text(json.dumps(agent_data), encoding="utf-8")
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")

    service.save_configuration(
        hooks={"enabled": True, "events": {}},
        expected_revision=service.get_configuration().revision,
        actor=_actor(),
    )

    saved = json.loads(agent_path.read_text(encoding="utf-8"))
    assert saved["language"] == "zh"
    assert saved["hooks"] == {"enabled": True, "events": {}}


def test_save_audits_removed_matcher_group_and_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    agent_path = workspace_dir / "agent.json"
    agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
    agent_data["hooks"] = {
        "enabled": True,
        "events": {
            "PreToolUse": [
                {
                    "id": "old-group",
                    "hooks": [
                        {
                            "id": "old-handler",
                            "type": "command",
                            "argv": ["echo"],
                        },
                    ],
                },
            ],
        },
    }
    agent_path.write_text(json.dumps(agent_data), encoding="utf-8")
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    before = service.get_configuration().hooks
    assert HookManagementService._removed_hook_ids(
        before,
        {"enabled": False, "events": {}},
    ) == (["old-group"], ["old-handler"])
    audit_log = Mock()
    monkeypatch.setattr(hook_management.logger, "info", audit_log)

    service.save_configuration(
        hooks={"enabled": False, "events": {}},
        expected_revision=service.get_configuration().revision,
        actor=_actor(),
    )

    removal = audit_log.call_args_list[-1].kwargs["extra"]
    assert removal["event"] == "configuration_removed"
    assert removal["removed_group_ids"] == '["old-group"]'
    assert removal["removed_handler_ids"] == '["old-handler"]'


def test_save_rejects_path_like_argv_zero_outside_script_library(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    hooks = {
        "enabled": True,
        "events": {
            "PreToolUse": [
                {
                    "id": "group",
                    "hooks": [
                        {
                            "id": "command",
                            "type": "command",
                            "argv": ["../../outside.sh"],
                        },
                    ],
                },
            ],
        },
    }

    with pytest.raises(HookManagementValidationError, match="hooks/scripts"):
        service.save_configuration(
            hooks=hooks,
            expected_revision=service.get_configuration().revision,
            actor=_actor(),
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["echo", "hello"],
        ["python", "-m", "package.module"],
        ["curl", "https://hooks.example.test/policy"],
    ],
)
def test_save_preserves_free_non_script_argv_arguments(
    argv: list[str],
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    hooks = {
        "enabled": True,
        "events": {
            "PreToolUse": [
                {
                    "id": "group",
                    "hooks": [
                        {"id": "command", "type": "command", "argv": argv},
                    ],
                },
            ],
        },
    }

    saved = service.save_configuration(
        hooks=hooks,
        expected_revision=service.get_configuration().revision,
        actor=_actor(),
    )

    assert saved.hooks["events"]["PreToolUse"][0]["hooks"][0]["argv"] == argv


def test_save_rejects_blank_group_or_handler_id(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    hooks = {
        "enabled": True,
        "events": {
            "PreToolUse": [
                {
                    "id": "",
                    "hooks": [{"id": "", "type": "command", "argv": ["echo"]}],
                },
            ],
        },
    }

    with pytest.raises(
        HookManagementValidationError,
        match="must not be blank",
    ):
        service.save_configuration(
            hooks=hooks,
            expected_revision=service.get_configuration().revision,
            actor=_actor(),
        )


def test_batch_keeps_valid_file_when_another_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    monkeypatch.setattr(
        hook_management,
        "scan_skill_directory",
        lambda *args, **kwargs: None,
    )
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")

    result = service.upload_scripts(
        files=[
            UploadFilePayload("guard.py", b"print('ok')"),
            UploadFilePayload("bad.exe", b"MZ"),
        ],
        overwrite_names=set(),
        actor=_actor(),
    )

    assert result.accepted_names == ["guard.py"]
    assert result.failed[0].filename == "bad.exe"
    assert (workspace_dir / "hooks" / "scripts" / "guard.py").read_bytes() == (
        b"print('ok')"
    )


def test_upload_rejects_binary_script_and_symlinked_library(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    monkeypatch.setattr(
        hook_management,
        "scan_skill_directory",
        lambda *args, **kwargs: None,
    )
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")

    binary = service.upload_scripts(
        files=[UploadFilePayload("binary.py", b"\x00not-a-script")],
        overwrite_names=set(),
        actor=_actor(),
    )
    assert binary.failed[0].reason == "script content must be UTF-8 text"

    external = tmp_path / "external"
    external.mkdir()
    hooks_dir = workspace_dir / "hooks"
    (hooks_dir / "scripts").rmdir()
    (hooks_dir / "scripts").symlink_to(external, target_is_directory=True)
    with pytest.raises(HookManagementValidationError, match="symbolic link"):
        service.upload_scripts(
            files=[UploadFilePayload("guard.py", b"print('ok')")],
            overwrite_names=set(),
            actor=_actor(),
        )


def test_save_rejects_symlinked_referenced_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    monkeypatch.setattr(
        hook_management,
        "scan_skill_directory",
        lambda *args, **kwargs: None,
    )
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    service.upload_scripts(
        files=[UploadFilePayload("guard.py", b"print('ok')")],
        overwrite_names=set(),
        actor=_actor(),
    )
    script_path = workspace_dir / "hooks" / "scripts" / "guard.py"
    external_script = tmp_path / "external.py"
    external_script.write_text("print('outside')", encoding="utf-8")
    script_path.unlink()
    script_path.symlink_to(external_script)

    hooks = {
        "enabled": True,
        "events": {
            "PreToolUse": [
                {
                    "id": "group",
                    "hooks": [
                        {
                            "id": "command",
                            "type": "command",
                            "argv": ["python", "hooks/scripts/guard.py"],
                        },
                    ],
                },
            ],
        },
    }
    with pytest.raises(HookManagementValidationError, match="symbolic link"):
        service.save_configuration(
            hooks=hooks,
            expected_revision=service.get_configuration().revision,
            actor=_actor(),
        )


def test_uploaded_shell_script_is_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    monkeypatch.setattr(
        hook_management,
        "scan_skill_directory",
        lambda *args, **kwargs: None,
    )
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    service.upload_scripts(
        files=[UploadFilePayload("direct.sh", b"#!/bin/sh\necho '{}'\n")],
        overwrite_names=set(),
        actor=_actor(),
    )

    assert (
        workspace_dir / "hooks" / "scripts" / "direct.sh"
    ).stat().st_mode & 0o100


def test_list_scripts_returns_controlled_library_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    monkeypatch.setattr(
        hook_management,
        "scan_skill_directory",
        lambda *args, **kwargs: None,
    )
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    service.upload_scripts(
        files=[UploadFilePayload("guard.py", b"print('ok')")],
        overwrite_names=set(),
        actor=_actor(),
    )

    scripts = service.list_scripts()

    assert scripts[0]["filename"] == "guard.py"
    assert scripts[0]["size"] == len(b"print('ok')")
    assert len(scripts[0]["sha256"]) == 64


@pytest.mark.asyncio
async def test_manual_test_runs_one_draft_handler_without_persisting_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    original_config = (workspace_dir / "agent.json").read_text(
        encoding="utf-8",
    )
    execute = AsyncMock(
        return_value=HookHandlerResult(
            handler_id="check-command",
            order=0,
            reason="completed",
        ),
    )
    monkeypatch.setattr(hook_management, "execute_handler", execute)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")

    result = await service.manual_test(
        handler={"id": "check-command", "type": "command", "argv": ["echo"]},
        context=HookContext(
            session_id="test-session",
            transcript_path="",
            cwd=str(workspace_dir),
            hook_event_name="PreToolUse",
            tenant_id="tenant-a",
            effective_tenant_id="tenant-a",
            user_id="user-a",
            agent_id="default",
            channel="test",
            workspace_dir=str(workspace_dir),
        ),
        actor=_actor(),
        source_id="source-a",
    )

    execute.assert_awaited_once()
    assert execute.call_args.args[1].source_id == "source-a"
    assert result.redacted_summary["status"] == "completed"
    assert "reason" not in result.redacted_summary
    assert (workspace_dir / "agent.json").read_text(
        encoding="utf-8",
    ) == original_config


@pytest.mark.asyncio
async def test_manual_test_supports_one_stop_output_transformer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspaces" / "default"
    _write_default_agent(workspace_dir)
    execute = AsyncMock(
        return_value=HookHandlerResult(
            handler_id="format",
            order=0,
            replacement_text="sample final",
        ),
    )
    monkeypatch.setattr(hook_management, "execute_handler", execute)
    service = HookManagementService(workspace_dir, tenant_id="tenant-a")
    context = HookContext(
        session_id="test-session",
        transcript_path="",
        cwd=str(workspace_dir),
        hook_event_name="Stop",
        tenant_id="tenant-a",
        effective_tenant_id="tenant-a",
        user_id="user-a",
        agent_id="default",
        channel="test",
        workspace_dir=str(workspace_dir),
        assistant_response="sample candidate",
    )

    result = await service.manual_test(
        handler={
            "id": "format",
            "type": "command",
            "argv": ["echo"],
            "outputTransform": True,
        },
        context=context,
        actor=_actor(),
    )

    assert execute.call_args.args[1].assistant_response == "sample candidate"
    assert result.redacted_summary["output_transform"] is True
    assert result.redacted_summary["replacement_applied"] is True
    assert result.redacted_summary["replacement_length"] == len("sample final")

    with pytest.raises(HookManagementValidationError, match="outputTransform"):
        await service.manual_test(
            handler={
                "id": "invalid-format",
                "type": "command",
                "argv": ["echo"],
                "outputTransform": True,
            },
            context=context.model_copy(
                update={"hook_event_name": "PreToolUse"},
            ),
            actor=_actor(),
        )


@pytest.mark.asyncio
async def test_distribution_merges_selected_groups_and_copies_scripts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        hook_management,
        "scan_skill_directory",
        lambda *args, **kwargs: None,
    )
    source_workspace = tmp_path / "source" / "workspaces" / "default"
    target_workspace = tmp_path / "target" / "workspaces" / "default"
    _write_default_agent(source_workspace)
    _write_default_agent(target_workspace)
    source = HookManagementService(source_workspace, tenant_id="source")
    target = HookManagementService(target_workspace, tenant_id="target")
    source.upload_scripts(
        files=[UploadFilePayload("guard.py", b"print('source')")],
        overwrite_names=set(),
        actor=_actor(),
    )
    source.save_configuration(
        hooks={
            "enabled": True,
            "events": {
                "PreToolUse": [
                    {
                        "id": "replace-group",
                        "matcher": {"tools": ["shell"]},
                        "hooks": [
                            {
                                "id": "source-handler",
                                "type": "command",
                                "argv": ["python", "hooks/scripts/guard.py"],
                            },
                        ],
                    },
                    {
                        "id": "new-group",
                        "hooks": [
                            {
                                "id": "new-handler",
                                "type": "command",
                                "argv": ["echo", "new"],
                            },
                        ],
                    },
                ],
            },
        },
        expected_revision=source.get_configuration().revision,
        actor=_actor(),
    )
    target.save_configuration(
        hooks={
            "enabled": False,
            "events": {
                "PreToolUse": [
                    {
                        "id": "replace-group",
                        "matcher": {"tools": ["old"]},
                        "hooks": [
                            {
                                "id": "old-handler",
                                "type": "command",
                                "argv": ["echo", "old"],
                            },
                        ],
                    },
                    {
                        "id": "preserved-group",
                        "hooks": [
                            {
                                "id": "preserved-handler",
                                "type": "command",
                                "argv": ["echo", "preserved"],
                            },
                        ],
                    },
                ],
            },
        },
        expected_revision=target.get_configuration().revision,
        actor=_actor(),
    )
    activate = AsyncMock()

    result = await source.distribute_to_target(
        target=target,
        matcher_group_ids=["replace-group", "new-group"],
        actor=_actor(),
        activate=activate,
    )

    target_hooks = target.get_configuration().hooks
    groups = target_hooks["events"]["PreToolUse"]
    assert target_hooks["enabled"] is True
    assert [group["id"] for group in groups] == [
        "replace-group",
        "preserved-group",
        "new-group",
    ]
    assert groups[0]["matcher"] == {"tools": ["shell"]}
    assert groups[0]["hooks"][0]["id"] == "source-handler"
    assert (
        target_workspace / "hooks" / "scripts" / "guard.py"
    ).read_bytes() == (b"print('source')")
    assert result.matcher_group_ids == ("replace-group", "new-group")
    activate.assert_awaited_once()


@pytest.mark.asyncio
async def test_distribution_rejects_conflicting_script_used_by_preserved_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        hook_management,
        "scan_skill_directory",
        lambda *args, **kwargs: None,
    )
    source_workspace = tmp_path / "source" / "workspaces" / "default"
    target_workspace = tmp_path / "target" / "workspaces" / "default"
    _write_default_agent(source_workspace)
    _write_default_agent(target_workspace)
    source = HookManagementService(source_workspace, tenant_id="source")
    target = HookManagementService(target_workspace, tenant_id="target")
    source.upload_scripts(
        files=[UploadFilePayload("shared.py", b"print('source')")],
        overwrite_names=set(),
        actor=_actor(),
    )
    source.save_configuration(
        hooks={
            "enabled": True,
            "events": {
                "PreToolUse": [
                    {
                        "id": "source-group",
                        "hooks": [
                            {
                                "id": "source-handler",
                                "type": "command",
                                "argv": ["python", "hooks/scripts/shared.py"],
                            },
                        ],
                    },
                ],
            },
        },
        expected_revision=source.get_configuration().revision,
        actor=_actor(),
    )
    target.upload_scripts(
        files=[UploadFilePayload("shared.py", b"print('target')")],
        overwrite_names=set(),
        actor=_actor(),
    )
    target.save_configuration(
        hooks={
            "enabled": True,
            "events": {
                "PreToolUse": [
                    {
                        "id": "preserved-group",
                        "hooks": [
                            {
                                "id": "preserved-handler",
                                "type": "command",
                                "argv": ["python", "hooks/scripts/shared.py"],
                            },
                        ],
                    },
                ],
            },
        },
        expected_revision=target.get_configuration().revision,
        actor=_actor(),
    )
    activate = AsyncMock()

    with pytest.raises(HookManagementConflict, match="conflicting script"):
        await source.distribute_to_target(
            target=target,
            matcher_group_ids=["source-group"],
            actor=_actor(),
            activate=activate,
        )

    assert target.get_configuration().hooks["events"]["PreToolUse"][0][
        "id"
    ] == ("preserved-group")
    assert (
        target_workspace / "hooks" / "scripts" / "shared.py"
    ).read_bytes() == (b"print('target')")
    activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_distribution_does_not_create_script_library_without_scripts(
    tmp_path: Path,
) -> None:
    source_workspace = tmp_path / "source" / "workspaces" / "default"
    target_workspace = tmp_path / "target" / "workspaces" / "default"
    _write_default_agent(source_workspace)
    _write_default_agent(target_workspace)
    source = HookManagementService(source_workspace, tenant_id="source")
    target = HookManagementService(target_workspace, tenant_id="target")
    source.save_configuration(
        hooks={
            "enabled": True,
            "events": {
                "PreToolUse": [
                    {
                        "id": "source-group",
                        "hooks": [
                            {
                                "id": "source-handler",
                                "type": "command",
                                "argv": ["echo", "source"],
                            },
                        ],
                    },
                ],
            },
        },
        expected_revision=source.get_configuration().revision,
        actor=_actor(),
    )

    await source.distribute_to_target(
        target=target,
        matcher_group_ids=["source-group"],
        actor=_actor(),
        activate=AsyncMock(),
    )

    assert not (target_workspace / "hooks").exists()
