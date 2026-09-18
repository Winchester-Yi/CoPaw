# -*- coding: utf-8 -*-
"""HTTP contract tests for W+ SOP generated artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from swe.app.wplus_sop import router as wplus_router
from swe.app.wplus_sop import service as service_module
from swe.app.wplus_sop.models import (
    CommandReceipt,
    ConfirmedStageSnapshot,
    CumulativePreview,
    FinalSopResult,
    OwnershipTuple,
    SessionProjection,
    SessionState,
    Stage,
    StageReport,
    StageStatus,
)
from swe.app.wplus_sop.runtime import get_wplus_safe_stream_trace_registry
from swe.app.wplus_sop.service import (
    WPlusOwningChatFinalizingError,
    WPlusSopService,
    store_path_for_workspace,
)
from swe.app.wplus_sop.store import WPlusSopStore


def _build_client(tmp_path, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(wplus_router.router)
    app.include_router(wplus_router.router, prefix="/api")

    @app.middleware("http")
    async def add_identity(request: Request, call_next):
        request.state.tenant_id = "tenant-1"
        request.state.source_id = "console"
        request.state.user_id = request.headers.get("X-Test-User", "user-1")
        return await call_next(request)

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent-1",
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        wplus_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )
    store = WPlusSopStore(store_path_for_workspace(tmp_path))
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    artifacts = []
    contents = {
        "sop_spec.json": json.dumps(
            {"name": "客户经营 SOP", "version": 1},
            ensure_ascii=False,
        ),
        "sop_render.md": "# 客户经营 SOP",
        "sop_render.html": "<h1>客户经营 SOP</h1>",
        "example_result.html": "<h1>脱敏示例</h1>",
    }
    artifact_ids = {
        "sop_spec.json": "sop_spec",
        "sop_render.md": "sop_render_md",
        "sop_render.html": "sop_render_html",
        "example_result.html": "example_result_html",
    }
    for name, content in contents.items():
        raw = content.encode("utf-8")
        (static_dir / name).write_bytes(raw)
        artifacts.append(
            {
                "artifact_id": artifact_ids[name],
                "name": name,
                "static_file_name": name,
                "static_url": f"http://files.local/static/tenant-1/agent-1/{name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "copied_by": "copy_file_to_static",
            },
        )
    stage_artifacts = []
    stage_contents = {
        "stage-1-r1-v1.json": '{"stage":"客户识别"}',
        "stage-1-r1-v1.md": "# 客户识别",
        "stage-1-r1-v1.html": "<h1>客户识别</h1>",
    }
    stage_artifact_ids = {
        "stage-1-r1-v1.json": ("stage_sop_json", "stage_sop.json"),
        "stage-1-r1-v1.md": ("stage_sop_md", "stage_sop.md"),
        "stage-1-r1-v1.html": ("stage_sop_html", "stage_sop.html"),
    }
    for name, content in stage_contents.items():
        raw = content.encode("utf-8")
        (static_dir / name).write_bytes(raw)
        artifact_id, logical_name = stage_artifact_ids[name]
        stage_artifacts.append(
            {
                "artifact_id": artifact_id,
                "name": logical_name,
                "static_file_name": name,
                "static_url": f"http://files.local/static/tenant-1/agent-1/{name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "copied_by": "copy_file_to_static",
            },
        )
    cumulative_artifacts = []
    cumulative_contents = {
        "cumulative-v1.json": '{"stages":["客户识别"]}',
        "cumulative-v1.md": "# 累计 SOP\n\n## 客户识别",
        "cumulative-v1.html": "<h1>累计 SOP</h1><h2>客户识别</h2>",
    }
    cumulative_artifact_ids = {
        "cumulative-v1.json": ("stage_sop_json", "stage_sop.json"),
        "cumulative-v1.md": ("stage_sop_md", "stage_sop.md"),
        "cumulative-v1.html": ("stage_sop_html", "stage_sop.html"),
    }
    for name, content in cumulative_contents.items():
        raw = content.encode("utf-8")
        (static_dir / name).write_bytes(raw)
        artifact_id, logical_name = cumulative_artifact_ids[name]
        cumulative_artifacts.append(
            {
                "artifact_id": artifact_id,
                "name": logical_name,
                "static_file_name": name,
                "static_url": f"http://files.local/static/tenant-1/agent-1/{name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "copied_by": "copy_file_to_static",
            },
        )
    stage_id = "stage/客户 1"
    stage_report = StageReport(
        stage_id=stage_id,
        report_no=1,
        revision=1,
        artifacts=stage_artifacts,
        validation={
            "schema_validator": "scripts/validate_stage_sop.py",
            "schema_exit_code": 0,
            "renderers": [
                "scripts/render_stage_md.py",
                "scripts/render_stage_sop.py",
            ],
        },
    )
    confirmed_snapshot = ConfirmedStageSnapshot(
        stage_id=stage_id,
        report_no=1,
        revision=1,
        artifact_sha256=stage_artifacts[0]["sha256"],
    )
    store.create_session(
        SessionProjection(
            sop_session_id="sop-1",
            ownership=OwnershipTuple(
                tenant_id="tenant-1",
                source_id="console",
                user_id="user-1",
                agent_id="agent-1",
                chat_id="chat-1",
                logical_chat_session_id="logical-1",
            ),
            skill_snapshot_id="sha256:miner-v1",
            state=SessionState.COMPLETED,
            state_version=20,
            title="客户经营 SOP",
            stages=[
                Stage(
                    stage_id=stage_id,
                    name="客户识别",
                    status=StageStatus.CONFIRMED,
                ),
            ],
            stage_reports=[stage_report],
            confirmed_snapshots=[confirmed_snapshot],
            cumulative_preview=CumulativePreview(
                preview_version=1,
                stage_order=[stage_id],
                snapshots=[confirmed_snapshot],
                artifacts=cumulative_artifacts,
            ),
            final_result=FinalSopResult(
                sop_spec={"name": "客户经营 SOP", "version": 1},
                readable_sop="# 客户经营 SOP",
                html="<h1>客户经营 SOP</h1>",
                example_result_html="<h1>脱敏示例</h1>",
                artifacts=artifacts,
                validation={
                    "schema_validator": "scripts/validate_sop.py",
                    "schema_exit_code": 0,
                    "renderers": [
                        "scripts/render_md.py",
                        "scripts/render_sop.py",
                    ],
                },
            ),
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-entry",
            command="confirm_entry",
            sop_session_id="sop-1",
            resulting_state_version=20,
        ),
    )
    return TestClient(app)


def test_completed_artifacts_have_authenticated_downloads(
    tmp_path,
    monkeypatch,
) -> None:
    client = _build_client(tmp_path, monkeypatch)

    projection = client.get("/api/wplus-sop/sessions/sop-1")
    snapshot = projection.json()
    spec_url = snapshot["artifacts"][0]["download_url"]
    downloaded = client.get(
        "/api/wplus-sop/sessions/sop-1/artifacts/sop_spec",
        follow_redirects=False,
    )

    assert projection.status_code == 200
    assert spec_url == (
        "/api/wplus-sop/sessions/sop-1/artifacts/sop_spec?download=true"
    )
    assert snapshot["result_preview"]["markdown_url"].endswith(
        "/artifacts/sop_render_md?download=true",
    )
    assert snapshot["stage_reports"][0]["artifacts"][0]["download_url"] == (
        "/api/wplus-sop/sessions/sop-1/stage-report-artifacts/"
        "stage_sop_json?stage_id=stage%2F%E5%AE%A2%E6%88%B7+1&revision=1"
        "&report_no=1&download=true"
    )
    assert snapshot["cumulative_preview"]["artifacts"][0][
        "download_url"
    ] == (
        "/api/wplus-sop/sessions/sop-1/cumulative-artifacts/"
        "stage_sop_json?preview_version=1&download=true"
    )
    assert "static_url" not in json.dumps(snapshot)
    assert downloaded.status_code == 200
    assert downloaded.text == '{"name": "客户经营 SOP", "version": 1}'
    assert downloaded.headers["content-type"].startswith(
        "text/plain; charset=utf-8",
    )
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["content-security-policy"] == (
        "default-src 'none'; sandbox"
    )


@pytest.mark.parametrize(
    ("artifact_id", "expected"),
    [
        ("stage_sop_json", '{"stage":"客户识别"}'),
        ("stage_sop_md", "# 客户识别"),
        ("stage_sop_html", "<h1>客户识别</h1>"),
    ],
)
def test_stage_report_artifact_reads_exact_authenticated_version(
    tmp_path,
    monkeypatch,
    artifact_id,
    expected,
) -> None:
    client = _build_client(tmp_path, monkeypatch)

    response = client.get(
        f"/api/wplus-sop/sessions/sop-1/stage-report-artifacts/{artifact_id}",
        params={"stage_id": "stage/客户 1", "revision": 1, "report_no": 1},
    )

    assert response.status_code == 200
    assert response.text == expected
    assert response.headers["content-type"].startswith(
        "text/plain; charset=utf-8",
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; sandbox"
    )


def test_cumulative_artifact_reads_exact_authenticated_version(
    tmp_path,
    monkeypatch,
) -> None:
    client = _build_client(tmp_path, monkeypatch)

    response = client.get(
        "/api/wplus-sop/sessions/sop-1/cumulative-artifacts/stage_sop_html",
        params={"preview_version": 1},
    )

    assert response.status_code == 200
    assert response.text == "<h1>累计 SOP</h1><h2>客户识别</h2>"
    assert response.headers["content-type"].startswith(
        "text/plain; charset=utf-8",
    )
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    ("url", "content_type", "filename"),
    [
        (
            "/api/wplus-sop/sessions/sop-1/artifacts/sop_spec?download=true",
            "application/json",
            "sop_spec.json",
        ),
        (
            "/api/wplus-sop/sessions/sop-1/stage-report-artifacts/"
            "stage_sop_md?stage_id=stage%2F%E5%AE%A2%E6%88%B7+1&revision=1"
            "&report_no=1&download=true",
            "text/markdown",
            "stage_sop.md",
        ),
        (
            "/api/wplus-sop/sessions/sop-1/cumulative-artifacts/"
            "stage_sop_html?preview_version=1&download=true",
            "text/html",
            "stage_sop.html",
        ),
    ],
)
def test_artifact_downloads_are_attachments_with_safe_headers(
    tmp_path,
    monkeypatch,
    url,
    content_type,
    filename,
) -> None:
    client = _build_client(tmp_path, monkeypatch)

    response = client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["content-disposition"] == (
        f"attachment; filename*=UTF-8''{filename}"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "url",
    [
        (
            "/wplus-sop/sessions/sop-1/stage-report-artifacts/"
            "stage_sop_md?stage_id=unknown&revision=1&report_no=1"
        ),
        (
            "/wplus-sop/sessions/sop-1/stage-report-artifacts/"
            "stage_sop_md?stage_id=stage%2F%E5%AE%A2%E6%88%B7+1"
            "&revision=1&report_no=99"
        ),
        (
            "/wplus-sop/sessions/sop-1/cumulative-artifacts/"
            "stage_sop_md?preview_version=99"
        ),
        "/wplus-sop/sessions/sop-1/artifacts/unknown",
    ],
)
def test_artifact_reads_fail_closed_for_unknown_identity(
    tmp_path,
    monkeypatch,
    url,
) -> None:
    client = _build_client(tmp_path, monkeypatch)

    response = client.get(url)

    assert response.status_code == 404
    assert response.json() == {"detail": "W+ SOP artifact not found"}


@pytest.mark.parametrize(
    "failure",
    ["missing", "hash_mismatch", "escaped_symlink"],
)
def test_artifact_reads_fail_closed_for_file_integrity(
    tmp_path,
    monkeypatch,
    failure,
) -> None:
    client = _build_client(tmp_path, monkeypatch)
    target = tmp_path / "static" / "stage-1-r1-v1.md"
    if failure == "missing":
        target.unlink()
    elif failure == "escaped_symlink":
        outside = tmp_path / "outside.md"
        outside.write_text("# escaped", encoding="utf-8")
        target.unlink()
        try:
            target.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"OS denied symlink creation: {exc}")
    else:
        target.write_text("tampered", encoding="utf-8")

    response = client.get(
        "/wplus-sop/sessions/sop-1/stage-report-artifacts/stage_sop_md",
        params={"stage_id": "stage/客户 1", "revision": 1, "report_no": 1},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "W+ SOP artifact not found"}


def test_artifact_containment_rejects_parent_path_without_symlinks(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "static"
    static_root.mkdir()
    outside = tmp_path / "outside.md"
    raw = b"# outside"
    outside.write_bytes(raw)
    artifact = SimpleNamespace(
        static_file_name="../outside.md",
        sha256=hashlib.sha256(raw).hexdigest(),
    )

    with pytest.raises(HTTPException) as exc_info:
        wplus_router._verify_artifact_file(
            static_root,
            artifact,
            read_content=False,
        )

    assert exc_info.value.status_code == 404


def test_artifact_preview_rejects_content_over_five_mib(
    tmp_path,
    monkeypatch,
) -> None:
    client = _build_client(tmp_path, monkeypatch)
    monkeypatch.setattr(wplus_router, "MAX_ARTIFACT_PREVIEW_BYTES", 1)

    preview = client.get(
        "/api/wplus-sop/sessions/sop-1/stage-report-artifacts/stage_sop_md",
        params={"stage_id": "stage/客户 1", "revision": 1, "report_no": 1},
    )
    download = client.get(
        "/api/wplus-sop/sessions/sop-1/stage-report-artifacts/stage_sop_md",
        params={
            "stage_id": "stage/客户 1",
            "revision": 1,
            "report_no": 1,
            "download": "true",
        },
    )

    assert wplus_router.DEFAULT_MAX_ARTIFACT_PREVIEW_BYTES == 5 * 1024 * 1024
    assert preview.status_code == 413
    assert preview.json() == {
        "detail": "W+ SOP artifact preview exceeds 5 MiB limit",
    }
    assert download.status_code == 200


def test_artifact_file_io_runs_off_the_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    original_to_thread = wplus_router.asyncio.to_thread

    async def tracked_to_thread(func, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(wplus_router.asyncio, "to_thread", tracked_to_thread)
    client = _build_client(tmp_path, monkeypatch)

    response = client.get(
        "/api/wplus-sop/sessions/sop-1/artifacts/sop_spec",
    )

    assert response.status_code == 200
    assert "_verify_artifact_file" in calls


def test_stage_artifact_read_is_fail_closed_for_another_user(
    tmp_path,
    monkeypatch,
) -> None:
    client = _build_client(tmp_path, monkeypatch)

    response = client.get(
        "/wplus-sop/sessions/sop-1/stage-report-artifacts/stage_sop_html",
        params={"stage_id": "stage/客户 1", "revision": 1, "report_no": 1},
        headers={"X-Test-User": "attacker"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "W+ SOP Session not found"}


def test_artifact_download_is_fail_closed_for_another_user(
    tmp_path,
    monkeypatch,
) -> None:
    client = _build_client(tmp_path, monkeypatch)

    response = client.get(
        "/wplus-sop/sessions/sop-1/artifacts/sop_render_html",
        headers={"X-Test-User": "attacker"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "W+ SOP Session not found"}


def test_first_confirm_accepts_source_id_distinct_from_chat_channel(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(wplus_router.router)

    @app.middleware("http")
    async def add_identity(request: Request, call_next):
        request.state.tenant_id = "tenant-1"
        request.state.source_id = "external-source-1"
        request.state.user_id = "user-1"
        return await call_next(request)

    chat = SimpleNamespace(
        id="chat-1",
        session_id="logical-1",
        user_id="user-1",
        channel="console",
        meta={},
    )

    class FakeChatManager:
        async def get_chat(self, chat_id):
            return chat if chat_id == chat.id else None

        async def update_chat(self, updated):
            return updated

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent-1",
        chat_manager=FakeChatManager(),
    )
    ownership = OwnershipTuple(
        tenant_id="tenant-1",
        source_id="external-source-1",
        user_id="user-1",
        agent_id="agent-1",
        chat_id=chat.id,
        logical_chat_session_id=chat.session_id,
    )
    service = WPlusSopService(workspace=workspace, ownership=ownership)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(
        wplus_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )
    monkeypatch.setattr(
        wplus_router,
        "_skill_snapshot_id",
        lambda _workspace_dir: "sha256:miner",
    )
    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)

    response = TestClient(app).post(
        f"/wplus-sop/entry-proposals/{proposal.proposal_id}/confirm",
        json={"command_request_id": "cmd-first-confirm"},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    active = service.get_active_session()
    assert active is not None
    assert active.projection.ownership.source_id == "external-source-1"


@pytest.mark.asyncio
async def test_session_get_checks_for_orphaned_generation_before_read(
    monkeypatch,
) -> None:
    calls: list[str] = []
    record = SimpleNamespace(
        projection=SimpleNamespace(sop_session_id="sop-1"),
    )

    class FakeService:
        async def recover_orphaned_generation_run(self, session_id):
            calls.append(f"recover:{session_id}")

        async def flush_chat_projection_outbox(self):
            calls.append("flush")

        def get_session(self, session_id):
            calls.append(f"get:{session_id}")
            return record

        async def get_runtime_status(self, session_id):
            calls.append(f"runtime:{session_id}")
            return {
                "status": "finalizing",
                "runtime_ready": False,
                "blocking_run_id": "run-1",
            }

    async def fake_service_for_session(_request, _session_id):
        return FakeService()

    monkeypatch.setattr(
        wplus_router,
        "_service_for_session",
        fake_service_for_session,
    )
    monkeypatch.setattr(
        wplus_router,
        "serialize_session",
        lambda _record: {"state": "RecoverableFailure"},
    )

    response = await wplus_router.get_wplus_sop_session(
        "sop-orphan",
        SimpleNamespace(),
    )

    assert response == {
        "state": "RecoverableFailure",
        "runtime_status": {
            "status": "finalizing",
            "runtime_ready": False,
            "blocking_run_id": "run-1",
        },
    }
    assert calls == [
        "recover:sop-orphan",
        "flush",
        "get:sop-orphan",
        "runtime:sop-orphan",
    ]


def test_owning_chat_finalizing_maps_to_machine_readable_409() -> None:
    with pytest.raises(HTTPException) as raised:
        wplus_router._raise_http(
            WPlusOwningChatFinalizingError(retry_after_ms=750),
        )

    response = raised.value
    assert response.status_code == 409
    assert response.detail == {
        "code": "owning_chat_finalizing",
        "message": "The prior owning Chat Agent run is still finalizing",
        "retry_after_ms": 750,
    }


@pytest.mark.asyncio
async def test_confirm_route_does_not_repeat_service_owned_entry_projection(
    monkeypatch,
) -> None:
    calls: list[str] = []
    record = SimpleNamespace(
        projection=SimpleNamespace(sop_session_id="sop-1"),
    )
    receipt = SimpleNamespace(run_id="run-1", attempt_id="attempt-1")

    class FakeService:
        workspace = SimpleNamespace(workspace_dir="unused")

        async def confirm_entry(self, **kwargs):
            calls.append(f"confirm:{kwargs['proposal_id']}")
            return SimpleNamespace(record=record, receipt=receipt)

        async def flush_chat_projection_outbox(self):
            calls.append("flush")

        async def get_runtime_status(self, _session_id):
            return {
                "status": "running",
                "runtime_ready": False,
                "blocking_run_id": "run-1",
            }

    async def fake_service_for_proposal(_request, _proposal_id):
        return FakeService()

    monkeypatch.setattr(
        wplus_router,
        "_service_for_proposal",
        fake_service_for_proposal,
    )
    monkeypatch.setattr(
        wplus_router,
        "_skill_snapshot_id",
        lambda _workspace_dir: "sha256:miner",
    )
    monkeypatch.setattr(
        wplus_router,
        "serialize_session",
        lambda _record: {"session_id": "sop-1"},
    )

    response = await wplus_router.confirm_wplus_sop_entry(
        "proposal-1",
        SimpleNamespace(command_request_id="cmd-entry"),
        SimpleNamespace(),
    )

    assert calls == ["confirm:proposal-1", "flush"]
    assert response == {
        "command_request_id": "cmd-entry",
        "accepted": True,
        "session": {
            "session_id": "sop-1",
            "runtime_status": {
                "status": "running",
                "runtime_ready": False,
                "blocking_run_id": "run-1",
            },
        },
        "run_id": "run-1",
        "attempt_id": "attempt-1",
    }


@pytest.mark.asyncio
async def test_active_session_snapshot_contains_runtime_status(
    monkeypatch,
) -> None:
    record = SimpleNamespace(
        projection=SimpleNamespace(sop_session_id="sop-1"),
    )

    class FakeService:
        def get_active_session(self):
            return record

        async def flush_chat_projection_outbox(self):
            return None

        async def get_runtime_status(self, _session_id):
            return {
                "status": "ready",
                "runtime_ready": True,
                "blocking_run_id": None,
            }

    async def fake_service_for_chat(_request, _chat_id):
        return FakeService()

    monkeypatch.setattr(
        wplus_router,
        "_service_for_chat",
        fake_service_for_chat,
    )
    monkeypatch.setattr(
        wplus_router,
        "serialize_session",
        lambda _record: {"session_id": "sop-1"},
    )

    response = await wplus_router.get_active_wplus_sop_session(
        "chat-1",
        SimpleNamespace(),
    )

    assert response["runtime_status"] == {
        "status": "ready",
        "runtime_ready": True,
        "blocking_run_id": None,
    }


@pytest.mark.parametrize(
    ("chat_user", "expected_status"),
    [("user-1", 200), ("other-user", 404), (None, 404)],
)
def test_active_session_empty_http_contract(
    tmp_path,
    monkeypatch,
    chat_user,
    expected_status,
) -> None:
    async def get_chat(_chat_id):
        if chat_user is None:
            return None
        return SimpleNamespace(
            id="chat-1",
            session_id="logical-1",
            user_id=chat_user,
        )

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent-1",
        chat_manager=SimpleNamespace(get_chat=get_chat),
    )

    async def get_workspace(_request):
        return workspace

    monkeypatch.setattr(wplus_router, "get_agent_for_request", get_workspace)
    app = FastAPI()
    app.include_router(wplus_router.router)

    @app.middleware("http")
    async def add_identity(request: Request, call_next):
        request.state.tenant_id = "tenant-1"
        request.state.source_id = "console"
        request.state.user_id = "user-1"
        return await call_next(request)

    with TestClient(app) as client:
        response = client.get("/wplus-sop/chats/chat-1/active-session")

    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json() is None


@pytest.mark.asyncio
async def test_command_snapshot_contains_runtime_status(monkeypatch) -> None:
    record = SimpleNamespace(
        projection=SimpleNamespace(sop_session_id="sop-1"),
    )
    receipt = SimpleNamespace(run_id="run-2", attempt_id="attempt-2")

    class FakeService:
        async def execute_command(self, **_kwargs):
            return SimpleNamespace(record=record, receipt=receipt)

        async def flush_chat_projection_outbox(self):
            return None

        async def get_runtime_status(self, _session_id):
            return {
                "status": "running",
                "runtime_ready": False,
                "blocking_run_id": "run-2",
            }

    async def fake_service_for_session(_request, _session_id):
        return FakeService()

    monkeypatch.setattr(
        wplus_router,
        "_service_for_session",
        fake_service_for_session,
    )
    monkeypatch.setattr(
        wplus_router,
        "serialize_session",
        lambda _record: {"session_id": "sop-1"},
    )

    response = await wplus_router.post_wplus_sop_command(
        "sop-1",
        SimpleNamespace(
            command="submit_answers",
            command_request_id="cmd-1",
            expected_state_version=2,
            payload={"answers": {}},
        ),
        SimpleNamespace(),
    )

    assert response["session"]["runtime_status"] == {
        "status": "running",
        "runtime_ready": False,
        "blocking_run_id": "run-2",
    }


@pytest.mark.asyncio
async def test_sse_checks_for_orphan_and_emits_recovery_event(
    monkeypatch,
) -> None:
    calls: list[str] = []
    event = SimpleNamespace(
        event_id="evt-orphan",
        state_version=2,
        kind=SimpleNamespace(value="recoverable_failure"),
    )
    record = SimpleNamespace(
        events=[event],
        projection=SimpleNamespace(
            current_run_id="run-orphan",
            is_terminal=True,
        ),
    )

    class FakeService:
        async def recover_orphaned_generation_run(self, session_id):
            calls.append(f"recover:{session_id}")

        def get_session(self, session_id):
            calls.append(f"get:{session_id}")
            return record

    class FakeRequest:
        async def is_disconnected(self):
            return False

    async def fake_service_for_session(_request, _session_id):
        return FakeService()

    monkeypatch.setattr(
        wplus_router,
        "_service_for_session",
        fake_service_for_session,
    )
    monkeypatch.setattr(
        wplus_router,
        "serialize_session",
        lambda _record: {"state": "RecoverableFailure"},
    )

    response = await wplus_router.stream_wplus_sop_events(
        "sop-orphan",
        FakeRequest(),
    )
    chunk = await anext(response.body_iterator)

    assert '"kind": "recoverable_failure"' in chunk
    assert calls == ["recover:sop-orphan", "get:sop-orphan"]


@pytest.mark.asyncio
async def test_sse_emits_changed_safe_trace_without_persisting_an_event(
    monkeypatch,
) -> None:
    workspace = SimpleNamespace()
    registry = get_wplus_safe_stream_trace_registry(workspace)
    registry.start_run("sop-1", "run-1")
    registry.ingest(
        "sop-1",
        "run-1",
        "data: "
        + json.dumps(
            {
                "object": "message",
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [
                    {
                        "type": "text",
                        "text": "正在分析请求并整理关键事实。",
                    },
                ],
            },
            ensure_ascii=False,
        )
        + "\n\n",
    )
    persisted_events: list[object] = []
    record = SimpleNamespace(
        events=persisted_events,
        projection=SimpleNamespace(
            current_run_id="run-1",
            state_version=7,
            is_terminal=False,
        ),
    )

    class FakeService:
        def __init__(self):
            self.workspace = workspace

        async def recover_orphaned_generation_run(self, _session_id):
            return None

        def get_session(self, _session_id):
            return record

    class FakeRequest:
        def __init__(self):
            self.checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks >= 3

    async def fake_service_for_session(_request, _session_id):
        return FakeService()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(
        wplus_router,
        "_service_for_session",
        fake_service_for_session,
    )
    monkeypatch.setattr(wplus_router.asyncio, "sleep", no_sleep)

    response = await wplus_router.stream_wplus_sop_events(
        "sop-1",
        FakeRequest(),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 1
    payload = json.loads(chunks[0].removeprefix("data: ").strip())
    assert payload == {
        "event_id": "trace:sop-1:run-1:1",
        "session_id": "sop-1",
        "state_version": 7,
        "kind": "safe_stream_trace",
        "run_id": "run-1",
        "safe_stream_trace": {
            "sequence": 1,
            "summary_text": "正在分析请求并整理关键事实。",
            "truncated": False,
            "entries": [
                {
                    "entry_id": "assistant_text:msg-1",
                    "kind": "assistant_text",
                    "status": "running",
                    "text": "正在分析请求并整理关键事实。",
                },
            ],
        },
    }
    assert persisted_events == []

    reconnect_response = await wplus_router.stream_wplus_sop_events(
        "sop-1",
        FakeRequest(),
    )
    reconnect_chunks = [chunk async for chunk in reconnect_response.body_iterator]
    assert len(reconnect_chunks) == 1
    reconnect_payload = json.loads(
        reconnect_chunks[0].removeprefix("data: ").strip(),
    )
    assert reconnect_payload["safe_stream_trace"]["sequence"] == 1
    assert persisted_events == []


@pytest.mark.asyncio
async def test_sse_emits_initial_and_changed_runtime_status_without_event(
    monkeypatch,
) -> None:
    persisted_events: list[object] = []
    record = SimpleNamespace(
        events=persisted_events,
        projection=SimpleNamespace(
            current_run_id="run-1",
            state_version=7,
            is_terminal=False,
        ),
    )

    class FakeService:
        workspace = SimpleNamespace()

        def __init__(self):
            self.statuses = [
                {
                    "status": "finalizing",
                    "runtime_ready": False,
                    "blocking_run_id": "run-1",
                },
                {
                    "status": "ready",
                    "runtime_ready": True,
                    "blocking_run_id": None,
                },
            ]

        async def recover_orphaned_generation_run(self, _session_id):
            return None

        def get_session(self, _session_id):
            return record

        async def get_runtime_status(self, _session_id):
            if len(self.statuses) > 1:
                return self.statuses.pop(0)
            return self.statuses[0]

    class FakeRequest:
        def __init__(self):
            self.checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks >= 4

    async def fake_service_for_session(_request, _session_id):
        return FakeService()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(
        wplus_router,
        "_service_for_session",
        fake_service_for_session,
    )
    monkeypatch.setattr(wplus_router.asyncio, "sleep", no_sleep)

    response = await wplus_router.stream_wplus_sop_events(
        "sop-1",
        FakeRequest(),
    )
    chunks = [chunk async for chunk in response.body_iterator]
    payloads = [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in chunks
    ]
    reconnect_response = await wplus_router.stream_wplus_sop_events(
        "sop-1",
        FakeRequest(),
    )
    reconnect_payloads = [
        json.loads(chunk.removeprefix("data: ").strip())
        async for chunk in reconnect_response.body_iterator
    ]

    assert [payload["runtime_status"]["status"] for payload in payloads] == [
        "finalizing",
        "ready",
    ]
    assert all(payload["kind"] == "runtime_status" for payload in payloads)
    assert all(payload["state_version"] == 7 for payload in payloads)
    assert {payload["event_id"] for payload in payloads}.isdisjoint(
        payload["event_id"] for payload in reconnect_payloads
    )
    assert persisted_events == []
