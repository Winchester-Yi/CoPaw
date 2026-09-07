# -*- coding: utf-8 -*-
"""Application-service tests for the complete W+ SOP lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swe.app.answer_turn.models import StopClaim, TurnIdentity, TurnStatus
from swe.app.wplus_sop import service as service_module
from swe.app.wplus_sop.models import (
    CommandReceipt,
    FinalSopResult,
    OwnershipTuple,
    Question,
    QuestionBatch,
    QuestionOption,
    QuestionType,
    RecoverableFailurePayload,
    RunAttempt,
    RunStatus,
    SessionProjection,
    SessionState,
    Stage,
    StageStatus,
)
from swe.app.wplus_sop.runtime import WPlusChatRunBusyError
from swe.app.wplus_sop.service import (
    WPlusCommandError,
    WPlusOwnershipError,
    WPlusOwningChatFinalizingError,
    WPlusRuntimeStartError,
    WPlusSopService,
    serialize_session,
    store_path_for_workspace,
)
from swe.app.wplus_sop.store import StaleStateVersionError, WPlusSopStore
from swe.config.context import resolve_file_url_base


class FakeChatManager:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(
            id="chat-1",
            session_id="logical-1",
            user_id="user-1",
            channel="console",
            meta={},
        )
        self.updates = 0

    async def get_chat(self, chat_id: str):
        return self.chat if chat_id == self.chat.id else None

    async def update_chat(self, chat):
        self.chat = chat
        self.updates += 1
        return chat


class FakeTaskTracker:
    def __init__(self) -> None:
        self.stops = 0
        self.status = "idle"
        self.status_reads = 0
        self.idle_after_reads: int | None = None

    async def request_stop(self, _run_key: str) -> bool:
        self.stops += 1
        return True

    async def read_status(self, _run_key: str) -> str:
        self.status_reads += 1
        if (
            self.idle_after_reads is not None
            and self.status_reads >= self.idle_after_reads
        ):
            self.status = "idle"
        return self.status

    async def call_if_idle(self, _run_key: str, callback):
        if self.status != "idle":
            return False, None
        return True, callback()


class FakeAnswerTurnCoordinator:
    def __init__(self, tracker: FakeTaskTracker) -> None:
        self.tracker = tracker
        self.identity = TurnIdentity(
            chat_id="chat-1",
            msgid="msg-1",
            turn_id="turn-1",
        )

    async def status(self, _chat_id: str) -> TurnStatus | None:
        status = await self.tracker.read_status(_chat_id)
        if status == "idle":
            return None
        if status == "stopping":
            return TurnStatus.STOPPING
        return TurnStatus.RUNNING

    async def current_identity(self, chat_id: str) -> TurnIdentity | None:
        status = await self.status(chat_id)
        return self.identity if status is not None else None

    async def claim_stop(
        self,
        identity: TurnIdentity,
        *,
        msgid: str | None = None,
        internal: bool = False,
    ) -> StopClaim:
        _ = msgid, internal
        self.tracker.stops += 1
        return StopClaim(True, identity=identity, status=TurnStatus.STOPPING)


def _ownership() -> OwnershipTuple:
    return OwnershipTuple(
        tenant_id="tenant-1",
        source_id="console",
        user_id="user-1",
        agent_id="agent-1",
        chat_id="chat-1",
        logical_chat_session_id="logical-1",
    )


def _service(tmp_path: Path) -> WPlusSopService:
    tracker = FakeTaskTracker()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        chat_manager=FakeChatManager(),
        task_tracker=tracker,
    )
    workspace.answer_turn_coordinator = FakeAnswerTurnCoordinator(tracker)
    return WPlusSopService(
        workspace=workspace,
        ownership=_ownership(),
        store=WPlusSopStore(tmp_path / "wplus-sop.json"),
    )


def test_store_path_for_workspace_uses_sop_directory_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / ".copaw" / "wplus-sop.json"
    legacy_path.parent.mkdir()
    legacy_path.write_text("{}", encoding="utf-8")

    store_path = store_path_for_workspace(tmp_path)

    assert store_path == tmp_path / ".sop" / "wplus-sop.json"
    assert store_path != legacy_path


def _create_generation_run(
    service: WPlusSopService,
    *,
    session_id: str,
    created_at: datetime,
    state: SessionState = SessionState.GENERATING_STAGE_PROPOSAL,
    status: RunStatus = RunStatus.CLAIMED,
) -> None:
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=state,
            state_version=1,
            title="SOP",
            current_run_id=f"run-{session_id}",
        ),
        command_receipt=CommandReceipt(
            command_request_id=f"cmd-{session_id}",
            command="confirm_entry",
            sop_session_id=session_id,
            resulting_state_version=1,
            starts_run=True,
            run_id=f"run-{session_id}",
            attempt_id=f"attempt-{session_id}",
        ),
        run_attempt=RunAttempt(
            run_id=f"run-{session_id}",
            attempt_id=f"attempt-{session_id}",
            command_request_id=f"cmd-{session_id}",
            command="propose_stage_queue",
            status=status,
            created_at=created_at,
        ),
    )


def _create_question_generation_run(
    service: WPlusSopService,
    *,
    session_id: str,
) -> None:
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.GENERATING_QUESTIONS,
            state_version=1,
            title="SOP",
            stages=[
                Stage(
                    stage_id="stage-1",
                    name="确认范围",
                    status="clarifying",
                ),
                Stage(stage_id="stage-2", name="生成结果"),
            ],
            current_stage_id="stage-1",
            current_run_id=f"run-{session_id}",
        ),
        command_receipt=CommandReceipt(
            command_request_id=f"cmd-{session_id}",
            command="confirm_stage_queue",
            sop_session_id=session_id,
            resulting_state_version=1,
            starts_run=True,
            run_id=f"run-{session_id}",
            attempt_id=f"attempt-{session_id}",
        ),
        run_attempt=RunAttempt(
            run_id=f"run-{session_id}",
            attempt_id=f"attempt-{session_id}",
            command_request_id=f"cmd-{session_id}",
            command="confirm_stage_queue",
            status=RunStatus.CLAIMED,
        ),
    )


async def _send(
    service: WPlusSopService,
    session_id: str,
    command: str,
    payload: dict | None = None,
    *,
    request_id: str,
):
    record = service.get_session(session_id)
    return await service.execute_command(
        sop_session_id=session_id,
        command=command,
        command_request_id=request_id,
        expected_state_version=record.projection.state_version,
        payload=payload or {},
    )


def _question_payload(stage_id: str, suffix: str) -> dict:
    return {
        "batch_id": f"batch-{suffix}",
        "stage_id": stage_id,
        "questions": [
            {
                "question_id": f"q-{suffix}",
                "prompt": "请确认范围",
                "type": "single_select",
                "options": [
                    {"option_id": "yes", "label": "确认"},
                    {"option_id": "no", "label": "调整"},
                ],
            },
        ],
    }


def _create_structured_answer_session(service: WPlusSopService) -> str:
    session_id = "sop-structured-answers"
    stage = Stage(stage_id="stage-1", name="确认范围")
    question_batch = QuestionBatch(
        batch_id="batch-structured",
        stage_id=stage.stage_id,
        questions=[
            Question(
                question_id="q-single",
                prompt="选择主要入口",
                type=QuestionType.SINGLE_SELECT,
                options=[
                    QuestionOption(option_id="fixed", label="固定入口"),
                    QuestionOption(
                        option_id="other",
                        label="其他入口",
                        requires_custom_input=True,
                    ),
                ],
            ),
            Question(
                question_id="q-multi",
                prompt="选择辅助入口",
                type=QuestionType.MULTI_SELECT,
                options=[
                    QuestionOption(option_id="chat", label="Chat"),
                    QuestionOption(option_id="api", label="API"),
                ],
            ),
            Question(
                question_id="q-note",
                prompt="补充约束",
                type=QuestionType.FREE_TEXT,
            ),
        ],
    )
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.AWAITING_ANSWER,
            state_version=1,
            title="SOP",
            stages=[stage],
            current_stage_id=stage.stage_id,
            current_question_batch=question_batch,
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-create-structured-answers",
            command="test_setup",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    return session_id


def _trial_plan_payload(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "input_snapshot_id": f"input-{run_id}",
        "steps": [
            {
                "step_id": f"step-{run_id}",
                "label": "调用业务能力",
                "capability_id": "crm.query",
                "capability_version": "1",
            },
        ],
    }


def _trial_result_payload(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "summary": "预跑完成",
        "result_lists": [
            {
                "list_id": f"result-{run_id}",
                "label": "预跑对象列表",
                "columns": [
                    {
                        "field": "name",
                        "label": "名称",
                        "type": "string",
                    },
                    {
                        "field": "details",
                        "label": "详情",
                        "type": "object",
                    },
                ],
                "rows": [
                    {
                        "name": "示例记录",
                        "details": {
                            "status": "ready",
                            "children": [{"label": "子项"}],
                        },
                    },
                ],
            },
        ],
    }


def _stage_report_artifacts(seed: str = "a") -> list[dict]:
    return [
        {
            "artifact_id": "stage_sop_json",
            "name": "stage_sop.json",
            "static_file_name": f"{seed}.json",
            "static_url": f"https://static.example/{seed}.json",
            "sha256": seed * 64,
            "copied_by": "copy_file_to_static",
        },
        {
            "artifact_id": "stage_sop_md",
            "name": "stage_sop.md",
            "static_file_name": f"{seed}.md",
            "static_url": f"https://static.example/{seed}.md",
            "sha256": seed * 64,
            "copied_by": "copy_file_to_static",
        },
        {
            "artifact_id": "stage_sop_html",
            "name": "stage_sop.html",
            "static_file_name": f"{seed}.html",
            "static_url": f"https://static.example/{seed}.html",
            "sha256": seed * 64,
            "copied_by": "copy_file_to_static",
        },
    ]


def _stage_report_payload(
    stage_id: str,
    report_no: int,
    *,
    revision: int = 1,
) -> dict:
    return {
        "report": {
            "stage_id": stage_id,
            "report_no": report_no,
            "revision": revision,
            "artifacts": _stage_report_artifacts(
                "0123456789abcdef"[report_no % 16],
            ),
            "validation": {
                "schema_validator": "scripts/validate_stage_sop.py",
                "schema_exit_code": 0,
                "renderers": [
                    "scripts/render_stage_md.py",
                    "scripts/render_stage_sop.py",
                ],
            },
        },
    }


def _cumulative_refreshed_payload(
    session,
    *,
    preview_version: int = 1,
) -> dict:
    snapshots = session.projection.confirmed_snapshots
    return {
        "preview": {
            "preview_version": preview_version,
            "stage_order": [snapshot.stage_id for snapshot in snapshots],
            "snapshots": [
                snapshot.model_dump(mode="json") for snapshot in snapshots
            ],
            "artifacts": _stage_report_artifacts("c"),
            "rendered_sha256": {"stage_sop_json": "c" * 64},
        },
    }


def _final_result_payload(tmp_path: Path) -> dict:
    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    spec = {"name": "客户经营 SOP"}
    contents = {
        "sop_spec.json": json.dumps(
            spec,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "sop_render.md": "# 客户经营 SOP\n\n执行复核。",
        "sop_render.html": "<article><h1>客户经营 SOP</h1></article>",
        "example_result.html": "<article><h1>脱敏示例结果</h1></article>",
    }
    artifact_ids = {
        "sop_spec.json": "sop_spec",
        "sop_render.md": "sop_render_md",
        "sop_render.html": "sop_render_html",
        "example_result.html": "example_result_html",
    }
    artifacts = []
    static_access, _ = resolve_file_url_base()
    for name, content in contents.items():
        raw = content.encode("utf-8")
        (static_dir / name).write_bytes(raw)
        artifacts.append(
            {
                "artifact_id": artifact_ids[name],
                "name": name,
                "static_file_name": name,
                "static_url": (
                    f"{static_access}/static/tenant-1/agent-1/{name}"
                ),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "copied_by": "copy_file_to_static",
            },
        )
    return {
        "sop_spec": spec,
        "readable_sop": contents["sop_render.md"],
        "html": contents["sop_render.html"],
        "example_result_html": contents["example_result.html"],
        "artifacts": artifacts,
        "validation": {
            "schema_validator": "scripts/validate_sop.py",
            "schema_exit_code": 0,
            "renderers": ["scripts/render_md.py", "scripts/render_sop.py"],
        },
    }


def test_delivered_artifacts_accept_tool_urls_and_url_result_references(
    tmp_path: Path,
) -> None:
    payload = _final_result_payload(tmp_path)
    payload["sop_spec"] = {
        "file_url": "https://files.example.test/static/sop_spec_6.json",
        "sha256": payload["artifacts"][0]["sha256"],
    }
    payload["readable_sop"] = (
        "https://files.example.test/static/sop_render_6.md"
    )
    payload["html"] = "https://files.example.test/static/sop_render_6.html"
    payload["example_result_html"] = (
        "https://files.example.test/static/example_result_5.html"
    )
    for artifact in payload["artifacts"]:
        artifact["static_url"] = (
            "https://files.example.test/tool-output/"
            f"{artifact['static_file_name']}"
        )

    service_module._validate_delivered_artifacts(
        workspace_dir=tmp_path,
        result=FinalSopResult.model_validate(payload),
    )


def test_delivered_artifacts_reject_missing_local_file(tmp_path: Path) -> None:
    payload = _final_result_payload(tmp_path)
    (tmp_path / "static" / "sop_render.md").unlink()

    with pytest.raises(
        WPlusCommandError,
        match="delivered artifact is missing: sop_render_md",
    ):
        service_module._validate_delivered_artifacts(
            workspace_dir=tmp_path,
            result=FinalSopResult.model_validate(payload),
        )


def test_delivered_artifacts_reject_sha256_mismatch(tmp_path: Path) -> None:
    payload = _final_result_payload(tmp_path)
    payload["artifacts"][1]["sha256"] = "0" * 64

    with pytest.raises(
        WPlusCommandError,
        match="delivered artifact hash mismatch: sop_render_md",
    ):
        service_module._validate_delivered_artifacts(
            workspace_dir=tmp_path,
            result=FinalSopResult.model_validate(payload),
        )


@pytest.mark.asyncio
async def test_completed_trial_snapshot_restores_results_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-trial-evidence"
    _create_question_generation_run(service, session_id=session_id)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service.append_agent_event(
        kind="question_batch",
        payload=_question_payload("stage-1", "trial-evidence"),
        event_key="trial-evidence-questions",
    )
    await _send(
        service,
        session_id,
        "submit_answers",
        {"answers": {"q-trial-evidence": "yes"}},
        request_id="cmd-trial-evidence-answers",
    )
    run_id = service.get_session(session_id).projection.current_run_id
    assert run_id is not None
    service.append_agent_event(
        kind="trial_plan",
        payload=_trial_plan_payload(run_id),
        event_key="trial-evidence-plan",
    )
    service.append_agent_event(
        kind="trial_execution_started",
        payload={
            "run_id": run_id,
            "attempt_id": "attempt-trial-evidence",
            "started_at": "2026-08-03T08:00:00Z",
        },
        event_key="trial-evidence-started",
    )
    service.append_agent_event(
        kind="trial_execution_progress",
        payload={
            "run_id": run_id,
            "step_id": f"step-{run_id}",
            "status": "completed",
            "summary": "查询完成，共 1 条脱敏记录",
            "elapsed_ms": 1200,
        },
        event_key="trial-evidence-progress",
    )
    completed_payload = _trial_result_payload(run_id)
    completed_payload.update(
        {
            "warnings": ["结果仅包含脱敏字段"],
            "confirmed_facts": ["统计范围为未来 30 天"],
            "unknowns": ["是否排除已冻结账户"],
            "completed_at": "2026-08-03T08:00:02Z",
        },
    )
    service.append_agent_event(
        kind="trial_execution_completed",
        payload=completed_payload,
        event_key="trial-evidence-completed",
    )

    reloaded = WPlusSopStore(tmp_path / "wplus-sop.json").get_session(
        session_id,
    )
    assert reloaded is not None
    snapshot = serialize_session(reloaded)

    assert snapshot["trial"]["summary"] == "预跑完成"
    assert snapshot["trial"]["warnings"] == ["结果仅包含脱敏字段"]
    assert snapshot["trial"]["started_at"] == "2026-08-03T08:00:00Z"
    assert snapshot["trial"]["completed_at"] == "2026-08-03T08:00:02Z"
    assert snapshot["trial"]["steps"] == [
        {
            "step_id": f"step-{run_id}",
            "title": "调用业务能力",
            "capability": "crm.query",
            "status": "completed",
            "summary": "查询完成，共 1 条脱敏记录",
            "elapsed_ms": 1200,
        },
    ]
    assert snapshot["trial"]["result_rows"][0]["name"] == "示例记录"
    assert snapshot["facts"] == ["统计范围为未来 30 天"]
    assert snapshot["unknowns"] == ["是否排除已冻结账户"]
    assert snapshot["capabilities"] == [
        {
            "capability_id": "crm.query",
            "name": "crm.query",
            "verification_status": "verified",
            "output_contract_status": "verified",
        },
    ]

    await _send(
        service,
        session_id,
        "submit_trial_feedback",
        {"feedback": "请排除冻结账户"},
        request_id="cmd-trial-evidence-rerun",
    )
    rerun_snapshot = serialize_session(service.get_session(session_id))
    assert rerun_snapshot["trial"]["status"] == "planning"
    assert rerun_snapshot["trial"]["result_rows"] == []
    assert rerun_snapshot["trial"]["summary"] is None
    assert rerun_snapshot["capabilities"] == []


@pytest.mark.asyncio
async def test_confirm_waits_for_owning_chat_idle_before_store_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    before_proposal = service.store.get_entry_proposal(proposal.proposal_id)
    service.workspace.task_tracker.status = "running"
    monkeypatch.setattr(
        service_module,
        "_CHAT_IDLE_WAIT_TIMEOUT_SECONDS",
        0.001,
    )
    monkeypatch.setattr(service_module, "_CHAT_IDLE_POLL_SECONDS", 0.001)

    async def unexpected_start(**_kwargs):
        pytest.fail("Agent run must not start while the owning Chat is busy")

    monkeypatch.setattr(
        service_module,
        "start_wplus_chat_turn",
        unexpected_start,
    )

    with pytest.raises(WPlusOwningChatFinalizingError):
        await service.confirm_entry(
            proposal_id=proposal.proposal_id,
            command_request_id="cmd-entry-chat-busy",
            skill_snapshot_id="sha256:miner",
        )

    assert service.store.get_entry_proposal(proposal.proposal_id) == (
        before_proposal
    )
    assert service.get_active_session() is None


@pytest.mark.asyncio
async def test_confirm_persists_session_before_starting_agent_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="请帮我梳理客户经营 SOP",
        mode="explicit",
        memory_user_scope="anon_scope_123456",
    )
    observed: dict[str, object] = {}

    async def fake_start(**kwargs):
        record = service.store.get_session(kwargs["sop_session_id"])
        assert record is not None
        assert (
            record.projection.state is SessionState.GENERATING_STAGE_PROPOSAL
        )
        observed["session_id"] = kwargs["sop_session_id"]
        observed["payload"] = kwargs["payload"]
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    mutation = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry",
        skill_snapshot_id="sha256:miner",
    )

    assert mutation.record.projection.sop_session_id == observed["session_id"]
    assert mutation.record.projection.memory_user_scope == "anon_scope_123456"
    assert observed["payload"] == {
        "original_request": "请帮我梳理客户经营 SOP",
        "memory_user_scope": "anon_scope_123456",
    }
    assert service.get_active_session() is not None

    projected = await service.flush_chat_projection_outbox()
    assert projected == 1
    assert (
        service.workspace.chat_manager.chat.meta["wplus_sop_session"]["state"]
        == "GeneratingStageProposal"
    )
    assert service.store.pending_outbox() == []


@pytest.mark.asyncio
async def test_confirm_allows_source_id_to_differ_from_chat_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    service.ownership = _ownership().model_copy(
        update={"source_id": "external-source-1"},
    )
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    captured: dict[str, Any] = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    mutation = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-different-source",
        skill_snapshot_id="sha256:miner",
    )

    assert (
        mutation.record.projection.ownership.source_id == "external-source-1"
    )
    assert captured["source_id"] == "external-source-1"
    assert captured["chat"] is service.workspace.chat_manager.chat


@pytest.mark.asyncio
async def test_confirm_reuses_verified_chat_for_entry_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    chat = service.workspace.chat_manager.chat
    lookups: list[str] = []

    async def one_shot_get_chat(chat_id: str):
        lookups.append(chat_id)
        return chat if len(lookups) == 1 else None

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(
        service.workspace.chat_manager,
        "get_chat",
        one_shot_get_chat,
    )
    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)

    mutation = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-one-chat-read",
        skill_snapshot_id="sha256:miner",
    )

    assert mutation.record.projection.state is (
        SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert lookups == ["chat-1"]
    assert service.workspace.chat_manager.updates == 1
    assert chat.meta["wplus_sop_entry_proposal"] == {
        "proposal_id": proposal.proposal_id,
        "mode": "explicit",
        "status": "confirmed",
        "session_id": mutation.record.projection.sop_session_id,
    }


@pytest.mark.asyncio
async def test_confirm_outbox_recovers_failed_entry_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )

    class FailOnceChatManager:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(
                id="chat-1",
                session_id="logical-1",
                user_id="user-1",
                channel="console",
                meta={
                    "wplus_sop_entry_proposal": {
                        "proposal_id": proposal.proposal_id,
                        "mode": "explicit",
                        "status": "pending",
                        "session_id": None,
                    },
                },
            )
            self.update_attempts = 0

        async def get_chat(self, chat_id: str):
            return deepcopy(self.chat) if chat_id == self.chat.id else None

        async def update_chat(self, chat):
            self.update_attempts += 1
            if self.update_attempts == 1:
                raise OSError("temporary chats.json write failure")
            self.chat = deepcopy(chat)
            return deepcopy(chat)

    chat_manager = FailOnceChatManager()
    service.workspace.chat_manager = chat_manager

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)

    mutation = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-recover-projection",
        skill_snapshot_id="sha256:miner",
    )
    projected = await service.flush_chat_projection_outbox()

    assert mutation.record.projection.state is (
        SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert projected == 1
    assert chat_manager.update_attempts == 2
    assert chat_manager.chat.meta["wplus_sop_entry_proposal"] == {
        "proposal_id": proposal.proposal_id,
        "mode": "explicit",
        "status": "confirmed",
        "session_id": mutation.record.projection.sop_session_id,
    }
    assert service.store.pending_outbox() == []


@pytest.mark.asyncio
async def test_submit_answers_accepts_structured_and_legacy_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    session_id = _create_structured_answer_session(service)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    mutation = await _send(
        service,
        session_id,
        "submit_answers",
        {
            "answers": {
                "q-single": {
                    "selected_option_ids": ["other"],
                    "text": "企业微信侧边栏",
                },
                "q-multi": ["chat", "api"],
                "q-note": "仅处理企业租户",
            },
        },
        request_id="cmd-structured-answers",
    )

    accepted = mutation.record.projection.answers[-1].answers
    assert (
        mutation.record.projection.state is SessionState.GENERATING_QUESTIONS
    )
    assert accepted[0].selected_option_ids == ["other"]
    assert accepted[0].text == "企业微信侧边栏"
    assert accepted[1].selected_option_ids == ["chat", "api"]
    assert accepted[2].text == "仅处理企业租户"


@pytest.mark.asyncio
async def test_submit_answers_waits_for_prior_chat_run_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    session_id = _create_structured_answer_session(service)
    tracker = service.workspace.task_tracker
    tracker.status = "running"
    tracker.idle_after_reads = 2
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        if tracker.status != "idle":
            raise WPlusChatRunBusyError(
                "The owning Chat already has an active Agent run",
            )
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    mutation = await _send(
        service,
        session_id,
        "submit_answers",
        {
            "answers": {
                "q-single": {"selected_option_ids": ["fixed"]},
                "q-multi": {"selected_option_ids": ["chat"]},
                "q-note": "无",
            },
        },
        request_id="cmd-wait-for-prior-chat-run",
    )

    assert tracker.status_reads >= 2
    assert len(starts) == 1
    assert (
        mutation.record.projection.state is SessionState.GENERATING_QUESTIONS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tracker_status", "expected_status", "runtime_ready"),
    [
        ("idle", "ready", True),
        ("running", "finalizing", False),
        ("stopping", "stopping", False),
    ],
)
async def test_runtime_status_projects_owning_chat_task_state(
    tmp_path: Path,
    tracker_status: str,
    expected_status: str,
    runtime_ready: bool,
) -> None:
    service = _service(tmp_path)
    session_id = _create_structured_answer_session(service)
    service.workspace.task_tracker.status = tracker_status

    status = await service.get_runtime_status(session_id)

    assert status == {
        "status": expected_status,
        "runtime_ready": runtime_ready,
        "blocking_run_id": (
            None
            if runtime_ready
            else service.get_session(session_id).projection.current_run_id
        ),
    }


@pytest.mark.asyncio
async def test_runtime_status_without_task_tracker_is_ready(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session_id = _create_structured_answer_session(service)
    del service.workspace.task_tracker

    assert await service.get_runtime_status(session_id) == {
        "status": "ready",
        "runtime_ready": True,
        "blocking_run_id": None,
    }


@pytest.mark.asyncio
async def test_runtime_status_is_running_during_active_generation(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-runtime-running"
    _create_generation_run(
        service,
        session_id=session_id,
        created_at=datetime.now(timezone.utc),
        state=SessionState.GENERATING_QUESTIONS,
    )
    service.workspace.task_tracker.status = "running"

    assert await service.get_runtime_status(session_id) == {
        "status": "running",
        "runtime_ready": False,
        "blocking_run_id": f"run-{session_id}",
    }


@pytest.mark.asyncio
async def test_prior_chat_run_timeout_does_not_mutate_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    session_id = _create_structured_answer_session(service)
    service.workspace.task_tracker.status = "running"
    monkeypatch.setattr(
        service_module,
        "_CHAT_IDLE_WAIT_TIMEOUT_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        service_module,
        "_CHAT_IDLE_POLL_SECONDS",
        0.001,
    )
    before = service.get_session(session_id)

    with pytest.raises(
        WPlusOwningChatFinalizingError,
        match="still finalizing",
    ) as raised:
        await _send(
            service,
            session_id,
            "submit_answers",
            {
                "answers": {
                    "q-single": {"selected_option_ids": ["fixed"]},
                    "q-multi": {"selected_option_ids": ["chat"]},
                    "q-note": "无",
                },
            },
            request_id="cmd-prior-chat-run-timeout",
        )

    assert raised.value.code == "owning_chat_finalizing"
    assert raised.value.retry_after_ms > 0
    assert service.get_session(session_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer_overrides", "error_match"),
    [
        (
            {"q-single": {"selected_option_ids": ["missing"]}},
            "selected option",
        ),
        (
            {
                "q-single": {
                    "selected_option_ids": ["fixed", "other"],
                    "text": "自定义",
                },
            },
            "single_select",
        ),
        (
            {"q-multi": {"selected_option_ids": []}},
            "multi_select",
        ),
        (
            {
                "q-single": {
                    "selected_option_ids": ["other"],
                    "text": "   ",
                },
            },
            "custom input",
        ),
        (
            {"q-multi": {"selected_option_ids": "chat"}},
            "selected_option_ids",
        ),
    ],
)
async def test_invalid_structured_answers_do_not_advance_session(
    tmp_path: Path,
    answer_overrides: dict,
    error_match: str,
) -> None:
    service = _service(tmp_path)
    session_id = _create_structured_answer_session(service)
    answers = {
        "q-single": {"selected_option_ids": ["fixed"]},
        "q-multi": {"selected_option_ids": ["chat"]},
        "q-note": {"selected_option_ids": [], "text": "无"},
        **answer_overrides,
    }
    before = service.get_session(session_id)

    with pytest.raises(WPlusCommandError, match=error_match):
        await _send(
            service,
            session_id,
            "submit_answers",
            {"answers": answers},
            request_id=f"cmd-invalid-{error_match}",
        )

    assert service.get_session(session_id) == before


@pytest.mark.asyncio
async def test_outbox_rejects_recreated_chat_with_drifted_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-drift-before-outbox",
        skill_snapshot_id="sha256:miner",
    )
    updates_before_flush = service.workspace.chat_manager.updates
    service.workspace.chat_manager.chat = SimpleNamespace(
        id="chat-1",
        session_id="other-logical-session",
        user_id="other-user",
        channel="console",
        meta={},
    )

    assert await service.flush_chat_projection_outbox() == 0
    assert service.workspace.chat_manager.updates == updates_before_flush
    assert len(service.store.pending_outbox()) == 1
    assert service.workspace.chat_manager.chat.meta == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_attribute", "drifted_value"),
    [
        ("id", "other-chat"),
        ("user_id", "other-user"),
        ("session_id", "other-logical-session"),
    ],
)
async def test_confirm_rejects_chat_identity_drift_before_store_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_attribute: str,
    drifted_value: str,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    before_proposal = service.store.get_entry_proposal(proposal.proposal_id)
    setattr(service.workspace.chat_manager.chat, chat_attribute, drifted_value)
    if chat_attribute == "id":
        drifted_chat = service.workspace.chat_manager.chat

        async def get_drifted_chat(_chat_id: str):
            return drifted_chat

        monkeypatch.setattr(
            service.workspace.chat_manager,
            "get_chat",
            get_drifted_chat,
        )

    async def unexpected_start(**_kwargs):
        pytest.fail("Agent run must not start for a drifted Chat")

    monkeypatch.setattr(
        service_module,
        "start_wplus_chat_turn",
        unexpected_start,
    )
    with pytest.raises(WPlusOwnershipError):
        await service.confirm_entry(
            proposal_id=proposal.proposal_id,
            command_request_id=f"cmd-entry-drifted-{chat_attribute}",
            skill_snapshot_id="sha256:miner",
        )

    assert service.store.get_entry_proposal(proposal.proposal_id) == (
        before_proposal
    )
    assert service.store.list_sessions() == []
    assert service.store.pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_attribute", "drifted_value"),
    [
        ("id", "other-chat"),
        ("user_id", "other-user"),
        ("session_id", "other-logical-session"),
    ],
)
async def test_run_command_rejects_chat_identity_drift_before_store_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_attribute: str,
    drifted_value: str,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-drifted-command"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.AWAITING_QUEUE_CONFIRMATION,
            state_version=1,
            title="SOP",
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-create-drifted-command",
            command="confirm_entry",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    before_record = service.get_session(session_id)
    setattr(service.workspace.chat_manager.chat, chat_attribute, drifted_value)
    if chat_attribute == "id":
        drifted_chat = service.workspace.chat_manager.chat

        async def get_drifted_chat(_chat_id: str):
            return drifted_chat

        monkeypatch.setattr(
            service.workspace.chat_manager,
            "get_chat",
            get_drifted_chat,
        )

    async def unexpected_start(**_kwargs):
        pytest.fail("Agent run must not start for a drifted Chat")

    monkeypatch.setattr(
        service_module,
        "start_wplus_chat_turn",
        unexpected_start,
    )
    with pytest.raises(WPlusOwnershipError):
        await service.execute_command(
            sop_session_id=session_id,
            command="confirm_stage_queue",
            command_request_id=f"cmd-run-drifted-{chat_attribute}",
            expected_state_version=1,
            payload={
                "stages": [
                    {"stage_id": "stage-1", "title": "确认范围"},
                    {"stage_id": "stage-2", "title": "生成结果"},
                ],
            },
        )

    assert service.get_session(session_id) == before_record
    assert service.store.pending_outbox() == []


@pytest.mark.asyncio
async def test_complete_two_stage_flow_preserves_nested_object_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建完整 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry",
        skill_snapshot_id="sha256:miner",
    )
    session_id = confirmed.record.projection.sop_session_id

    service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "创建任务"},
            ],
        },
        event_key="stage-proposal",
    )
    queue = await _send(
        service,
        session_id,
        "confirm_stage_queue",
        {
            "stages": [
                {"stage_id": "stage-1", "title": "确认范围"},
                {"stage_id": "stage-2", "title": "创建任务"},
            ],
        },
        request_id="cmd-queue",
    )
    duplicate = await service.execute_command(
        sop_session_id=session_id,
        command="confirm_stage_queue",
        command_request_id="cmd-queue",
        expected_state_version=queue.record.projection.state_version - 1,
        payload={"stages": []},
    )
    assert duplicate.duplicate is True
    assert duplicate.record.projection.state_version == (
        queue.record.projection.state_version
    )

    for index, stage_id in enumerate(("stage-1", "stage-2"), start=1):
        service.append_agent_event(
            kind="question_batch",
            payload=_question_payload(stage_id, str(index)),
            event_key=f"questions-{index}",
        )
        await _send(
            service,
            session_id,
            "submit_answers",
            {
                "batch_id": f"batch-{index}",
                "answers": {f"q-{index}": "yes"},
            },
            request_id=f"cmd-answers-{index}",
        )
        service.append_agent_event(
            kind="trial_plan",
            payload=_trial_plan_payload(f"trial-{index}"),
            event_key=f"trial-plan-{index}",
        )
        service.append_agent_event(
            kind="trial_execution_completed",
            payload=_trial_result_payload(f"trial-{index}"),
            event_key=f"trial-result-{index}",
        )
        await _send(
            service,
            session_id,
            "accept_trial",
            request_id=f"cmd-accept-{index}",
        )
        service.append_agent_event(
            kind="stage_report_generated",
            payload=_stage_report_payload(stage_id, 1),
            event_key=f"stage-report-{index}",
        )
        await _send(
            service,
            session_id,
            "confirm_stage",
            request_id=f"cmd-stage-{index}",
        )
        refresh_start = starts[-1]
        starts_before_refresh = len(starts)
        assert refresh_start["target_state"] == "RefreshingCumulative"
        service.append_agent_event(
            kind="cumulative_refreshed",
            payload=_cumulative_refreshed_payload(
                service.get_session(session_id),
                preview_version=index,
            ),
            event_key=f"cumulative-{index}",
        )
        assert len(starts) == starts_before_refresh
        blocked_kind = "sop_result" if index == 2 else "question_batch"
        blocked_payload = (
            {"result": _final_result_payload(tmp_path)}
            if index == 2
            else _question_payload("stage-2", "before-handoff")
        )
        with pytest.raises(
            WPlusCommandError,
            match="Agent run must complete before the next step",
        ):
            service.append_agent_event(
                kind=blocked_kind,
                payload=blocked_payload,
                event_key=f"blocked-before-handoff-{index}",
            )

        await refresh_start["on_complete"]()

        assert len(starts) == starts_before_refresh + 1
        continuation = starts[-1]
        assert continuation["command"] == "continue_after_cumulative"
        assert continuation["run_id"] != refresh_start["run_id"]
        assert continuation["target_state"] == (
            "FinalizingOutputs" if index == 2 else "GeneratingQuestions"
        )
        handoff_record = service.get_session(session_id)
        completed_refresh = next(
            run
            for run in handoff_record.runs
            if run.run_id == refresh_start["run_id"]
        )
        assert completed_refresh.status is RunStatus.COMPLETED
        assert handoff_record.projection.current_run_id == continuation["run_id"]

    service.append_agent_event(
        kind="sop_result",
        payload={"result": _final_result_payload(tmp_path)},
        event_key="final-result",
    )
    generated = service.append_agent_event(
        kind="memory_candidates",
        payload={"candidates": []},
        event_key="no-memory-candidates",
    )

    assert generated.record.projection.state is SessionState.OUTPUT_REVIEW
    completed = await _send(
        service,
        session_id,
        "confirm_outputs",
        request_id="cmd-confirm-outputs",
    )
    assert completed.record.projection.state is SessionState.COMPLETED
    nested = completed.record.projection.trial_result_lists[0].rows[0]
    assert nested["details"]["children"][0]["label"] == "子项"
    assert [start["command"] for start in starts] == [
        "propose_stage_queue",
        "confirm_stage_queue",
        "submit_answers",
        "accept_trial",
        "confirm_stage",
        "continue_after_cumulative",
        "submit_answers",
        "accept_trial",
        "confirm_stage",
        "continue_after_cumulative",
    ]
    assert starts[-1]["target_state"] == "FinalizingOutputs"
    assert await service.flush_chat_projection_outbox() > 0
    assert (
        service.workspace.chat_manager.chat.meta["wplus_sop_session"]["state"]
        == "Completed"
    )
    assert service.store.pending_outbox() == []


@pytest.mark.asyncio
async def test_output_review_previews_artifacts_then_writes_approved_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    session_id = "sop-output-review"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.FINALIZING_OUTPUTS,
            state_version=1,
            title="SOP",
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-finalizing",
            command="confirm_stage",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    service.append_agent_event(
        kind="sop_result",
        payload={"result": _final_result_payload(tmp_path)},
        event_key="result",
    )
    generated = service.append_agent_event(
        kind="memory_candidates",
        payload={
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "summary": "保留复核口径",
                    "memory_type": "common_wplus_knowledge",
                    "value": {"rule": "优先复核高风险分组"},
                    "evidence": "用户确认该页面口径已经验证。",
                },
            ],
        },
        event_key="memory",
    )

    assert generated.record.projection.state is SessionState.OUTPUT_REVIEW
    snapshot = serialize_session(generated.record)
    assert snapshot["result_preview"] == {
        "markdown": "# 客户经营 SOP\n\n执行复核。",
        "html": "<article><h1>客户经营 SOP</h1></article>",
        "markdown_url": snapshot["artifacts"][1]["download_url"],
        "html_url": snapshot["artifacts"][2]["download_url"],
        "markdown_sha256": snapshot["artifacts"][1]["sha256"],
        "html_sha256": snapshot["artifacts"][2]["sha256"],
    }
    assert [artifact["artifact_id"] for artifact in snapshot["artifacts"]] == [
        "sop_spec",
        "sop_render_md",
        "sop_render_html",
        "example_result_html",
    ]
    assert snapshot["artifacts"][0]["download_url"].startswith(
        f"/api/wplus-sop/sessions/{session_id}/artifacts/",
    )
    assert snapshot["artifacts"][0]["download_url"].endswith(
        "?download=true",
    )
    assert "static_url" not in json.dumps(snapshot)
    assert snapshot["memory_candidates"][0]["content"] == {
        "rule": "优先复核高风险分组",
    }
    assert snapshot["memory_candidates"][0]["memory_type"] == (
        "common_wplus_knowledge"
    )
    assert snapshot["memory_candidates"][0]["evidence"] == (
        "用户确认该页面口径已经验证。"
    )
    assert snapshot["memory_candidates"][0]["target_file"] == (
        "memory/common-wplus-knowledge.jsonl"
    )

    reviewing = await _send(
        service,
        session_id,
        "confirm_outputs",
        request_id="cmd-confirm-outputs",
    )
    assert reviewing.record.projection.state is SessionState.MEMORY_REVIEW
    writing = await _send(
        service,
        session_id,
        "resolve_memory",
        {
            "decisions": [
                {"candidate_id": "candidate-1", "decision": "approve"},
            ],
        },
        request_id="cmd-approve-memory",
    )

    candidate = writing.record.projection.memory_candidates[0]
    assert writing.record.projection.state is SessionState.WRITING_MEMORY
    assert candidate.status.value == "writing"
    assert candidate.write_receipt is None
    assert starts[-1]["target_state"] == "WritingMemory"
    assert starts[-1]["payload"] == {
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "type": "common_wplus_knowledge",
                "content": {"rule": "优先复核高风险分组"},
                "evidence": "用户确认该页面口径已经验证。",
                "target_scope": "common",
                "target_file": "memory/common-wplus-knowledge.jsonl",
                "script": "scripts/memory_store.py",
                "approved": True,
            },
        ],
    }

    completed = service.append_agent_event(
        kind="memory_write_batch_result",
        payload={
            "results": [
                {
                    "candidate_id": "candidate-1",
                    "status": "succeeded",
                    "target_scope": "common",
                    "target_file": "memory/common-wplus-knowledge.jsonl",
                    "result": "appended",
                    "script": "scripts/memory_store.py",
                },
            ],
        },
        event_key="memory-write-candidate-1",
        trusted_sop_session_id=session_id,
        trusted_run_id=starts[-1]["run_id"],
        trusted_attempt_id=starts[-1]["attempt_id"],
    )
    candidate = completed.record.projection.memory_candidates[0]
    assert completed.record.projection.state is SessionState.COMPLETED
    assert candidate.status.value == "approved"
    assert candidate.write_receipt is not None
    assert candidate.write_receipt.memory_id == (
        "wplus-sop/sop-output-review/candidate-1"
    )

    duplicate = await service.execute_command(
        sop_session_id=session_id,
        command="resolve_memory",
        command_request_id="cmd-approve-memory",
        expected_state_version=1,
        payload={
            "decisions": [
                {"candidate_id": "candidate-1", "decision": "approve"},
            ],
        },
    )
    assert duplicate.duplicate is True


@pytest.mark.asyncio
async def test_failed_memory_write_stays_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    session_id = "sop-memory-retry"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.MEMORY_REVIEW,
            state_version=1,
            title="SOP",
            final_result=FinalSopResult.model_validate(
                _final_result_payload(tmp_path),
            ),
            memory_candidates=[
                {
                    "candidate_id": "candidate-1",
                    "summary": "保存规则",
                    "memory_type": "sop_case",
                    "value": {"pattern": "优先执行复核"},
                    "evidence": "用户确认这是完全脱敏的通用 SOP 模式。",
                    "target_scope": "cases",
                    "target_file": "memory/cases/sop-cases.jsonl",
                },
            ],
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-memory-review",
            command="confirm_outputs",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )

    writing = await _send(
        service,
        session_id,
        "resolve_memory",
        {
            "decisions": [
                {"candidate_id": "candidate-1", "decision": "approve"},
            ],
        },
        request_id="cmd-memory-failed",
    )
    failed = service.append_agent_event(
        kind="memory_write_batch_result",
        payload={
            "results": [
                {
                    "candidate_id": "candidate-1",
                    "status": "failed",
                    "error_code": "memory_store_rejected",
                    "summary": "disk unavailable",
                    "script": "scripts/memory_store.py",
                },
            ],
        },
        event_key="memory-write-failed-candidate-1",
        trusted_sop_session_id=session_id,
        trusted_run_id=starts[-1]["run_id"],
        trusted_attempt_id=starts[-1]["attempt_id"],
    )
    candidate = failed.record.projection.memory_candidates[0]
    assert writing.record.projection.state is SessionState.WRITING_MEMORY
    assert failed.record.projection.state is SessionState.MEMORY_REVIEW
    assert candidate.status.value == "failed"
    assert candidate.failure_reason == "disk unavailable"

    retried = await _send(
        service,
        session_id,
        "resolve_memory",
        {
            "decisions": [
                {"candidate_id": "candidate-1", "decision": "approve"},
            ],
        },
        request_id="cmd-memory-retry",
    )
    assert retried.record.projection.state is SessionState.WRITING_MEMORY
    assert len(starts) == 2


def test_memory_write_receipt_cannot_switch_candidate_or_target(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-memory-boundary"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.WRITING_MEMORY,
            state_version=1,
            title="SOP",
            current_run_id="run-memory-boundary",
            active_memory_candidate_id="candidate-1",
            final_result=FinalSopResult.model_validate(
                _final_result_payload(tmp_path),
            ),
            memory_candidates=[
                {
                    "candidate_id": "candidate-1",
                    "summary": "保存规则",
                    "memory_type": "sop_case",
                    "value": {"pattern": "优先执行复核"},
                    "evidence": "用户确认这是脱敏模式。",
                    "target_scope": "cases",
                    "target_file": "memory/cases/sop-cases.jsonl",
                    "status": "writing",
                },
            ],
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-memory-boundary",
            command="resolve_memory",
            sop_session_id=session_id,
            resulting_state_version=1,
            starts_run=True,
            run_id="run-memory-boundary",
            attempt_id="attempt-memory-boundary",
        ),
        run_attempt=RunAttempt(
            run_id="run-memory-boundary",
            attempt_id="attempt-memory-boundary",
            command_request_id="cmd-memory-boundary",
            command="resolve_memory",
            status=RunStatus.RUNNING,
        ),
    )

    with pytest.raises(WPlusCommandError, match="does not match approved"):
        service.append_agent_event(
            kind="memory_write_completed",
            payload={
                "candidate_id": "candidate-1",
                "target_scope": "common",
                "target_file": "memory/common-wplus-knowledge.jsonl",
                "result": "appended",
                "script": "scripts/memory_store.py",
            },
            event_key="forged-memory-target",
            trusted_sop_session_id=session_id,
            trusted_run_id="run-memory-boundary",
            trusted_attempt_id="attempt-memory-boundary",
        )


@pytest.mark.asyncio
async def test_memory_decisions_are_atomic_and_approved_candidates_share_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    session_id = "sop-memory-batch"
    candidates = [
        {
            "candidate_id": f"candidate-{index}",
            "summary": f"保存规则 {index}",
            "memory_type": "sop_case",
            "value": {"pattern": f"rule-{index}"},
            "evidence": f"用户确认脱敏规则 {index}。",
            "target_scope": "cases",
            "target_file": "memory/cases/sop-cases.jsonl",
        }
        for index in (1, 2)
    ]
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.MEMORY_REVIEW,
            state_version=1,
            title="SOP",
            final_result=FinalSopResult.model_validate(
                _final_result_payload(tmp_path),
            ),
            memory_candidates=candidates,
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-memory-review",
            command="confirm_outputs",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )

    with pytest.raises(WPlusCommandError, match="every unresolved"):
        await _send(
            service,
            session_id,
            "resolve_memory",
            {
                "decisions": [
                    {"candidate_id": "candidate-1", "decision": "approve"},
                ],
            },
            request_id="cmd-incomplete-memory",
        )
    assert service.store.get_session(session_id).projection.state_version == 1
    assert starts == []

    writing = await _send(
        service,
        session_id,
        "resolve_memory",
        {
            "decisions": [
                {"candidate_id": "candidate-1", "decision": "approve"},
                {"candidate_id": "candidate-2", "decision": "approve"},
            ],
        },
        request_id="cmd-batch-memory",
    )
    assert len(starts) == 1
    assert writing.record.projection.active_memory_candidate_ids == [
        "candidate-1",
        "candidate-2",
    ]
    assert len(starts[0]["payload"]["candidates"]) == 2

    result = service.append_agent_event(
        kind="memory_write_batch_result",
        payload={
            "results": [
                {
                    "candidate_id": "candidate-1",
                    "status": "succeeded",
                    "target_scope": "cases",
                    "target_file": "memory/cases/sop-cases.jsonl",
                    "result": "appended",
                    "script": "scripts/memory_store.py",
                },
                {
                    "candidate_id": "candidate-2",
                    "status": "failed",
                    "error_code": "store_failed",
                    "summary": "disk unavailable",
                    "script": "scripts/memory_store.py",
                },
            ],
        },
        event_key="memory-batch-result",
        trusted_sop_session_id=session_id,
        trusted_run_id=starts[0]["run_id"],
        trusted_attempt_id=starts[0]["attempt_id"],
    )
    assert result.record.projection.state is SessionState.MEMORY_REVIEW
    assert [
        candidate.status.value
        for candidate in result.record.projection.memory_candidates
    ] == [
        "approved",
        "failed",
    ]
    assert result.record.projection.active_memory_candidate_ids == []


@pytest.mark.asyncio
async def test_rejecting_all_memory_candidates_does_not_start_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    session_id = "sop-memory-reject-all"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.MEMORY_REVIEW,
            state_version=1,
            title="SOP",
            memory_candidates=[
                {
                    "candidate_id": "candidate-1",
                    "summary": "不保存规则",
                    "memory_type": "sop_case",
                    "value": {"pattern": "rule"},
                    "evidence": "用户审阅该脱敏规则。",
                    "target_scope": "cases",
                    "target_file": "memory/cases/sop-cases.jsonl",
                },
            ],
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-memory-review",
            command="confirm_outputs",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    completed = await _send(
        service,
        session_id,
        "resolve_memory",
        {"decisions": [{"candidate_id": "candidate-1", "decision": "reject"}]},
        request_id="cmd-reject-all",
    )
    assert completed.record.projection.state is SessionState.COMPLETED
    assert starts == []


@pytest.mark.asyncio
async def test_legacy_unwritable_memory_candidate_can_only_be_rejected(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-legacy-memory"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:legacy-miner",
            state=SessionState.MEMORY_REVIEW,
            state_version=7,
            title="Legacy SOP",
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-legacy-entry",
            command="confirm_entry",
            sop_session_id=session_id,
            resulting_state_version=7,
        ),
    )
    raw = json.loads(service.store.path.read_text(encoding="utf-8"))
    raw["sessions"][session_id]["projection"]["memory_candidates"] = [
        {
            "candidate_id": "legacy-failed",
            "summary": "旧版失败候选",
            "value": "旧版自由文本",
            "status": "failed",
        },
    ]
    service.store.path.write_text(
        json.dumps(raw, ensure_ascii=False),
        encoding="utf-8",
    )

    snapshot = serialize_session(service.get_session(session_id))
    assert snapshot["state"] == "MemoryReview"
    assert snapshot["memory_candidates"][0]["status"] == "failed"
    assert snapshot["memory_candidates"][0]["failure_reason"] is None
    assert snapshot["memory_candidates"][0]["legacy_read_only"] is True

    with pytest.raises(WPlusCommandError, match="read-only legacy"):
        await _send(
            service,
            session_id,
            "resolve_memory",
            {
                "decisions": [
                    {"candidate_id": "legacy-failed", "decision": "approve"},
                ],
            },
            request_id="cmd-approve-legacy",
        )
    unchanged = service.get_session(session_id)
    assert unchanged.projection.state is SessionState.MEMORY_REVIEW
    assert unchanged.projection.state_version == 7

    rejected = await _send(
        service,
        session_id,
        "resolve_memory",
        {
            "decisions": [
                {"candidate_id": "legacy-failed", "decision": "reject"},
            ],
        },
        request_id="cmd-reject-legacy",
    )
    assert rejected.record.projection.state is SessionState.COMPLETED
    assert (
        rejected.record.projection.memory_candidates[0].status.value
        == "rejected"
    )


@pytest.mark.asyncio
async def test_memory_batch_waits_for_prior_agent_then_starts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    tracker = service.workspace.task_tracker
    tracker.status = "running"
    tracker.idle_after_reads = 2
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        assert tracker.status == "idle"
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    session_id = "sop-memory-wait"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.MEMORY_REVIEW,
            state_version=1,
            title="SOP",
            memory_candidates=[
                {
                    "candidate_id": "candidate-1",
                    "summary": "保存规则",
                    "memory_type": "sop_case",
                    "value": {"pattern": "rule"},
                    "evidence": "用户确认该脱敏规则。",
                    "target_scope": "cases",
                    "target_file": "memory/cases/sop-cases.jsonl",
                },
            ],
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-memory-review",
            command="confirm_outputs",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    await _send(
        service,
        session_id,
        "resolve_memory",
        {
            "decisions": [
                {"candidate_id": "candidate-1", "decision": "approve"},
            ],
        },
        request_id="cmd-memory-wait",
    )
    assert tracker.status_reads >= 2
    assert len(starts) == 1


@pytest.mark.asyncio
async def test_memory_batch_idle_timeout_does_not_mutate_or_create_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    service.workspace.task_tracker.status = "running"
    monkeypatch.setattr(
        service_module,
        "_CHAT_IDLE_WAIT_TIMEOUT_SECONDS",
        0.001,
    )
    monkeypatch.setattr(service_module, "_CHAT_IDLE_POLL_SECONDS", 0.001)
    session_id = "sop-memory-timeout"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.MEMORY_REVIEW,
            state_version=1,
            title="SOP",
            memory_candidates=[
                {
                    "candidate_id": "candidate-1",
                    "summary": "保存规则",
                    "memory_type": "sop_case",
                    "value": {"pattern": "rule"},
                    "evidence": "用户确认该脱敏规则。",
                    "target_scope": "cases",
                    "target_file": "memory/cases/sop-cases.jsonl",
                },
            ],
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-memory-review",
            command="confirm_outputs",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    before = service.get_session(session_id)
    with pytest.raises(WPlusOwningChatFinalizingError):
        await _send(
            service,
            session_id,
            "resolve_memory",
            {
                "decisions": [
                    {"candidate_id": "candidate-1", "decision": "approve"},
                ],
            },
            request_id="cmd-memory-timeout",
        )
    after = service.get_session(session_id)
    assert after == before
    assert after.runs == []
    assert "cmd-memory-timeout" not in after.command_receipts


@pytest.mark.asyncio
async def test_question_generation_commands_forward_server_target_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建两环节 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-target-state",
        skill_snapshot_id="sha256:miner",
    )
    session_id = confirmed.record.projection.sop_session_id
    service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        event_key="target-state-stage-proposal",
    )

    await _send(
        service,
        session_id,
        "confirm_stage_queue",
        {
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        request_id="cmd-target-state-queue",
    )
    assert starts[-1]["command"] == "confirm_stage_queue"
    assert starts[-1]["target_state"] == "GeneratingQuestions"
    assert starts[-1]["payload"]["current_stage_id"] == "stage-1"

    service.append_agent_event(
        kind="question_batch",
        payload=_question_payload("stage-1", "target-state"),
        event_key="target-state-questions",
    )
    await _send(
        service,
        session_id,
        "submit_answers",
        {
            "answers": {"q-target-state": "yes"},
        },
        request_id="cmd-target-state-answers",
    )
    service.append_agent_event(
        kind="trial_plan",
        payload=_trial_plan_payload("target-state"),
        event_key="target-state-trial-plan",
    )
    service.append_agent_event(
        kind="trial_execution_completed",
        payload=_trial_result_payload("target-state"),
        event_key="target-state-trial-result",
    )
    await _send(
        service,
        session_id,
        "accept_trial",
        request_id="cmd-target-state-accept",
    )
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="target-state-stage-report",
    )
    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="cmd-target-state-next-stage",
    )
    service.append_agent_event(
        kind="cumulative_refreshed",
        payload=_cumulative_refreshed_payload(service.get_session(session_id)),
        event_key="target-state-cumulative",
    )

    assert starts[-1]["command"] == "confirm_stage"
    assert starts[-1]["target_state"] == "RefreshingCumulative"
    assert starts[-1]["payload"]["current_stage_id"] == "stage-2"
    assert [
        snapshot["stage_id"]
        for snapshot in starts[-1]["payload"]["confirmed_snapshots"]
    ] == ["stage-1"]


@pytest.mark.asyncio
async def test_resume_question_generation_forwards_server_target_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-resume-questions"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.PAUSED,
            state_version=1,
            title="SOP",
            stages=[Stage(stage_id="stage-1", name="确认范围")],
            current_stage_id="stage-1",
            resume_state=SessionState.GENERATING_QUESTIONS,
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-create-resume-questions",
            command="test_setup",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    captured: dict[str, Any] = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    await _send(
        service,
        session_id,
        "resume",
        request_id="cmd-resume-questions",
    )

    assert captured["target_state"] == "GeneratingQuestions"
    assert captured["payload"]["current_stage_id"] == "stage-1"


@pytest.mark.asyncio
async def test_retry_question_generation_forwards_server_target_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-retry-questions"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.RECOVERABLE_FAILURE,
            state_version=1,
            title="SOP",
            stages=[Stage(stage_id="stage-1", name="确认范围")],
            current_stage_id="stage-1",
            current_run_id="run-failed-questions",
            resume_state=SessionState.GENERATING_QUESTIONS,
            last_error={
                "error_code": "question_generation_failed",
                "summary": "生成问题失败",
                "failed_operation": "confirm_stage_queue",
                "failed_run_id": "run-failed-questions",
            },
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-create-retry-questions",
            command="test_setup",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
        run_attempt=RunAttempt(
            run_id="run-failed-questions",
            attempt_id="attempt-failed-questions",
            command_request_id="cmd-failed-questions",
            command="confirm_stage_queue",
            status=RunStatus.FAILED,
        ),
    )
    captured: dict[str, Any] = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    await _send(
        service,
        session_id,
        "retry_current_turn",
        request_id="cmd-retry-questions",
    )

    assert captured["target_state"] == "GeneratingQuestions"
    assert captured["payload"] == {
        "target_state": "GeneratingQuestions",
        "retry_of_run_id": "run-failed-questions",
        "current_stage_id": "stage-1",
    }


def test_wrong_event_reports_allowed_events_for_generating_questions(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _create_generation_run(
        service,
        session_id="sop-question-event-contract",
        created_at=datetime.now(timezone.utc),
        state=SessionState.GENERATING_QUESTIONS,
    )
    before = service.get_session("sop-question-event-contract")

    with pytest.raises(
        WPlusCommandError,
        match=(
            "allowed agent events: lifecycle_progress, question_batch, "
            "recoverable_failure"
        ),
    ):
        service.append_agent_event(
            kind="stage_queue_confirmed",
            payload={
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "name": "确认范围",
                        "status": "clarifying",
                    },
                    {"stage_id": "stage-2", "name": "生成结果"},
                ],
            },
            event_key="wrong-stage-queue-confirmed",
            trusted_sop_session_id="sop-question-event-contract",
            trusted_run_id="run-sop-question-event-contract",
            trusted_attempt_id="attempt-sop-question-event-contract",
        )

    assert service.get_session("sop-question-event-contract") == before


def test_question_batch_rejects_stage_id_mismatch_without_side_effects(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-question-stage-mismatch"
    _create_question_generation_run(service, session_id=session_id)
    before = service.get_session(session_id)
    outbox_before = service.store.pending_outbox()

    with pytest.raises(WPlusCommandError, match="current_stage_id=stage-1"):
        service.append_agent_event(
            kind="question_batch",
            payload=_question_payload("stage-2", "mismatch"),
            event_key="mismatched-question-batch",
            trusted_sop_session_id=session_id,
            trusted_run_id=f"run-{session_id}",
            trusted_attempt_id=f"attempt-{session_id}",
        )

    assert service.get_session(session_id) == before
    assert service.store.pending_outbox() == outbox_before


def test_failed_event_can_be_corrected_within_the_same_run_attempt(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-correct-question-event"
    _create_question_generation_run(service, session_id=session_id)
    trusted_identity = {
        "trusted_sop_session_id": session_id,
        "trusted_run_id": f"run-{session_id}",
        "trusted_attempt_id": f"attempt-{session_id}",
    }
    baseline = service.get_session(session_id)
    baseline_event_ids = {event.event_id for event in baseline.events}
    baseline_outbox_ids = {
        item.projection_event_id for item in service.store.pending_outbox()
    }

    with pytest.raises(WPlusCommandError, match="allowed agent events"):
        service.append_agent_event(
            kind="stage_queue_confirmed",
            payload={
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "name": "确认范围",
                        "status": "clarifying",
                    },
                    {"stage_id": "stage-2", "name": "生成结果"},
                ],
            },
            event_key="question-boundary",
            **trusted_identity,
        )

    accepted = service.append_agent_event(
        kind="question_batch",
        payload=_question_payload("stage-1", "corrected"),
        event_key="question-boundary",
        **trusted_identity,
    )

    assert accepted.record.projection.state is SessionState.AWAITING_ANSWER
    assert [
        event.kind.value
        for event in accepted.record.events
        if event.event_id not in baseline_event_ids
    ] == [
        "question_batch",
    ]
    assert [
        item.kind
        for item in service.store.pending_outbox()
        if item.projection_event_id not in baseline_outbox_ids
    ] == [
        "question_batch",
    ]


@pytest.mark.asyncio
async def test_historical_question_event_is_not_duplicate_in_next_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建两环节 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-historical-question",
        skill_snapshot_id="sha256:miner",
    )
    session_id = confirmed.record.projection.sop_session_id
    service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        event_key="historical-question-stages",
    )
    await _send(
        service,
        session_id,
        "confirm_stage_queue",
        {
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        request_id="cmd-historical-question-queue",
    )
    stage_one_payload = _question_payload("stage-1", "historical")
    first = service.append_agent_event(
        kind="question_batch",
        payload=stage_one_payload,
        event_key="historical-question-key",
    )
    awaiting_answer_replay = service.append_agent_event(
        kind="question_batch",
        payload=stage_one_payload,
        event_key="historical-question-key",
    )

    assert first.duplicate is False
    assert awaiting_answer_replay.duplicate is True
    assert len(awaiting_answer_replay.record.events) == len(
        first.record.events,
    )

    await _send(
        service,
        session_id,
        "submit_answers",
        {"answers": {"q-historical": "yes"}},
        request_id="cmd-historical-question-answers",
    )
    service.append_agent_event(
        kind="trial_plan",
        payload=_trial_plan_payload("historical-question"),
        event_key="historical-question-trial-plan",
    )
    service.append_agent_event(
        kind="trial_execution_completed",
        payload=_trial_result_payload("historical-question"),
        event_key="historical-question-trial-result",
    )
    await _send(
        service,
        session_id,
        "accept_trial",
        request_id="cmd-historical-question-accept",
    )
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="historical-question-stage-report",
    )
    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="cmd-historical-question-next-stage",
    )
    service.append_agent_event(
        kind="cumulative_refreshed",
        payload=_cumulative_refreshed_payload(service.get_session(session_id)),
        event_key="historical-question-cumulative",
    )
    before = service.get_session(session_id)
    outbox_before = service.store.pending_outbox()

    with pytest.raises(WPlusCommandError, match="current_stage_id=stage-2"):
        service.append_agent_event(
            kind="question_batch",
            payload=stage_one_payload,
            event_key="historical-question-key",
        )

    assert service.get_session(session_id) == before
    assert service.store.pending_outbox() == outbox_before


@pytest.mark.asyncio
async def test_pending_exit_settles_at_next_structured_event_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry",
        skill_snapshot_id="sha256:miner",
    )
    session_id = confirmed.record.projection.sop_session_id

    pending = await _send(
        service,
        session_id,
        "save_and_exit",
        request_id="cmd-exit",
    )
    assert pending.record.projection.state is SessionState.PENDING_EXIT

    paused = service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "创建任务"},
            ],
        },
        event_key="safe-boundary",
    )
    assert paused.record.projection.state is SessionState.PAUSED
    assert paused.record.projection.resume_state is (
        SessionState.AWAITING_QUEUE_CONFIRMATION
    )
    with pytest.raises(WPlusCommandError):
        service.append_agent_event(
            kind="question_batch",
            payload=_question_payload("stage-1", "late"),
            event_key="late-event-from-paused-run",
        )

    resumed = await _send(
        service,
        session_id,
        "resume",
        request_id="cmd-resume",
    )
    assert resumed.record.projection.state is (
        SessionState.AWAITING_QUEUE_CONFIRMATION
    )


@pytest.mark.asyncio
async def test_pending_exit_rejects_duplicate_exit_and_supports_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-controls",
        skill_snapshot_id="sha256:miner",
    )
    session_id = confirmed.record.projection.sop_session_id
    await _send(
        service,
        session_id,
        "save_and_exit",
        request_id="cmd-exit-controls",
    )

    with pytest.raises(WPlusCommandError, match="pending exit"):
        await _send(
            service,
            session_id,
            "save_and_exit",
            request_id="cmd-exit-again",
        )
    assert (
        service.get_session(session_id).projection.resume_state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )

    waiting = await _send(
        service,
        session_id,
        "continue_waiting",
        request_id="cmd-wait",
    )
    assert waiting.record.projection.state is SessionState.PENDING_EXIT
    assert (
        waiting.record.projection.resume_state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )

    service.workspace.task_tracker.status = "running"
    paused = await _send(
        service,
        session_id,
        "cancel_run_and_pause",
        request_id="cmd-cancel-pause",
    )
    assert paused.record.projection.state is SessionState.PAUSED
    assert (
        paused.record.projection.resume_state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert service.workspace.task_tracker.stops == 1
    assert service.get_session(session_id).runs[0].status.value == "cancelled"


def test_same_text_creates_a_new_proposal_after_resolution(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = service.create_entry_proposal(
        original_text="创建相同 SOP",
        mode="explicit",
    )
    service.reject_entry(
        proposal_id=first.proposal_id,
        command_request_id="cmd-reject-first",
    )

    second = service.create_entry_proposal(
        original_text="创建相同 SOP",
        mode="explicit",
    )
    duplicate_pending = service.create_entry_proposal(
        original_text="创建相同 SOP",
        mode="explicit",
    )

    assert second.proposal_id != first.proposal_id
    assert duplicate_pending.proposal_id == second.proposal_id
    assert duplicate_pending.status.value == "pending"


@pytest.mark.asyncio
async def test_agent_event_retry_is_idempotent_after_state_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-event-retry",
        skill_snapshot_id="sha256:miner",
    )
    payload = {
        "stages": [
            {"stage_id": "stage-1", "name": "确认范围"},
            {"stage_id": "stage-2", "name": "生成结果"},
        ],
    }

    first = service.append_agent_event(
        kind="stage_proposal",
        payload=payload,
        event_key="stable-stage-proposal",
    )
    duplicate = service.append_agent_event(
        kind="stage_proposal",
        payload=payload,
        event_key="stable-stage-proposal",
    )

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert len(duplicate.record.events) == len(first.record.events)


@pytest.mark.asyncio
async def test_stage_proposal_rejects_non_pending_stage_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-invalid-stage-status",
        skill_snapshot_id="sha256:miner",
    )
    before = confirmed.record.projection.state_version

    with pytest.raises(
        WPlusCommandError,
        match="stages must start as pending",
    ):
        service.append_agent_event(
            kind="stage_proposal",
            payload={
                "stages": [
                    {
                        "stage_id": "stage-1",
                        "name": "确认范围",
                        "status": "clarifying",
                    },
                    {
                        "stage_id": "stage-2",
                        "name": "生成结果",
                        "status": "pending",
                    },
                ],
            },
            event_key="invalid-stage-status",
        )

    record = service.get_session(
        confirmed.record.projection.sop_session_id,
    )
    assert record.projection.state_version == before
    assert record.projection.state is SessionState.GENERATING_STAGE_PROPOSAL


@pytest.mark.asyncio
async def test_concurrent_outbox_flush_keeps_the_complete_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-outbox",
        skill_snapshot_id="sha256:miner",
    )
    service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        event_key="outbox-stage",
    )
    peer = WPlusSopService(
        workspace=service.workspace,
        ownership=_ownership(),
        store=WPlusSopStore(tmp_path / "wplus-sop.json"),
    )

    await asyncio.gather(
        service.flush_chat_projection_outbox(),
        peer.flush_chat_projection_outbox(),
    )

    audit = service.workspace.chat_manager.chat.meta["wplus_sop_audit"]
    assert len(audit) == 2
    assert len({item["projection_event_id"] for item in audit}) == 2
    assert service.store.pending_outbox() == []


@pytest.mark.asyncio
async def test_outbox_get_chat_failure_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    async def fail_get_chat(_chat_id: str):
        raise RuntimeError("temporary Chat storage failure")

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-outbox-failure",
        skill_snapshot_id="sha256:miner",
    )
    monkeypatch.setattr(
        service.workspace.chat_manager,
        "get_chat",
        fail_get_chat,
    )

    assert await service.flush_chat_projection_outbox() == 0
    assert len(service.store.pending_outbox()) == 1


@pytest.mark.asyncio
async def test_memory_candidates_require_final_sop_result(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store.create_session(
        SessionProjection(
            sop_session_id="sop-finalizing",
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.FINALIZING_OUTPUTS,
            state_version=1,
            title="SOP",
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-finalizing",
            command="confirm_entry",
            sop_session_id="sop-finalizing",
            resulting_state_version=1,
        ),
    )

    with pytest.raises(WPlusCommandError):
        service.append_agent_event(
            kind="memory_candidates",
            payload={"candidates": []},
            event_key="misordered-memory",
        )

    assert (
        service.get_session("sop-finalizing").projection.state
        is SessionState.FINALIZING_OUTPUTS
    )


def test_agent_cannot_forge_memory_approval_or_write_receipt(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store.create_session(
        SessionProjection(
            sop_session_id="sop-forged-memory",
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.FINALIZING_OUTPUTS,
            state_version=1,
            title="SOP",
            final_result=FinalSopResult.model_validate(
                _final_result_payload(tmp_path),
            ),
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-finalizing",
            command="confirm_stage",
            sop_session_id="sop-forged-memory",
            resulting_state_version=1,
        ),
    )

    with pytest.raises(WPlusCommandError, match="must be pending"):
        service.append_agent_event(
            kind="memory_candidates",
            payload={
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "summary": "伪造批准",
                        "memory_type": "common_wplus_knowledge",
                        "value": {"rule": "规则"},
                        "evidence": "用户确认该规则。",
                        "target_scope": "common",
                        "target_file": "memory/common-wplus-knowledge.jsonl",
                        "status": "approved",
                        "write_receipt": {
                            "memory_id": "wplus-sop/forged/candidate-1",
                            "target_scope": "common",
                            "target_file": "memory/common-wplus-knowledge.jsonl",
                            "written_at": "2026-08-04T10:00:00Z",
                            "reused_existing": False,
                            "store_result": "appended",
                        },
                    },
                ],
            },
            event_key="forged-memory",
        )

    with pytest.raises(WPlusCommandError, match="must be pending"):
        service.append_agent_event(
            kind="memory_candidates",
            payload={
                "candidates": [
                    {
                        "candidate_id": "candidate-legacy-forgery",
                        "summary": "伪造旧版只读标记",
                        "memory_type": "common_wplus_knowledge",
                        "value": {"rule": "规则"},
                        "evidence": "用户确认该规则。",
                        "status": "pending",
                        "legacy_read_only": True,
                    },
                ],
            },
            event_key="forged-legacy-memory",
        )


def test_user_memory_candidate_requires_caller_anonymous_scope(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store.create_session(
        SessionProjection(
            sop_session_id="sop-user-memory-no-scope",
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.FINALIZING_OUTPUTS,
            state_version=1,
            title="SOP",
            final_result=FinalSopResult.model_validate(
                _final_result_payload(tmp_path),
            ),
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-finalizing-no-scope",
            command="confirm_stage",
            sop_session_id="sop-user-memory-no-scope",
            resulting_state_version=1,
        ),
    )

    with pytest.raises(WPlusCommandError, match="anonymous user_scope"):
        service.append_agent_event(
            kind="memory_candidates",
            payload={
                "candidates": [
                    {
                        "candidate_id": "candidate-user",
                        "summary": "保存个人检查偏好",
                        "memory_type": "user_wplus_usage",
                        "value": {"preference": "先查看资产变化"},
                        "evidence": "用户确认这是其个人工作偏好。",
                    },
                ],
            },
            event_key="user-memory-no-scope",
        )


def test_user_memory_candidate_uses_persisted_anonymous_scope(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store.create_session(
        SessionProjection(
            sop_session_id="sop-user-memory-with-scope",
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.FINALIZING_OUTPUTS,
            state_version=1,
            title="SOP",
            memory_user_scope="anon_scope_123456",
            final_result=FinalSopResult.model_validate(
                _final_result_payload(tmp_path),
            ),
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-finalizing-with-scope",
            command="confirm_stage",
            sop_session_id="sop-user-memory-with-scope",
            resulting_state_version=1,
        ),
    )

    generated = service.append_agent_event(
        kind="memory_candidates",
        payload={
            "candidates": [
                {
                    "candidate_id": "candidate-user",
                    "summary": "保存个人检查偏好",
                    "memory_type": "user_wplus_usage",
                    "value": {"preference": "先查看资产变化"},
                    "evidence": "用户确认这是其个人工作偏好。",
                },
            ],
        },
        event_key="user-memory-with-scope",
    )

    candidate = generated.record.projection.memory_candidates[0]
    assert candidate.target_scope == "user"
    assert candidate.target_file == (
        "memory/users/anon_scope_123456/wplus-usage-preferences.jsonl"
    )


@pytest.mark.asyncio
async def test_revise_answer_invalidates_downstream_and_starts_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    starts: list[dict] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-revision",
        skill_snapshot_id="sha256:miner",
    )
    session_id = confirmed.record.projection.sop_session_id
    service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        event_key="revision-stages",
    )
    await _send(
        service,
        session_id,
        "confirm_stage_queue",
        {
            "stages": [
                {"stage_id": "stage-1", "title": "确认范围"},
                {"stage_id": "stage-2", "title": "生成结果"},
            ],
        },
        request_id="cmd-revision-queue",
    )
    revision_questions = _question_payload("stage-1", "revision")
    revision_questions["questions"][0]["options"][1][
        "requires_custom_input"
    ] = True
    service.append_agent_event(
        kind="question_batch",
        payload=revision_questions,
        event_key="revision-questions",
    )
    await _send(
        service,
        session_id,
        "submit_answers",
        {
            "answers": {"q-revision": "yes"},
        },
        request_id="cmd-original-answer",
    )
    service.append_agent_event(
        kind="trial_plan",
        payload=_trial_plan_payload("trial-revision"),
        event_key="revision-plan",
    )
    revision_result = _trial_result_payload("trial-revision")
    revision_result.update(
        {
            "confirmed_facts": ["旧范围事实"],
            "unknowns": ["旧范围未知项"],
        },
    )
    service.append_agent_event(
        kind="trial_execution_completed",
        payload=revision_result,
        event_key="revision-result",
    )

    before_revision = service.get_session(session_id).projection.state_version
    with pytest.raises(WPlusCommandError, match="custom input"):
        await _send(
            service,
            session_id,
            "revise_answer",
            {
                "revised_round": 1,
                "answers": {
                    "q-revision": {
                        "selected_option_ids": ["no"],
                        "text": "   ",
                    },
                },
            },
            request_id="cmd-invalid-custom-revision",
        )
    assert (
        service.get_session(session_id).projection.state_version
        == before_revision
    )

    revised = await _send(
        service,
        session_id,
        "revise_answer",
        {
            "revised_round": 1,
            "answers": {
                "q-revision": {
                    "selected_option_ids": ["no"],
                    "text": "改为人工复核",
                },
            },
            "reason": "范围发生变化",
        },
        request_id="cmd-revise-answer",
    )

    projection = revised.record.projection
    assert projection.state is SessionState.GENERATING_QUESTIONS
    assert projection.revision == 2
    assert projection.round == 1
    assert projection.answers[0].answers[0].selected_option_ids == ["no"]
    assert projection.answers[0].answers[0].text == "改为人工复核"
    assert projection.trial_result_lists == []
    assert projection.confirmed_facts == []
    assert projection.unknowns == []
    assert projection.invalidated_history[0]["revised_round"] == 1
    assert starts[-1]["command"] == "revise_answer"


@pytest.mark.asyncio
async def test_outbox_projects_every_audit_item_before_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)

    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-audit",
        skill_snapshot_id="sha256:miner",
    )
    service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        event_key="audit-stage-proposal",
    )

    assert len(service.store.pending_outbox()) == 2
    assert await service.flush_chat_projection_outbox() == 2
    audit = service.workspace.chat_manager.chat.meta["wplus_sop_audit"]
    assert [item["kind"] for item in audit] == [
        "session_state_changed",
        "stage_proposal",
    ]
    assert service.store.pending_outbox() == []


@pytest.mark.asyncio
async def test_agent_completion_without_boundary_becomes_recoverable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    captured: dict[str, Any] = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id="cmd-entry-no-event",
        skill_snapshot_id="sha256:miner",
    )

    await captured["on_complete"]()

    record = service.get_session(
        confirmed.record.projection.sop_session_id,
    )
    assert record.projection.state is SessionState.RECOVERABLE_FAILURE
    assert (
        record.projection.resume_state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert record.projection.last_error.failed_run_id == captured["run_id"]
    assert record.runs[0].status.value == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        SessionState.GENERATING_STAGE_REPORT,
        SessionState.REFRESHING_CUMULATIVE,
    ],
)
async def test_incremental_generation_requires_boundary_before_completion(
    tmp_path: Path,
    state: SessionState,
) -> None:
    service = _service(tmp_path)
    session_id = f"sop-incomplete-{state.value}"
    _create_generation_run(
        service,
        session_id=session_id,
        created_at=datetime.now(timezone.utc),
        state=state,
        status=RunStatus.RUNNING,
    )
    attempt = service.get_session(session_id).runs[0]

    await service._on_agent_turn_complete(
        sop_session_id=session_id,
        run_id=attempt.run_id,
        attempt_id=attempt.attempt_id,
        command=attempt.command,
    )

    record = service.get_session(session_id)
    assert record.projection.state is SessionState.RECOVERABLE_FAILURE
    assert record.projection.resume_state is state
    assert record.projection.last_error is not None
    assert record.projection.last_error.failed_run_id == attempt.run_id
    assert record.runs[0].status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_runtime_start_failure_can_retry_from_server_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )

    async def fail_start(**_kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fail_start)
    with pytest.raises(WPlusRuntimeStartError, match="runtime unavailable"):
        await service.confirm_entry(
            proposal_id=proposal.proposal_id,
            command_request_id="cmd-entry-start-failure",
            skill_snapshot_id="sha256:miner",
        )

    failed = service.get_active_session()
    assert failed is not None
    assert failed.projection.state is SessionState.RECOVERABLE_FAILURE
    assert (
        failed.projection.resume_state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert failed.runs[0].status is RunStatus.FAILED

    captured: dict[str, Any] = {}

    async def succeed_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(
        service_module,
        "start_wplus_chat_turn",
        succeed_start,
    )
    retried = await _send(
        service,
        failed.projection.sop_session_id,
        "retry_current_turn",
        {
            "target_state": "FinalizingOutputs",
            "retry_of_run_id": "forged",
        },
        request_id="cmd-retry-start-failure",
    )

    assert (
        retried.record.projection.state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert captured["payload"] == {
        "target_state": "GeneratingStageProposal",
        "retry_of_run_id": failed.runs[0].run_id,
    }


@pytest.mark.asyncio
async def test_retry_current_turn_allows_source_id_to_differ_from_chat_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    service.ownership = _ownership().model_copy(
        update={"source_id": "external-source-1"},
    )
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )

    async def fail_start(**_kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fail_start)
    with pytest.raises(WPlusRuntimeStartError, match="runtime unavailable"):
        await service.confirm_entry(
            proposal_id=proposal.proposal_id,
            command_request_id="cmd-entry-external-source-retry",
            skill_snapshot_id="sha256:miner",
        )

    failed = service.get_active_session()
    assert failed is not None
    assert failed.projection.state is SessionState.RECOVERABLE_FAILURE
    assert service.workspace.chat_manager.chat.channel == "console"
    failed_run = failed.runs[0]
    captured: dict[str, Any] = {}

    async def succeed_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(
        service_module,
        "start_wplus_chat_turn",
        succeed_start,
    )
    retried = await _send(
        service,
        failed.projection.sop_session_id,
        "retry_current_turn",
        request_id="cmd-retry-external-source",
    )

    assert captured["source_id"] == "external-source-1"
    assert captured["command"] == "retry_current_turn"
    assert captured["payload"] == {
        "target_state": "GeneratingStageProposal",
        "retry_of_run_id": failed_run.run_id,
    }
    assert (
        retried.record.projection.state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert len(retried.record.runs) == 2
    assert retried.record.runs[0].status is RunStatus.FAILED
    assert retried.record.runs[1].retry_of_run_id == failed_run.run_id
    assert retried.record.runs[1].run_id == captured["run_id"]


@pytest.mark.asyncio
async def test_confirm_retry_replays_runtime_failure_without_duplicate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    proposal = service.create_entry_proposal(
        original_text="创建 SOP",
        mode="explicit",
    )
    starts: list[dict[str, Any]] = []

    async def fail_start(**kwargs):
        starts.append(kwargs)
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fail_start)
    command_request_id = "cmd-entry-idempotent-runtime-failure"
    with pytest.raises(WPlusRuntimeStartError, match="runtime unavailable"):
        await service.confirm_entry(
            proposal_id=proposal.proposal_id,
            command_request_id=command_request_id,
            skill_snapshot_id="sha256:miner",
        )

    failed = service.get_active_session()
    assert failed is not None
    original_receipt = failed.command_receipts[command_request_id]
    original_outbox = service.store.pending_outbox()

    replayed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id=command_request_id,
        skill_snapshot_id="sha256:miner",
    )

    assert replayed.duplicate is True
    assert replayed.receipt == original_receipt
    assert (
        replayed.record.projection.sop_session_id
        == failed.projection.sop_session_id
    )
    assert len(starts) == 1
    sessions = service.store.list_sessions()
    assert len(sessions) == 1
    assert len(sessions[0].runs) == 1
    assert service.store.pending_outbox() == original_outbox
    assert len(
        {item.projection_event_id for item in original_outbox},
    ) == len(original_outbox)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        SessionState.GENERATING_STAGE_PROPOSAL,
        SessionState.GENERATING_QUESTIONS,
        SessionState.GENERATING_TRIAL,
        SessionState.EXECUTING_TRIAL,
        SessionState.FINALIZING_OUTPUTS,
    ],
)
@pytest.mark.parametrize(
    "status",
    [RunStatus.CLAIMED, RunStatus.RUNNING],
)
async def test_idle_orphaned_generation_run_becomes_retryable_once(
    tmp_path: Path,
    state: SessionState,
    status: RunStatus,
) -> None:
    service = _service(tmp_path)
    _create_generation_run(
        service,
        session_id="sop-orphan",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        state=state,
        status=status,
    )

    recovered = await service.recover_orphaned_generation_run("sop-orphan")
    duplicate = await service.recover_orphaned_generation_run("sop-orphan")

    assert recovered is not None
    assert duplicate is None
    record = service.get_session("sop-orphan")
    assert record.projection.state is SessionState.RECOVERABLE_FAILURE
    assert record.projection.resume_state is state
    assert record.projection.last_error.error_code == "orphaned_agent_run"
    assert record.projection.last_error.failed_run_id == "run-sop-orphan"
    assert record.runs[0].status is RunStatus.FAILED
    assert [event.kind.value for event in record.events] == [
        "session_state_changed",
        "recoverable_failure",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tracker_status", "is_fresh"),
    [
        ("running", False),
        ("idle", True),
    ],
)
async def test_active_or_fresh_generation_run_is_not_recovered(
    tmp_path: Path,
    tracker_status: str,
    is_fresh: bool,
) -> None:
    service = _service(tmp_path)
    service.workspace.task_tracker.status = tracker_status
    now = datetime.now(timezone.utc)
    _create_generation_run(
        service,
        session_id="sop-active-or-fresh",
        created_at=(now if is_fresh else now - timedelta(minutes=1)),
    )

    recovered = await service.recover_orphaned_generation_run(
        "sop-active-or-fresh",
    )

    assert recovered is None
    record = service.get_session("sop-active-or-fresh")
    assert record.projection.state is SessionState.GENERATING_STAGE_PROPOSAL
    assert record.runs[0].status is RunStatus.CLAIMED


@pytest.mark.asyncio
async def test_pending_exit_orphan_pauses_into_retryable_failure(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _create_generation_run(
        service,
        session_id="sop-pending-exit-orphan",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    pending = await _send(
        service,
        "sop-pending-exit-orphan",
        "save_and_exit",
        request_id="cmd-save-orphan",
    )
    assert pending.record.projection.state is SessionState.PENDING_EXIT

    recovered = await service.recover_orphaned_generation_run(
        "sop-pending-exit-orphan",
    )

    assert recovered is not None
    record = recovered.record
    assert record.projection.state is SessionState.PAUSED
    assert record.projection.resume_state is SessionState.RECOVERABLE_FAILURE
    assert record.projection.last_error.error_code == "orphaned_agent_run"
    assert record.projection.pending_exit_action is None
    assert record.runs[0].status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_retry_uses_only_server_owned_target_and_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    captured: dict[str, Any] = {}
    _create_generation_run(
        service,
        session_id="sop-server-owned-retry",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await service.recover_orphaned_generation_run("sop-server-owned-retry")

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    retried = await _send(
        service,
        "sop-server-owned-retry",
        "retry_current_turn",
        {
            "target_state": "FinalizingOutputs",
            "retry_of_run_id": "run-forged",
        },
        request_id="cmd-server-owned-retry",
    )

    assert (
        retried.record.projection.state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert captured["payload"] == {
        "target_state": "GeneratingStageProposal",
        "retry_of_run_id": "run-sop-server-owned-retry",
    }
    assert (
        service.get_session("sop-server-owned-retry").runs[-1].retry_of_run_id
        == "run-sop-server-owned-retry"
    )


@pytest.mark.asyncio
async def test_finalizing_retry_reports_when_sop_result_is_already_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    captured: dict[str, Any] = {}
    session_id = "sop-finalizing-retry"
    failed_run_id = "run-finalizing-failed"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.RECOVERABLE_FAILURE,
            state_version=1,
            title="SOP",
            current_run_id=failed_run_id,
            resume_state=SessionState.FINALIZING_OUTPUTS,
            last_error=RecoverableFailurePayload(
                error_code="agent_turn_incomplete",
                summary="最终产出未完成",
                failed_operation="confirm_stage",
                failed_run_id=failed_run_id,
            ),
            final_result=FinalSopResult.model_validate(
                _final_result_payload(tmp_path),
            ),
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-finalizing-original",
            command="confirm_stage",
            sop_session_id=session_id,
            resulting_state_version=1,
            starts_run=True,
            run_id=failed_run_id,
            attempt_id="attempt-finalizing-failed",
        ),
        run_attempt=RunAttempt(
            run_id=failed_run_id,
            attempt_id="attempt-finalizing-failed",
            command_request_id="cmd-finalizing-original",
            command="confirm_stage",
            status=RunStatus.FAILED,
        ),
    )

    async def fake_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    await _send(
        service,
        session_id,
        "retry_current_turn",
        {"final_result_persisted": False},
        request_id="cmd-finalizing-retry",
    )

    assert captured["payload"] == {
        "target_state": "FinalizingOutputs",
        "retry_of_run_id": failed_run_id,
        "final_result_persisted": True,
        "memory_user_scope_available": False,
    }


@pytest.mark.asyncio
async def test_orphan_recovery_fails_closed_on_stale_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _create_generation_run(
        service,
        session_id="sop-stale",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    def stale_commit(*_args, **_kwargs):
        raise StaleStateVersionError("settled concurrently")

    monkeypatch.setattr(service.store, "commit_event", stale_commit)

    assert await service.recover_orphaned_generation_run("sop-stale") is None
    record = service.get_session("sop-stale")
    assert (
        record.projection.state
        is SessionState.GENERATING_STAGE_PROPOSAL
    )
    assert record.runs[0].status is RunStatus.CLAIMED


async def _new_two_stage_session(
    service: WPlusSopService,
    session_id: str,
) -> str:
    proposal = service.create_entry_proposal(
        original_text="创建两环节 SOP",
        mode="explicit",
    )
    confirmed = await service.confirm_entry(
        proposal_id=proposal.proposal_id,
        command_request_id=f"cmd-entry-{session_id}",
        skill_snapshot_id="sha256:miner",
    )
    sid = confirmed.record.projection.sop_session_id
    service.append_agent_event(
        kind="stage_proposal",
        payload={
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        event_key=f"stages-{session_id}",
    )
    await _send(
        service,
        sid,
        "confirm_stage_queue",
        {
            "stages": [
                {"stage_id": "stage-1", "name": "确认范围"},
                {"stage_id": "stage-2", "name": "生成结果"},
            ],
        },
        request_id=f"cmd-queue-{session_id}",
    )
    return sid


async def _advance_to_stage_report(
    service: WPlusSopService,
    session_id: str,
    stage_id: str,
    suffix: str,
) -> None:
    service.append_agent_event(
        kind="question_batch",
        payload=_question_payload(stage_id, suffix),
        event_key=f"questions-{suffix}",
    )
    await _send(
        service,
        session_id,
        "submit_answers",
        {"answers": {f"q-{suffix}": "yes"}},
        request_id=f"cmd-answers-{suffix}",
    )
    service.append_agent_event(
        kind="trial_plan",
        payload=_trial_plan_payload(suffix),
        event_key=f"trial-plan-{suffix}",
    )
    service.append_agent_event(
        kind="trial_execution_completed",
        payload=_trial_result_payload(suffix),
        event_key=f"trial-result-{suffix}",
    )
    await _send(
        service,
        session_id,
        "accept_trial",
        request_id=f"cmd-accept-{suffix}",
    )


@pytest.mark.asyncio
async def test_cumulative_handoff_start_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        if kwargs["command"] == "continue_after_cumulative":
            raise RuntimeError("continuation unavailable")
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "handoff-failure")
    await _advance_to_stage_report(
        service,
        session_id,
        "stage-1",
        "handoff-failure",
    )
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="handoff-failure-report",
    )
    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="handoff-failure-confirm",
    )
    refresh_start = starts[-1]
    service.append_agent_event(
        kind="cumulative_refreshed",
        payload=_cumulative_refreshed_payload(service.get_session(session_id)),
        event_key="handoff-failure-cumulative",
    )

    await refresh_start["on_complete"]()

    record = service.get_session(session_id)
    assert record.projection.state is SessionState.RECOVERABLE_FAILURE
    assert record.projection.resume_state is SessionState.GENERATING_QUESTIONS
    assert record.projection.last_error is not None
    assert record.projection.last_error.failed_operation == (
        "continue_after_cumulative"
    )
    refresh_run = next(
        run for run in record.runs if run.run_id == refresh_start["run_id"]
    )
    continuation_run = next(
        run
        for run in record.runs
        if run.command == "continue_after_cumulative"
    )
    assert refresh_run.status is RunStatus.COMPLETED
    assert continuation_run.status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_confirm_stage_rejects_without_acceptable_report(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session_id = "sop-gate-no-report"
    service.store.create_session(
        SessionProjection(
            sop_session_id=session_id,
            ownership=_ownership(),
            skill_snapshot_id="sha256:miner",
            state=SessionState.AWAITING_STAGE_CONFIRMATION,
            state_version=1,
            title="SOP",
            stages=[
                Stage(stage_id="stage-1", name="确认范围"),
                Stage(stage_id="stage-2", name="生成结果"),
            ],
            current_stage_id="stage-1",
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-create-gate",
            command="test_setup",
            sop_session_id=session_id,
            resulting_state_version=1,
        ),
    )
    with pytest.raises(WPlusCommandError, match="no acceptable report"):
        await _send(
            service,
            session_id,
            "confirm_stage",
            request_id="cmd-gate-confirm",
        )


@pytest.mark.asyncio
async def test_stage_report_version_must_increment_by_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "version")
    await _advance_to_stage_report(service, session_id, "stage-1", "version")

    with pytest.raises(WPlusCommandError, match="increment by one"):
        service.append_agent_event(
            kind="stage_report_generated",
            payload=_stage_report_payload("stage-1", 2),
            event_key="version-skip-report",
        )


@pytest.mark.asyncio
async def test_stage_report_generation_failed_enters_recoverable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "fail")
    await _advance_to_stage_report(service, session_id, "stage-1", "fail")

    failed = service.append_agent_event(
        kind="stage_report_generation_failed",
        payload={
            "stage_id": "stage-1",
            "error_code": "render_failed",
            "summary": "环节报告渲染失败",
        },
        event_key="fail-report",
    )
    assert (
        failed.record.projection.state
        is SessionState.RECOVERABLE_FAILURE
    )
    assert (
        failed.record.projection.last_error.failed_operation
        == "stage_report_generation"
    )
    assert (
        failed.record.projection.last_error.error_code
        == "render_failed"
    )
    assert (
        failed.record.projection.resume_state
        is SessionState.GENERATING_STAGE_REPORT
    )


@pytest.mark.asyncio
async def test_serialize_session_exposes_stage_reports_and_cumulative_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "serialize")

    for index, stage_id in enumerate(("stage-1", "stage-2"), start=1):
        await _advance_to_stage_report(
            service,
            session_id,
            stage_id,
            f"serialize-{index}",
        )
        service.append_agent_event(
            kind="stage_report_generated",
            payload=_stage_report_payload(stage_id, 1),
            event_key=f"serialize-report-{index}",
        )
        await _send(
            service,
            session_id,
            "confirm_stage",
            request_id=f"cmd-serialize-confirm-{index}",
        )
        service.append_agent_event(
            kind="cumulative_refreshed",
            payload=_cumulative_refreshed_payload(
                service.get_session(session_id),
                preview_version=index,
            ),
            event_key=f"serialize-cumulative-{index}",
        )
        refresh_start = starts[-1]
        assert refresh_start["target_state"] == "RefreshingCumulative"
        await refresh_start["on_complete"]()

    record = service.get_session(session_id)
    snapshot = serialize_session(record)
    assert [
        report["stage_id"] for report in snapshot["stage_reports"]
    ] == ["stage-1", "stage-2"]
    assert [report["report_no"] for report in snapshot["stage_reports"]] == [
        1,
        1,
    ]
    assert snapshot["cumulative_preview"]["preview_version"] == 2
    assert snapshot["cumulative_preview"]["stage_order"] == [
        "stage-1",
        "stage-2",
    ]
    stage_download_url = snapshot["stage_reports"][0]["artifacts"][0][
        "download_url"
    ]
    assert stage_download_url == (
        f"/api/wplus-sop/sessions/{session_id}/stage-report-artifacts/"
        "stage_sop_json?stage_id=stage-1&revision=1&report_no=1"
        "&download=true"
    )
    cumulative_download_url = snapshot["cumulative_preview"]["artifacts"][0][
        "download_url"
    ]
    assert cumulative_download_url == (
        f"/api/wplus-sop/sessions/{session_id}/cumulative-artifacts/"
        "stage_sop_json?preview_version=2&download=true"
    )
    assert "static_url" not in json.dumps(snapshot)
    assert (
        snapshot["cumulative_preview"]["snapshots"][0]["stage_id"]
        == "stage-1"
    )
    assert record.projection.state is SessionState.FINALIZING_OUTPUTS


@pytest.mark.asyncio
async def test_ae1_report_generation_failure_blocks_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "ae1")
    await _advance_to_stage_report(service, session_id, "stage-1", "ae1")

    failed = service.append_agent_event(
        kind="stage_report_generation_failed",
        payload={
            "stage_id": "stage-1",
            "error_code": "render_failed",
            "summary": "环节报告渲染失败",
        },
        event_key="ae1-report-failed",
    )
    assert failed.record.projection.state is SessionState.RECOVERABLE_FAILURE
    with pytest.raises(WPlusCommandError, match="not awaiting confirmation"):
        await _send(
            service,
            session_id,
            "confirm_stage",
            request_id="ae1-confirm",
        )


@pytest.mark.asyncio
async def test_ae2_rerun_supersedes_report_versions_and_locks_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "ae2")
    await _advance_to_stage_report(service, session_id, "stage-1", "ae2")

    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="ae2-report-1",
    )
    assert len(service.get_session(session_id).projection.stage_reports) == 1
    # user feedback triggers a rerun; the rerun produces report v2 (R4)
    await _send(
        service,
        session_id,
        "submit_trial_feedback",
        {
            "feedback": "调整数据口径后重新预跑",
            "rerun_of_run_id": service.get_session(
                session_id,
            ).projection.current_run_id,
        },
        request_id="ae2-rerun",
    )
    service.append_agent_event(
        kind="trial_plan",
        payload=_trial_plan_payload("ae2-rerun"),
        event_key="ae2-rerun-plan",
    )
    service.append_agent_event(
        kind="trial_execution_completed",
        payload=_trial_result_payload("ae2-rerun"),
        event_key="ae2-rerun-result",
    )
    await _send(
        service,
        session_id,
        "accept_trial",
        request_id="ae2-rerun-accept",
    )
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 2),
        event_key="ae2-report-2",
    )
    reports = service.get_session(session_id).projection.stage_reports
    assert len(reports) == 2
    v1 = next(report for report in reports if report.report_no == 1)
    v2 = next(report for report in reports if report.report_no == 2)
    assert v1.superseded_by == 2
    assert v2.superseded_by is None

    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="ae2-confirm",
    )
    snapshots = service.get_session(session_id).projection.confirmed_snapshots
    assert snapshots[-1].stage_id == "stage-1"
    assert snapshots[-1].report_no == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("next_action", "expected_state"),
    [
        ("clarify", SessionState.GENERATING_QUESTIONS),
        ("rerun", SessionState.GENERATING_TRIAL),
    ],
)
async def test_stage_report_feedback_routes_to_clarification_or_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    next_action: str,
    expected_state: SessionState,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, f"feedback-{next_action}")
    await _advance_to_stage_report(
        service,
        session_id,
        "stage-1",
        f"feedback-{next_action}",
    )
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key=f"feedback-{next_action}-report",
    )
    record = service.get_session(session_id)

    mutation = await _send(
        service,
        session_id,
        "submit_trial_feedback",
        {
            "feedback": "根据阶段 SOP 补充规则后继续",
            "rerun_of_run_id": record.projection.current_run_id,
            "next_action": next_action,
        },
        request_id=f"feedback-{next_action}-command",
    )

    assert mutation.record.projection.state is expected_state
    assert mutation.record.projection.current_stage_id == "stage-1"


@pytest.mark.asyncio
async def test_ae3_confirmed_stage_cannot_be_reopened_or_confirm_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "ae3")
    await _advance_to_stage_report(service, session_id, "stage-1", "ae3")
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="ae3-report-1",
    )
    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="ae3-confirm",
    )
    assert (
        service.get_session(session_id).projection.state
        is SessionState.REFRESHING_CUMULATIVE
    )
    with pytest.raises(WPlusCommandError, match="not awaiting confirmation"):
        await _send(
            service,
            session_id,
            "confirm_stage",
            request_id="ae3-confirm-again",
        )


@pytest.mark.asyncio
async def test_confirmed_stage_answers_cannot_be_revised_from_next_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[dict[str, Any]] = []

    async def fake_start(**kwargs):
        starts.append(kwargs)
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "locked-revision")
    await _advance_to_stage_report(
        service,
        session_id,
        "stage-1",
        "locked-revision",
    )
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="locked-revision-report",
    )
    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="locked-revision-confirm",
    )
    service.append_agent_event(
        kind="cumulative_refreshed",
        payload=_cumulative_refreshed_payload(service.get_session(session_id)),
        event_key="locked-revision-cumulative",
    )
    refresh_start = starts[-1]
    assert refresh_start["target_state"] == "RefreshingCumulative"
    await refresh_start["on_complete"]()
    service.append_agent_event(
        kind="question_batch",
        payload=_question_payload("stage-2", "locked-revision-stage-2"),
        event_key="locked-revision-stage-2-questions",
    )

    with pytest.raises(WPlusCommandError, match="current unconfirmed stage"):
        await _send(
            service,
            session_id,
            "revise_answer",
            {
                "revised_round": 1,
                "answers": {"q-locked-revision": "no"},
                "reason": "尝试修改已确认环节",
            },
            request_id="locked-revision-attempt",
        )

    projection = service.get_session(session_id).projection
    assert projection.current_stage_id == "stage-2"
    assert projection.stages[0].status is StageStatus.CONFIRMED


@pytest.mark.asyncio
async def test_ae4_cumulative_contains_only_confirmed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "ae4")
    await _advance_to_stage_report(service, session_id, "stage-1", "ae4")
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="ae4-report-1",
    )
    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="ae4-confirm",
    )
    service.append_agent_event(
        kind="cumulative_refreshed",
        payload=_cumulative_refreshed_payload(
            service.get_session(session_id),
            preview_version=1,
        ),
        event_key="ae4-cumulative",
    )
    projection = service.get_session(session_id).projection
    assert projection.cumulative_preview is not None
    assert projection.cumulative_preview.stage_order == ["stage-1"]
    assert projection.state is SessionState.GENERATING_QUESTIONS
    assert projection.current_stage_id == "stage-2"


@pytest.mark.asyncio
async def test_refresh_failure_does_not_advance_to_next_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_start(**kwargs):
        return SimpleNamespace(run_id=kwargs["run_id"])

    monkeypatch.setattr(service_module, "start_wplus_chat_turn", fake_start)
    service = _service(tmp_path)
    session_id = await _new_two_stage_session(service, "refresh-fail")
    await _advance_to_stage_report(service, session_id, "stage-1", "refresh-fail")
    service.append_agent_event(
        kind="stage_report_generated",
        payload=_stage_report_payload("stage-1", 1),
        event_key="refresh-fail-report",
    )
    await _send(
        service,
        session_id,
        "confirm_stage",
        request_id="refresh-fail-confirm",
    )
    assert (
        service.get_session(session_id).projection.state
        is SessionState.REFRESHING_CUMULATIVE
    )
    bad_preview = _cumulative_refreshed_payload(
        service.get_session(session_id),
        preview_version=1,
    )
    extra_snapshot = dict(bad_preview["preview"]["snapshots"][0])
    extra_snapshot["stage_id"] = "stage-9"
    bad_preview["preview"]["stage_order"] = ["stage-1", "stage-9"]
    bad_preview["preview"]["snapshots"] = [
        bad_preview["preview"]["snapshots"][0],
        extra_snapshot,
    ]
    with pytest.raises(WPlusCommandError, match="does not match confirmed snapshots"):
        service.append_agent_event(
            kind="cumulative_refreshed",
            payload=bad_preview,
            event_key="refresh-fail-bad-cumulative",
        )
    projection = service.get_session(session_id).projection
    assert projection.state is SessionState.REFRESHING_CUMULATIVE
    assert len(projection.confirmed_snapshots) == 1
    assert projection.confirmed_snapshots[0].stage_id == "stage-1"
