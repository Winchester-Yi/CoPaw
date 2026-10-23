# -*- coding: utf-8 -*-
"""HTTP contract tests for Default Agent Profile Hook management."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.agents.hook_runtime.models import HookConfig, HookContext
from swe.agents.hook_runtime.resolver import HookResolver
from swe.app.hook_management import (
    HookConfigurationSnapshot,
    HookManagementService,
    HookScriptUploadResult,
)
from swe.app.routers import hook_management
from swe.config.context import encode_scope_id


@pytest.mark.parametrize("assistant_response", ["approved answer", "  ", None])
def test_manual_append_real_service_returns_only_safe_summary(
    monkeypatch,
    tmp_path,
    assistant_response,
):
    client, _, reload = _client(monkeypatch)
    workspace = tmp_path / "default"
    scripts = workspace / "hooks" / "scripts"
    scripts.mkdir(parents=True)
    agent_path = workspace / "agent.json"
    agent_path.write_text('{"id":"default"}', encoding="utf-8")
    (scripts / "append.py").write_text(
        "import json\n"
        "print(json.dumps({'decision': 'allow', 'reason': 'private reason', "
        "'hookSpecificOutput': {'appendText': '附加🙂'}}))\n",
        encoding="utf-8",
    )
    service = HookManagementService(workspace, tenant_id="tenant-a")
    monkeypatch.setattr(
        hook_management,
        "_service_for_request",
        lambda _: service,
    )
    before = agent_path.read_bytes()

    response = client.post(
        "/hook-management/manual-test",
        json={
            "confirmRealExecution": True,
            "handler": {
                "id": "append",
                "type": "command",
                "argv": ["python", "hooks/scripts/append.py"],
                "outputAppend": True,
                "failPolicy": "block",
            },
            "context": {
                "session_id": "manual-only",
                "transcript_path": "",
                "cwd": str(workspace),
                "hook_event_name": "Stop",
                "tenant_id": "tenant-a",
                "effective_tenant_id": "tenant-a",
                "user_id": "user-a",
                "agent_id": "default",
                "channel": "test",
                "assistant_response": assistant_response,
            },
        },
    )

    if assistant_response == "approved answer":
        assert response.status_code == 200
        assert set(response.json()) == {"redacted_summary"}
        summary = response.json()["redacted_summary"]
        assert summary["output_append"] is True
        assert summary["append_applied"] is True
        assert summary["append_bytes"] == 10
        assert summary["effective_fail_policy"] == "allow"
        assert isinstance(summary["duration_ms"], int)
        assert "附加" not in response.text
        assert "private reason" not in response.text
    else:
        assert response.status_code == 422
        assert "assistant_response" in response.json()["detail"]
    assert agent_path.read_bytes() == before
    assert not (workspace / "sessions").exists()
    assert not (workspace / "memory").exists()
    reload.assert_not_called()


class _FakeService:
    def __init__(self) -> None:
        self.snapshot = HookConfigurationSnapshot(
            hooks={"enabled": False, "events": {}},
            revision="revision-1",
        )
        self.saved: dict | None = None
        self.overwrite_names: set[str] | None = None
        self.manual_test_source_id: str | None = None

    def get_configuration(self) -> HookConfigurationSnapshot:
        return self.snapshot

    def upload_scripts(self, *, files, overwrite_names, actor):
        self.overwrite_names = overwrite_names
        return HookScriptUploadResult(accepted=(files[0].filename,))

    async def manual_test(self, *, handler, context, actor, source_id):
        self.manual_test_source_id = source_id
        return SimpleNamespace(
            redacted_summary={"status": "completed", "failed": False},
        )

    def save_configuration(self, *, hooks, expected_revision, actor):
        self.saved = {
            "hooks": hooks,
            "expected_revision": expected_revision,
            "actor": actor,
        }
        self.snapshot = HookConfigurationSnapshot(
            hooks=hooks,
            revision="revision-2",
        )
        return self.snapshot


def _client(monkeypatch) -> tuple[TestClient, _FakeService, Mock]:
    service = _FakeService()
    reload = Mock()
    monkeypatch.setattr(
        hook_management,
        "_service_for_request",
        lambda request: service,
    )
    monkeypatch.setattr(hook_management, "schedule_agent_reload", reload)
    app = FastAPI()

    @app.middleware("http")
    async def _request_identity(request: Request, call_next):
        request.state.tenant_id = "tenant-a"
        request.state.source_id = "source-a"
        request.state.scope_id = encode_scope_id("tenant-a", "source-a")
        request.state.user_id = "user-a"
        return await call_next(request)

    app.include_router(hook_management.router)
    return TestClient(app), service, reload


def test_put_configuration_reloads_default_agent_after_save(
    monkeypatch,
) -> None:
    client, service, reload = _client(monkeypatch)

    response = client.put(
        "/hook-management/configuration",
        headers={"If-Match": "revision-1"},
        json={"hooks": {"enabled": True, "events": {}}},
    )

    assert response.status_code == 200
    assert service.saved is not None
    assert reload.call_args.args[1] == "default"
    assert reload.call_args.kwargs["tenant_id"] == encode_scope_id(
        "tenant-a",
        "source-a",
    )


def test_manual_test_requires_real_execution_confirmation(monkeypatch) -> None:
    client, _, _ = _client(monkeypatch)

    response = client.post(
        "/hook-management/manual-test",
        json={
            "confirm_real_execution": False,
            "handler": {"id": "command", "type": "command", "argv": ["echo"]},
            "context": {
                "session_id": "test",
                "transcript_path": "",
                "cwd": ".",
                "hook_event_name": "PreToolUse",
                "tenant_id": "tenant-a",
                "effective_tenant_id": "tenant-a",
                "user_id": "user-a",
                "agent_id": "default",
                "channel": "test",
            },
        },
    )

    assert response.status_code == 400


def test_manual_test_returns_only_redacted_summary(monkeypatch) -> None:
    client, service, _ = _client(monkeypatch)

    response = client.post(
        "/hook-management/manual-test",
        json={
            "confirmRealExecution": True,
            "handler": {"id": "command", "type": "command", "argv": ["echo"]},
            "context": {
                "session_id": "test",
                "transcript_path": "",
                "cwd": ".",
                "hook_event_name": "PreToolUse",
                "tenant_id": "tenant-a",
                "effective_tenant_id": "tenant-a",
                "user_id": "user-a",
                "agent_id": "default",
                "channel": "test",
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "redacted_summary": {"status": "completed", "failed": False},
    }
    assert service.manual_test_source_id == "source-a"


def test_script_upload_reads_overwrite_names_from_multipart_form(
    monkeypatch,
) -> None:
    client, service, _ = _client(monkeypatch)

    response = client.post(
        "/hook-management/scripts",
        data={"overwrite": json.dumps(["guard.py"])},
        files=[("files", ("guard.py", b"print('ok')", "text/x-python"))],
    )

    assert response.status_code == 200
    assert service.overwrite_names == {"guard.py"}


def test_distribution_returns_per_target_result_and_reloads_target_agent(
    monkeypatch,
) -> None:
    client, _, _ = _client(monkeypatch)
    target_service = object()
    source = Mock()
    source.prepare_distribution.return_value = SimpleNamespace(
        revision="rev-1",
    )
    source.distribute_payload_to_target = AsyncMock(
        return_value=SimpleNamespace(
            matcher_group_ids=("tool-guards",),
            script_names=("guard.py",),
        ),
    )
    reload_agent = AsyncMock()
    manager = SimpleNamespace(reload_agent=reload_agent)
    monkeypatch.setattr(
        hook_management,
        "_service_for_request",
        lambda request: source,
    )
    monkeypatch.setattr(
        hook_management,
        "_ensure_target_hook_service",
        AsyncMock(return_value=(target_service, "target-a", True)),
    )
    monkeypatch.setattr(
        hook_management,
        "_get_multi_agent_manager",
        lambda request: manager,
    )

    response = client.post(
        "/hook-management/distribute/default-agents",
        json={
            "matcherGroupIds": ["tool-guards"],
            "targetTenantIds": ["target-a"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "source_revision": "rev-1",
        "results": [
            {
                "tenant_id": "target-a",
                "success": True,
                "bootstrapped": True,
                "matcher_group_ids": ["tool-guards"],
                "script_names": ["guard.py"],
                "error": "",
            },
        ],
    }
    source.distribute_payload_to_target.assert_awaited_once()
    activate = source.distribute_payload_to_target.call_args.kwargs["activate"]
    import asyncio

    asyncio.run(activate())
    reload_agent.assert_awaited_once_with("default", tenant_id="target-a")


def test_distribution_rejects_duplicate_or_scoped_target_ids(
    monkeypatch,
) -> None:
    client, _, _ = _client(monkeypatch)

    duplicate_response = client.post(
        "/hook-management/distribute/default-agents",
        json={
            "matcherGroupIds": ["tool-guards"],
            "targetTenantIds": ["target-a", "target-a"],
        },
    )
    scoped_response = client.post(
        "/hook-management/distribute/default-agents",
        json={
            "matcherGroupIds": ["tool-guards"],
            "targetTenantIds": [encode_scope_id("target-b", "source-b")],
        },
    )

    assert duplicate_response.status_code == 422
    assert scoped_response.status_code == 422


def test_distribution_preserves_bootstrap_result_when_target_apply_fails(
    monkeypatch,
) -> None:
    client, _, _ = _client(monkeypatch)
    source = Mock()
    source.prepare_distribution.return_value = SimpleNamespace(
        revision="rev-1",
    )
    source.distribute_payload_to_target = AsyncMock(
        side_effect=RuntimeError("target reload failed"),
    )
    monkeypatch.setattr(
        hook_management,
        "_service_for_request",
        lambda request: source,
    )
    monkeypatch.setattr(
        hook_management,
        "_ensure_target_hook_service",
        AsyncMock(return_value=(object(), "target-a", True)),
    )
    monkeypatch.setattr(
        hook_management,
        "_get_multi_agent_manager",
        lambda request: SimpleNamespace(reload_agent=AsyncMock()),
    )

    response = client.post(
        "/hook-management/distribute/default-agents",
        json={
            "matcherGroupIds": ["tool-guards"],
            "targetTenantIds": ["target-a"],
        },
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["bootstrapped"] is True


def test_distribution_audits_target_bootstrap_failure(monkeypatch) -> None:
    client, _, _ = _client(monkeypatch)
    source = Mock()
    source.prepare_distribution.return_value = SimpleNamespace(
        revision="rev-1",
    )
    monkeypatch.setattr(
        hook_management,
        "_service_for_request",
        lambda request: source,
    )
    monkeypatch.setattr(
        hook_management,
        "_ensure_target_hook_service",
        AsyncMock(side_effect=RuntimeError("bootstrap failed")),
    )
    monkeypatch.setattr(
        hook_management,
        "_get_multi_agent_manager",
        lambda request: SimpleNamespace(reload_agent=AsyncMock()),
    )

    response = client.post(
        "/hook-management/distribute/default-agents",
        json={
            "matcherGroupIds": ["tool-guards"],
            "targetTenantIds": ["target-a"],
        },
    )

    assert response.status_code == 200
    source.emit_distribution_failure.assert_called_once()
    assert response.json()["results"][0]["success"] is False


def _create_distribution_services(
    tmp_path: Path,
    source_enabled: bool,
    target_enabled: bool | None,
):
    group = {
        "id": "distributed-group",
        "hooks": [
            {
                "id": "distributed-handler",
                "type": "command",
                "argv": ["python", "hooks/scripts/guard.py"],
            },
        ],
    }
    preserved_group = {
        "id": "preserved-group",
        "hooks": [
            {
                "id": "preserved-handler",
                "type": "command",
                "argv": ["echo", "preserved"],
            },
        ],
    }
    services = {}
    config_paths = {}
    for tenant in ("tenant-a", "tenant-b"):
        workspace = tmp_path / tenant
        workspace.mkdir()
        config = {"id": "default", "name": "Default Agent"}
        if tenant == "tenant-a":
            config["hooks"] = {
                "enabled": source_enabled,
                "events": {"PreToolUse": [group]},
            }
        elif target_enabled is not None:
            config["hooks"] = {
                "enabled": target_enabled,
                "events": {"PreToolUse": [preserved_group]},
            }
        config_paths[tenant] = workspace / "agent.json"
        config_paths[tenant].write_text(json.dumps(config), encoding="utf-8")
        services[tenant] = HookManagementService(
            workspace,
            tenant_id=encode_scope_id(tenant, "source-a"),
        )
        scripts = workspace / "hooks" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "guard.py").write_bytes(
            b"print('source')" if tenant == "tenant-a" else b"print('old')",
        )
    return services, config_paths


@pytest.mark.parametrize(
    ("source_enabled", "target_enabled", "reload_fails"),
    [
        (True, False, False),
        (True, None, False),
        (True, True, False),
        (False, False, False),
        (True, False, True),
    ],
)
def test_distribution_enables_target_hooks_transactionally(
    monkeypatch,
    tmp_path: Path,
    source_enabled: bool,
    target_enabled: bool | None,
    reload_fails: bool,
) -> None:
    client, _, _ = _client(monkeypatch)
    services, config_paths = _create_distribution_services(
        tmp_path,
        source_enabled,
        target_enabled,
    )

    source_before = config_paths["tenant-a"].read_bytes()
    target_before = config_paths["tenant-b"].read_bytes()
    source_service = services["tenant-a"]
    target_service = services["tenant-b"]
    effective_target = encode_scope_id("tenant-b", "source-a")
    reload_configs = []

    async def reload_agent(agent_id, tenant_id):
        assert agent_id == "default"
        assert tenant_id == effective_target
        current = target_service.get_configuration().hooks
        reload_configs.append(current)
        if reload_fails and len(reload_configs) == 1:
            raise RuntimeError("reload failed")
        # An idle Agent loads persisted config on its next request.
        return False

    client.app.state.multi_agent_manager = SimpleNamespace(
        reload_agent=reload_agent,
    )
    monkeypatch.setattr(
        hook_management,
        "_service_for_request",
        lambda request: source_service,
    )
    monkeypatch.setattr(
        hook_management,
        "_ensure_target_hook_service",
        AsyncMock(return_value=(target_service, effective_target, False)),
    )
    payload = {
        "matcherGroupIds": ["distributed-group"],
        "targetTenantIds": ["tenant-b"],
    }
    response = client.post(
        "/hook-management/distribute/default-agents",
        json=payload,
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["success"] is (not reload_fails)
    assert reload_configs[0]["enabled"] is True
    assert config_paths["tenant-a"].read_bytes() == source_before
    target_script = tmp_path / "tenant-b" / "hooks" / "scripts" / "guard.py"
    if reload_fails:
        assert result["error"] == "reload failed"
        assert config_paths["tenant-b"].read_bytes() == target_before
        assert reload_configs[1]["enabled"] is False
        assert target_script.read_bytes() == b"print('old')"
        return

    assert target_script.read_bytes() == b"print('source')"
    hooks = target_service.get_configuration().hooks
    context = HookContext(
        session_id="test",
        transcript_path="",
        cwd=str(tmp_path / "tenant-b"),
        hook_event_name="PreToolUse",
        tenant_id="tenant-b",
        effective_tenant_id=effective_target,
        user_id="user-a",
        agent_id="default",
        channel="console",
        tool_name="shell",
    )
    plan = HookResolver(
        agent_config=HookConfig.model_validate(hooks),
    ).resolve_event_plan(context)
    expected = ["distributed-handler"]
    if target_enabled is not None:
        expected.insert(0, "preserved-handler")
    assert [item.handler.id for item in plan.handlers] == expected
    saved = config_paths["tenant-b"].read_bytes()
    repeated = client.post(
        "/hook-management/distribute/default-agents",
        json=payload,
    )
    assert repeated.json()["results"][0]["success"] is True
    assert config_paths["tenant-b"].read_bytes() == saved
