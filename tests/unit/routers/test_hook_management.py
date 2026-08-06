# -*- coding: utf-8 -*-
"""HTTP contract tests for Default Agent Profile Hook management."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.app.hook_management import (
    HookConfigurationSnapshot,
    HookScriptDiagnostic,
    HookScriptUploadResult,
)
from swe.app.routers import hook_management
from swe.config.context import encode_scope_id


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


def test_get_configuration_returns_script_diagnostics(monkeypatch) -> None:
    client, service, _ = _client(monkeypatch)
    service.snapshot = HookConfigurationSnapshot(
        hooks={"enabled": True, "events": {}},
        revision="revision-1",
        diagnostics=(
            HookScriptDiagnostic(
                event="PreToolUse",
                group_id="group",
                handler_id="missing-script",
                argument="hooks/scripts/missing.py",
                reason="script is not in the controlled library: missing.py",
            ),
        ),
    )

    response = client.get("/hook-management/configuration")

    assert response.status_code == 200
    assert response.json()["diagnostics"] == [
        {
            "event": "PreToolUse",
            "group_id": "group",
            "handler_id": "missing-script",
            "argument": "hooks/scripts/missing.py",
            "reason": "script is not in the controlled library: missing.py",
        },
    ]


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
