# -*- coding: utf-8 -*-
"""Ownership-aware W+ SOP application service."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import NAMESPACE_URL, uuid4, uuid5

from .models import (
    AnswerAcceptedPayload,
    AnswerBatch,
    ChatProjectionOutboxItem,
    CommandReceipt,
    ConfirmedStageSnapshot,
    CumulativePreview,
    CumulativeRefreshedPayload,
    EntryDetectionMode,
    EntryProposalStatus,
    EventKind,
    LifecycleProgressPayload,
    MemoryCandidatesPayload,
    MemoryCandidateStatus,
    MemoryWriteCompletedPayload,
    MemoryWriteBatchResultPayload,
    MemoryWriteFailedPayload,
    MemoryWriteReceipt,
    OwnershipTuple,
    Question,
    QuestionAnswer,
    QuestionBatchPayload,
    RecoverableFailurePayload,
    RevisionAppliedPayload,
    RunAttempt,
    RunStatus,
    SessionProjection,
    SessionRecord,
    SessionState,
    SessionStateChangedPayload,
    SopResultPayload,
    Stage,
    StageConfirmationRequiredPayload,
    StageConfirmedPayload,
    StageProposalPayload,
    StageQueue,
    StageQueueConfirmedPayload,
    StageReport,
    StageReportGeneratedPayload,
    StageReportGenerationFailedPayload,
    StageStatus,
    StructuredInteractionEnvelope,
    TerminationSummaryPayload,
    TrialExecutionCompletedPayload,
    TrialExecutionFailedPayload,
    TrialFeedbackAcceptedPayload,
    TrialPlanPayload,
    WPlusEntryProposal,
)
from .memory_policy import (
    WPlusMemoryPolicyError,
    normalize_anonymous_user_scope,
    resolve_memory_target,
)
from .runtime import WPlusChatRunBusyError, start_wplus_chat_turn
from .store import (
    StaleStateVersionError,
    StoreMutation,
    WPlusSopStore,
    WPlusSopStoreError,
)

logger = logging.getLogger(__name__)

_OUTBOX_LOCKS_GUARD = threading.Lock()
_OUTBOX_LOCKS: dict[str, asyncio.Lock] = {}
_CHAT_IDLE_WAIT_TIMEOUT_SECONDS = 10.0
_CHAT_IDLE_POLL_SECONDS = 0.05
_CUMULATIVE_CONTINUATION_COMMAND = "continue_after_cumulative"


def _cumulative_handoff_pending(record: SessionRecord) -> bool:
    """Return whether the current run crossed cumulative output only."""

    projection = record.projection
    if projection.state not in {
        SessionState.GENERATING_QUESTIONS,
        SessionState.FINALIZING_OUTPUTS,
    }:
        return False
    attempt = next(
        (
            item
            for item in record.runs
            if item.run_id == projection.current_run_id
            and item.status in {RunStatus.CLAIMED, RunStatus.RUNNING}
        ),
        None,
    )
    if attempt is None:
        return False
    receipt = record.command_receipts.get(attempt.command_request_id)
    claimed_version = receipt.resulting_state_version if receipt else None
    if claimed_version is None:
        return False
    return any(
        event.kind is EventKind.CUMULATIVE_REFRESHED
        and event.state_version > claimed_version
        for event in record.events
    )


class _CommandResult:
    """Result bag produced by each command handler."""

    __slots__ = (
        "target_state",
        "kind",
        "typed_payload",
        "changes",
        "starts_run",
        "retry_of",
        "rerun_of",
        "cancel_active_run",
        "runtime_payload",
    )

    def __init__(
        self,
        *,
        target_state: SessionState,
        kind: EventKind,
        typed_payload: Any,
        changes: dict[str, Any] | None = None,
        starts_run: bool = False,
        retry_of: str | None = None,
        rerun_of: str | None = None,
        cancel_active_run: bool = False,
        runtime_payload: dict[str, Any] | None = None,
    ) -> None:
        self.target_state = target_state
        self.kind = kind
        self.typed_payload = typed_payload
        self.changes = changes or {}
        self.starts_run = starts_run
        self.retry_of = retry_of
        self.rerun_of = rerun_of
        self.cancel_active_run = cancel_active_run
        self.runtime_payload = runtime_payload or {}


_CHAT_IDLE_RETRY_AFTER_MS = 1000


def _outbox_lock(store_path: Path, chat_id: str) -> asyncio.Lock:
    key = f"{store_path.expanduser().resolve()}::{chat_id}"
    with _OUTBOX_LOCKS_GUARD:
        return _OUTBOX_LOCKS.setdefault(key, asyncio.Lock())


def _build_chat_projection_audit(
    existing_meta: dict[str, Any],
    pending: list[ChatProjectionOutboxItem],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build the merged audit trail with deduplicated pending items."""
    existing_audit = existing_meta.get("wplus_sop_audit", [])
    audit = (
        [dict(item) for item in existing_audit]
        if isinstance(existing_audit, list)
        else []
    )
    projected_ids: set[str] = {
        str(item.get("projection_event_id") or "")
        for item in audit
        if isinstance(item, dict)
    }
    for item in pending:
        if item.projection_event_id in projected_ids:
            continue
        audit.append(
            {
                "projection_event_id": item.projection_event_id,
                "session_id": item.sop_session_id,
                "event_id": item.event_id,
                "kind": item.kind,
                "payload": item.payload,
                "created_at": item.created_at.isoformat(),
            },
        )
        projected_ids.add(item.projection_event_id)
    return audit, projected_ids


def _ack_pending_outbox_items(
    store: WPlusSopStore,
    pending: list[ChatProjectionOutboxItem],
    durable_ids: set[str],
) -> int:
    """Acknowledge outbox items that were durably written to Chat metadata."""
    acknowledged = 0
    for item in pending:
        if item.projection_event_id not in durable_ids:
            continue
        acknowledged += int(
            store.ack_outbox(item.projection_event_id),
        )
    return acknowledged


class WPlusOwnershipError(LookupError):
    """Fail-closed ownership mismatch."""


class WPlusCommandError(ValueError):
    """Malformed or illegal state command."""


def _parse_memory_decisions(
    payload: dict[str, Any],
    candidates: list[Any],
) -> dict[str, str]:
    raw_decisions = payload.get("decisions")
    if set(payload) != {"decisions"} or not isinstance(raw_decisions, list):
        raise WPlusCommandError("Memory decisions must be a complete list")
    decisions: dict[str, str] = {}
    for raw in raw_decisions:
        if not isinstance(raw, dict) or set(raw) != {
            "candidate_id",
            "decision",
        }:
            raise WPlusCommandError("Invalid memory decision")
        candidate_id = raw.get("candidate_id")
        decision = raw.get("decision")
        if (
            not isinstance(candidate_id, str)
            or candidate_id in decisions
            or decision not in {"approve", "reject"}
        ):
            raise WPlusCommandError("Invalid memory decision")
        candidate = next(
            (item for item in candidates if item.candidate_id == candidate_id),
            None,
        )
        if (
            decision == "approve"
            and candidate is not None
            and candidate.legacy_read_only
        ):
            raise WPlusCommandError(
                "Cannot approve a read-only legacy memory candidate",
            )
        decisions[candidate_id] = decision
    unresolved_ids = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.status
        in {MemoryCandidateStatus.PENDING, MemoryCandidateStatus.FAILED}
    }
    if set(decisions) != unresolved_ids:
        raise WPlusCommandError(
            "Memory decisions must cover every unresolved candidate",
        )
    return decisions


def _memory_runtime_candidate(candidate: Any) -> dict[str, Any]:
    if candidate.legacy_read_only:
        raise WPlusCommandError(
            "Cannot write a read-only legacy memory candidate",
        )
    if (
        candidate.memory_type is None
        or not isinstance(candidate.value, dict)
        or not (candidate.evidence or "").strip()
        or candidate.target_scope is None
        or candidate.target_file is None
    ):
        raise WPlusCommandError(
            "Memory candidate is not ready for an approved run",
        )
    return {
        "candidate_id": candidate.candidate_id,
        "type": candidate.memory_type,
        "content": candidate.value,
        "evidence": candidate.evidence,
        "target_scope": candidate.target_scope,
        "target_file": candidate.target_file,
        "script": "scripts/memory_store.py",
        "approved": True,
    }


def _apply_memory_batch_results(
    projection: SessionProjection,
    payload: MemoryWriteBatchResultPayload,
) -> tuple[list[Any], SessionState]:
    active_ids = projection.active_memory_candidate_ids or (
        [projection.active_memory_candidate_id]
        if projection.active_memory_candidate_id
        else []
    )
    results_by_id = {result.candidate_id: result for result in payload.results}
    if set(results_by_id) != set(active_ids):
        raise WPlusCommandError(
            "Memory batch result must cover every server-bound candidate",
        )
    candidates = [
        candidate.model_copy(deep=True)
        for candidate in projection.memory_candidates
    ]
    for index, candidate in enumerate(candidates):
        result = results_by_id.get(candidate.candidate_id)
        if result is None:
            continue
        if candidate.status is not MemoryCandidateStatus.WRITING:
            raise WPlusCommandError("Memory candidate is not being written")
        if result.status == "succeeded":
            if (
                result.target_scope != candidate.target_scope
                or result.target_file != candidate.target_file
            ):
                raise WPlusCommandError(
                    "Memory write receipt does not match approved candidate",
                )
            candidates[index] = candidate.model_copy(
                update={
                    "status": MemoryCandidateStatus.APPROVED,
                    "failure_reason": None,
                    "write_receipt": MemoryWriteReceipt(
                        memory_id=(
                            f"wplus-sop/{projection.sop_session_id}/"
                            f"{candidate.candidate_id}"
                        ),
                        target_scope=result.target_scope,
                        target_file=result.target_file,
                        reused_existing=(result.result == "duplicate"),
                        store_result=result.result,
                    ),
                },
            )
        else:
            candidates[index] = candidate.model_copy(
                update={
                    "status": MemoryCandidateStatus.FAILED,
                    "failure_reason": (result.summary or "")[:500],
                    "write_receipt": None,
                },
            )
    target = (
        SessionState.MEMORY_REVIEW
        if any(
            candidate.status is MemoryCandidateStatus.FAILED
            for candidate in candidates
        )
        else SessionState.COMPLETED
    )
    return candidates, target


class WPlusOwningChatFinalizingError(WPlusCommandError):
    """The owning Chat has not released its prior Agent run yet."""

    code = "owning_chat_finalizing"

    def __init__(self, *, retry_after_ms: int = _CHAT_IDLE_RETRY_AFTER_MS):
        super().__init__(
            "The prior owning Chat Agent run is still finalizing",
        )
        self.retry_after_ms = retry_after_ms


class WPlusRuntimeStartError(RuntimeError):
    """Session was persisted but its Agent run could not start."""


class _WPlusRunClaimLostError(RuntimeError):
    """The persisted run stopped being active before task registration."""


_RUN_EVENT_STATES: dict[EventKind, frozenset[SessionState]] = {
    EventKind.STAGE_PROPOSAL: frozenset(
        {SessionState.GENERATING_STAGE_PROPOSAL},
    ),
    EventKind.QUESTION_BATCH: frozenset(
        {SessionState.GENERATING_QUESTIONS},
    ),
    EventKind.TRIAL_PLAN: frozenset(
        {
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
        },
    ),
    EventKind.TRIAL_EXECUTION_STARTED: frozenset(
        {
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.EXECUTING_TRIAL,
        },
    ),
    EventKind.TRIAL_EXECUTION_PROGRESS: frozenset(
        {
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.EXECUTING_TRIAL,
        },
    ),
    EventKind.TRIAL_EXECUTION_COMPLETED: frozenset(
        {
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.EXECUTING_TRIAL,
        },
    ),
    EventKind.TRIAL_EXECUTION_FAILED: frozenset(
        {
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.EXECUTING_TRIAL,
        },
    ),
    EventKind.SOP_RESULT: frozenset({SessionState.FINALIZING_OUTPUTS}),
    EventKind.STAGE_REPORT_GENERATED: frozenset(
        {SessionState.GENERATING_STAGE_REPORT},
    ),
    EventKind.STAGE_REPORT_GENERATION_FAILED: frozenset(
        {SessionState.GENERATING_STAGE_REPORT},
    ),
    EventKind.CUMULATIVE_REFRESHED: frozenset(
        {SessionState.REFRESHING_CUMULATIVE},
    ),
    EventKind.MEMORY_CANDIDATES: frozenset(
        {SessionState.FINALIZING_OUTPUTS},
    ),
    EventKind.MEMORY_WRITE_COMPLETED: frozenset(
        {SessionState.WRITING_MEMORY},
    ),
    EventKind.MEMORY_WRITE_FAILED: frozenset(
        {SessionState.WRITING_MEMORY},
    ),
    EventKind.MEMORY_WRITE_BATCH_RESULT: frozenset(
        {SessionState.WRITING_MEMORY},
    ),
    EventKind.LIFECYCLE_PROGRESS: frozenset(
        {
            SessionState.GENERATING_STAGE_PROPOSAL,
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.EXECUTING_TRIAL,
            SessionState.GENERATING_STAGE_REPORT,
            SessionState.REFRESHING_CUMULATIVE,
            SessionState.FINALIZING_OUTPUTS,
            SessionState.WRITING_MEMORY,
        },
    ),
    EventKind.RECOVERABLE_FAILURE: frozenset(
        {
            SessionState.GENERATING_STAGE_PROPOSAL,
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.EXECUTING_TRIAL,
            SessionState.GENERATING_STAGE_REPORT,
            SessionState.REFRESHING_CUMULATIVE,
            SessionState.FINALIZING_OUTPUTS,
            SessionState.WRITING_MEMORY,
        },
    ),
}

_PENDING_EXIT_BOUNDARIES = frozenset(
    {
        EventKind.STAGE_PROPOSAL,
        EventKind.QUESTION_BATCH,
        EventKind.TRIAL_EXECUTION_COMPLETED,
        EventKind.TRIAL_EXECUTION_FAILED,
        EventKind.STAGE_REPORT_GENERATED,
        EventKind.STAGE_REPORT_GENERATION_FAILED,
        EventKind.CUMULATIVE_REFRESHED,
        EventKind.MEMORY_CANDIDATES,
        EventKind.MEMORY_WRITE_COMPLETED,
        EventKind.MEMORY_WRITE_FAILED,
        EventKind.MEMORY_WRITE_BATCH_RESULT,
        EventKind.RECOVERABLE_FAILURE,
    },
)

_ORPHAN_RECOVERY_STATES = frozenset(
    {
        SessionState.GENERATING_STAGE_PROPOSAL,
        SessionState.GENERATING_QUESTIONS,
        SessionState.GENERATING_TRIAL,
        SessionState.EXECUTING_TRIAL,
        SessionState.GENERATING_STAGE_REPORT,
        SessionState.REFRESHING_CUMULATIVE,
        SessionState.FINALIZING_OUTPUTS,
        SessionState.WRITING_MEMORY,
    },
)
_ORPHAN_RECOVERY_GRACE = timedelta(seconds=5)


def store_path_for_workspace(workspace_dir: Path | str) -> Path:
    """Return the local single-process W+ store path."""
    return Path(workspace_dir).expanduser() / ".sop" / "wplus-sop.json"


def _validate_delivered_artifacts(
    *,
    workspace_dir: Path | str,
    result: Any,
) -> None:
    """Verify that Agent-declared deliveries are real workspace static files."""
    static_root = (
        Path(workspace_dir).expanduser().resolve() / "static"
    ).resolve()
    for artifact in result.artifacts:
        local_file = (static_root / artifact.static_file_name).resolve()
        try:
            local_file.relative_to(static_root)
        except ValueError as exc:
            raise WPlusCommandError(
                "artifact escaped workspace static",
            ) from exc
        if not local_file.is_file():
            raise WPlusCommandError(
                f"delivered artifact is missing: {artifact.artifact_id}",
            )
        raw = local_file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact.sha256:
            raise WPlusCommandError(
                f"delivered artifact hash mismatch: {artifact.artifact_id}",
            )


def _artifact_path_segment(value: Any) -> str:
    return quote(str(value), safe="")


def _artifact_url(path: str, query: dict[str, Any]) -> str:
    return f"{path}?{urlencode(query)}"


def _final_artifact_download_url(
    sop_session_id: str,
    artifact_id: str,
) -> str:
    return _artifact_url(
        "/api/wplus-sop/sessions/"
        f"{_artifact_path_segment(sop_session_id)}/artifacts/"
        f"{_artifact_path_segment(artifact_id)}",
        {"download": "true"},
    )


def _stage_report_artifact_download_url(
    sop_session_id: str,
    report: StageReport,
    artifact_id: str,
) -> str:
    return _artifact_url(
        "/api/wplus-sop/sessions/"
        f"{_artifact_path_segment(sop_session_id)}/stage-report-artifacts/"
        f"{_artifact_path_segment(artifact_id)}",
        {
            "stage_id": report.stage_id,
            "revision": report.revision,
            "report_no": report.report_no,
            "download": "true",
        },
    )


def _cumulative_artifact_download_url(
    sop_session_id: str,
    preview: CumulativePreview,
    artifact_id: str,
) -> str:
    return _artifact_url(
        "/api/wplus-sop/sessions/"
        f"{_artifact_path_segment(sop_session_id)}/cumulative-artifacts/"
        f"{_artifact_path_segment(artifact_id)}",
        {
            "preview_version": preview.preview_version,
            "download": "true",
        },
    )


def _result_preview(
    result: Any,
    *,
    sop_session_id: str,
) -> dict[str, str | None]:
    artifacts_by_id = {
        artifact.artifact_id: artifact for artifact in result.artifacts
    }
    markdown_artifact = artifacts_by_id.get("sop_render_md")
    html_artifact = artifacts_by_id.get("sop_render_html")
    return {
        "markdown": result.readable_sop,
        "html": result.html,
        "markdown_url": (
            _final_artifact_download_url(
                sop_session_id,
                markdown_artifact.artifact_id,
            )
            if markdown_artifact
            else None
        ),
        "html_url": (
            _final_artifact_download_url(
                sop_session_id,
                html_artifact.artifact_id,
            )
            if html_artifact
            else None
        ),
        "markdown_sha256": (
            markdown_artifact.sha256 if markdown_artifact else None
        ),
        "html_sha256": html_artifact.sha256 if html_artifact else None,
    }


def _same_ownership(left: OwnershipTuple, right: OwnershipTuple) -> bool:
    return left.model_dump(mode="json") == right.model_dump(mode="json")


def _serialize_stage(
    stage: Stage,
    *,
    current_stage_id: str | None,
) -> dict[str, Any]:
    if stage.status is StageStatus.CONFIRMED:
        status = "confirmed"
    elif stage.stage_id == current_stage_id:
        status = "current"
    elif stage.status is StageStatus.INVALIDATED:
        status = "invalidated"
    else:
        status = "pending"
    return {
        "stage_id": stage.stage_id,
        "title": stage.name,
        "description": stage.description,
        "status": status,
    }


def _current_trial_events(
    record: SessionRecord,
    run_id: str | None,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    plan: dict[str, Any] | None = None
    started: dict[str, Any] | None = None
    completed: dict[str, Any] | None = None
    failed: dict[str, Any] | None = None
    progress_by_step: dict[str, dict[str, Any]] = {}
    for event in record.events:
        payload = event.payload.model_dump(mode="json")
        if payload.get("run_id") != run_id:
            continue
        if event.kind is EventKind.TRIAL_PLAN:
            plan = payload
        elif event.kind is EventKind.TRIAL_EXECUTION_STARTED:
            started = payload
        elif event.kind is EventKind.TRIAL_EXECUTION_PROGRESS:
            progress_by_step[str(payload["step_id"])] = payload
        elif event.kind is EventKind.TRIAL_EXECUTION_COMPLETED:
            completed = payload
        elif event.kind is EventKind.TRIAL_EXECUTION_FAILED:
            failed = payload
    return plan, started, progress_by_step, completed, failed


def _serialize_trial_steps(
    plan: dict[str, Any] | None,
    progress_by_step: dict[str, dict[str, Any]],
    completed: dict[str, Any] | None,
    failed: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    capabilities: list[dict[str, Any]] = []
    seen_capabilities: set[str] = set()
    is_completed = completed is not None
    contract_verified = bool(completed and completed.get("schema_validated"))
    for step in (plan or {}).get("steps", []):
        step_id = str(step["step_id"])
        progress = progress_by_step.get(step_id, {})
        status = str(progress.get("status") or "pending")
        if is_completed:
            status = "completed"
        elif failed is not None and failed.get("failed_step_id") == step_id:
            status = "failed"
        if status not in {
            "pending",
            "running",
            "completed",
            "failed",
            "blocked",
        }:
            status = "running"
        capability_id = str(step["capability_id"])
        steps.append(
            {
                "step_id": step_id,
                "title": str(step["label"]),
                "capability": capability_id,
                "status": status,
                "summary": progress.get("summary"),
                "elapsed_ms": progress.get("elapsed_ms"),
            },
        )
        if capability_id in seen_capabilities:
            continue
        seen_capabilities.add(capability_id)
        capabilities.append(
            {
                "capability_id": capability_id,
                "name": capability_id,
                "verification_status": (
                    "verified" if is_completed else "unverified"
                ),
                "output_contract_status": (
                    "verified" if contract_verified else "unverified"
                ),
            },
        )
    return steps, capabilities


def _trial_status(
    current_attempt: RunAttempt | None,
    *,
    completed: bool,
    failed: bool,
) -> str:
    if completed:
        return "completed"
    if failed:
        return "failed"
    if current_attempt is None or current_attempt.status is RunStatus.CLAIMED:
        return "planning"
    if current_attempt.status is RunStatus.RUNNING:
        return "running"
    return "failed"


def _serialize_current_trial(
    record: SessionRecord,
    current_attempt: RunAttempt | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    projection = record.projection
    run_id = projection.current_run_id
    result_lists = projection.trial_result_lists
    if not run_id and not result_lists:
        return None, []
    plan, started, progress_by_step, completed, failed = _current_trial_events(
        record,
        run_id,
    )

    result_columns: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    if result_lists:
        result_columns = [
            column.model_dump(mode="json")
            for column in result_lists[0].columns
        ]
        result_rows = result_lists[0].rows

    steps, capabilities = _serialize_trial_steps(
        plan,
        progress_by_step,
        completed,
        failed,
    )
    trial = {
        "run_id": run_id
        or (current_attempt.run_id if current_attempt else ""),
        "attempt_id": current_attempt.attempt_id if current_attempt else None,
        "rerun_of_run_id": (
            current_attempt.rerun_of_run_id if current_attempt else None
        ),
        "status": _trial_status(
            current_attempt,
            completed=completed is not None,
            failed=failed is not None,
        ),
        "started_at": (started or {}).get("started_at"),
        "completed_at": (completed or {}).get("completed_at"),
        "elapsed_ms": None,
        "steps": steps,
        "summary": (completed or failed or {}).get("summary"),
        "warnings": (completed or {}).get("warnings", []),
        "result_columns": result_columns,
        "result_rows": result_rows,
    }
    return trial, capabilities


def _serialize_artifact(
    artifact: Any,
    *,
    download_url: str,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "name": artifact.name,
        "format": (
            "json"
            if artifact.name.endswith(".json")
            else (
                "markdown"
                if artifact.name.endswith(".md")
                else "html"
            )
        ),
        "status": "validated",
        "download_url": download_url,
        "sha256": artifact.sha256,
        "copied_by": artifact.copied_by,
    }


def _serialize_cumulative_preview(
    preview: CumulativePreview,
    *,
    sop_session_id: str,
) -> dict[str, Any]:
    return {
        "preview_version": preview.preview_version,
        "stage_order": preview.stage_order,
        "snapshots": [
            snapshot.model_dump(mode="json")
            for snapshot in preview.snapshots
        ],
        "artifacts": [
            _serialize_artifact(
                artifact,
                download_url=_cumulative_artifact_download_url(
                    sop_session_id,
                    preview,
                    artifact.artifact_id,
                ),
            )
            for artifact in preview.artifacts
        ],
        "rendered_sha256": preview.rendered_sha256,
    }


def serialize_session(record: SessionRecord) -> dict[str, Any]:
    """Project the persisted domain model into the frontend contract."""
    projection = record.projection
    current_attempt = next(
        (
            attempt
            for attempt in reversed(record.runs)
            if attempt.run_id == projection.current_run_id
        ),
        None,
    )
    question_batch = None
    if projection.current_question_batch is not None:
        batch = projection.current_question_batch
        question_batch = {
            "batch_id": batch.batch_id,
            "stage_id": batch.stage_id,
            "questions": [
                {
                    "question_id": question.question_id,
                    "kind": question.type.value,
                    "prompt": question.prompt,
                    "help_text": question.help_text,
                    "required": question.required,
                    "options": [
                        option.model_dump(mode="json")
                        for option in question.options
                    ],
                }
                for question in batch.questions
            ],
        }

    trial, capabilities = _serialize_current_trial(record, current_attempt)

    return {
        "session_id": projection.sop_session_id,
        "chat_id": projection.chat_id,
        "logical_chat_session_id": projection.logical_chat_session_id,
        "title": projection.title,
        "state": projection.state.value,
        "state_version": projection.state_version,
        "revision": projection.revision,
        "round": projection.round,
        "stages": [
            _serialize_stage(
                stage,
                current_stage_id=projection.current_stage_id,
            )
            for stage in projection.stages
        ],
        "current_stage_id": projection.current_stage_id,
        "question_batch": question_batch,
        "trial": trial,
        "facts": projection.confirmed_facts,
        "unknowns": projection.unknowns,
        "capabilities": capabilities,
        "artifacts": [
            _serialize_artifact(
                artifact,
                download_url=_final_artifact_download_url(
                    projection.sop_session_id,
                    artifact.artifact_id,
                ),
            )
            for artifact in (
                projection.final_result.artifacts
                if projection.final_result is not None
                else []
            )
        ],
        "result_preview": (
            _result_preview(
                projection.final_result,
                sop_session_id=projection.sop_session_id,
            )
            if projection.final_result is not None
            else None
        ),
        "stage_reports": [
            {
                "stage_id": report.stage_id,
                "report_no": report.report_no,
                "revision": report.revision,
                "superseded_by": report.superseded_by,
                "created_at": report.created_at.isoformat(),
                "artifacts": [
                    _serialize_artifact(
                        artifact,
                        download_url=_stage_report_artifact_download_url(
                            projection.sop_session_id,
                            report,
                            artifact.artifact_id,
                        ),
                    )
                    for artifact in report.artifacts
                ],
            }
            for report in projection.stage_reports
        ],
        "cumulative_preview": (
            _serialize_cumulative_preview(
                projection.cumulative_preview,
                sop_session_id=projection.sop_session_id,
            )
            if projection.cumulative_preview is not None
            else None
        ),
        "memory_candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "title": candidate.summary,
                "description": candidate.summary,
                "memory_type": candidate.memory_type,
                "content": candidate.value,
                "evidence": candidate.evidence,
                "target_scope": candidate.target_scope,
                "target_file": candidate.target_file,
                "status": candidate.status.value,
                "failure_reason": candidate.failure_reason,
                "write_receipt": (
                    candidate.write_receipt.model_dump(mode="json")
                    if candidate.write_receipt is not None
                    else None
                ),
                "legacy_read_only": candidate.legacy_read_only,
            }
            for candidate in projection.memory_candidates
        ],
        "failure": (
            {
                "code": projection.last_error.error_code,
                "message": projection.last_error.summary,
                "retryable": True,
                "failed_run_id": projection.last_error.failed_run_id,
            }
            if projection.last_error is not None
            else None
        ),
        "pending_exit": (
            {
                "requested_action": projection.pending_exit_action,
                "requested_at": projection.updated_at.isoformat(),
            }
            if projection.pending_exit_action is not None
            else None
        ),
        "resume_state": (
            projection.resume_state.value
            if projection.resume_state is not None
            else None
        ),
        "updated_at": projection.updated_at.isoformat(),
    }


class WPlusSopService:
    """Coordinate the durable state machine and existing Agent runtime."""

    def __init__(
        self,
        *,
        workspace: Any,
        ownership: OwnershipTuple,
        store: WPlusSopStore | None = None,
    ):
        self.workspace = workspace
        self.ownership = ownership
        self.store = store or WPlusSopStore(
            store_path_for_workspace(workspace.workspace_dir),
        )

    def _owned_record(self, sop_session_id: str) -> SessionRecord:
        record = self.store.get_session(sop_session_id)
        if record is None or not _same_ownership(
            record.projection.ownership,
            self.ownership,
        ):
            raise WPlusOwnershipError(sop_session_id)
        return record

    def get_session(self, sop_session_id: str) -> SessionRecord:
        return self._owned_record(sop_session_id)

    def get_active_session(self) -> SessionRecord | None:
        return self.store.get_active_by_chat(self.ownership)

    async def get_runtime_status(
        self,
        sop_session_id: str,
    ) -> dict[str, Any]:
        """Project transient owning-Chat availability without persisting it."""

        record = self._owned_record(sop_session_id)
        coordinator = getattr(
            self.workspace,
            "answer_turn_coordinator",
            None,
        )
        turn_status = (
            await coordinator.status(self.ownership.chat_id)
            if coordinator is not None
            else None
        )
        tracker_status = (
            turn_status.value if turn_status is not None else "idle"
        )

        if tracker_status == "idle":
            status = "ready"
        elif tracker_status == "stopping":
            status = "stopping"
        else:
            projection = record.projection
            effective_state = (
                projection.resume_state
                if projection.state is SessionState.PENDING_EXIT
                else projection.state
            )
            status = (
                "running"
                if effective_state in _ORPHAN_RECOVERY_STATES
                else "finalizing"
            )
        runtime_ready = status == "ready"
        return {
            "status": status,
            "runtime_ready": runtime_ready,
            "blocking_run_id": (
                None if runtime_ready else record.projection.current_run_id
            ),
        }

    async def recover_orphaned_generation_run(
        self,
        sop_session_id: str,
    ) -> StoreMutation | None:
        """Fail an old persisted run only when its Chat task is gone."""

        record = self._owned_record(sop_session_id)
        projection = record.projection
        recovery_state = (
            projection.resume_state
            if projection.state is SessionState.PENDING_EXIT
            else projection.state
        )
        if (
            recovery_state not in _ORPHAN_RECOVERY_STATES
            or projection.current_run_id is None
        ):
            return None
        candidates = [
            attempt
            for attempt in record.runs
            if attempt.run_id == projection.current_run_id
            and attempt.status in {RunStatus.CLAIMED, RunStatus.RUNNING}
        ]
        if len(candidates) != 1:
            return None
        now = datetime.now(timezone.utc)
        if now - candidates[0].created_at < _ORPHAN_RECOVERY_GRACE:
            return None

        task_tracker = getattr(self.workspace, "task_tracker", None)
        if task_tracker is None:
            return None

        # TaskTracker serializes this callback with registration for this Chat.
        # The Store then revalidates and persists the event and run settlement
        # in one locked save.
        def _recover_while_idle() -> StoreMutation | None:
            current = self._owned_record(sop_session_id)
            current_projection = current.projection
            current_recovery_state = (
                current_projection.resume_state
                if current_projection.state is SessionState.PENDING_EXIT
                else current_projection.state
            )
            current_candidates = [
                attempt
                for attempt in current.runs
                if attempt.run_id == current_projection.current_run_id
                and attempt.status in {RunStatus.CLAIMED, RunStatus.RUNNING}
            ]
            if (
                current_recovery_state not in _ORPHAN_RECOVERY_STATES
                or current_projection.current_run_id is None
                or len(current_candidates) != 1
                or now - current_candidates[0].created_at
                < _ORPHAN_RECOVERY_GRACE
            ):
                return None
            attempt = current_candidates[0]
            payload = RecoverableFailurePayload(
                error_code="orphaned_agent_run",
                summary=("后台 Agent 任务已丢失；可以从原生成步骤安全重试。"),
                failed_operation=attempt.command,
                failed_run_id=attempt.run_id,
            )
            if current_projection.state is SessionState.PENDING_EXIT:
                terminate = (
                    current_projection.pending_exit_action == "terminate"
                )
                if terminate:
                    event_kind = EventKind.TERMINATION_SUMMARY
                    event_payload: Any = TerminationSummaryPayload(
                        summary=(
                            "用户已请求结束；丢失的后台 Agent 任务已安全终止。"
                        ),
                    )
                    next_state = SessionState.TERMINATED
                    projection_changes = {
                        "pending_exit_action": None,
                        "termination_summary": event_payload,
                    }
                    run_status = RunStatus.CANCELLED
                else:
                    event_kind = EventKind.SESSION_STATE_CHANGED
                    event_payload = SessionStateChangedPayload(
                        previous_state=SessionState.PENDING_EXIT,
                        state=SessionState.PAUSED,
                        reason="orphaned_agent_run_after_save_and_exit",
                    )
                    next_state = SessionState.PAUSED
                    projection_changes = {
                        "last_error": payload,
                        "pending_exit_action": None,
                        "resume_state": SessionState.RECOVERABLE_FAILURE,
                    }
                    run_status = RunStatus.FAILED
            else:
                event_kind = EventKind.RECOVERABLE_FAILURE
                event_payload = payload
                next_state = SessionState.RECOVERABLE_FAILURE
                projection_changes = {
                    "last_error": payload,
                    "resume_state": current_recovery_state,
                }
                run_status = RunStatus.FAILED
            event = self._event(
                current,
                event_kind,
                event_payload.model_dump(mode="json"),
                event_id=f"evt_orphaned_run_{attempt.attempt_id}",
            )
            return self.store.commit_event(
                sop_session_id,
                expected_state_version=current_projection.state_version,
                event=event,
                next_state=next_state,
                projection_changes=projection_changes,
                outbox_item=self._outbox(event),
                run_completion=(
                    attempt.run_id,
                    attempt.attempt_id,
                    run_status,
                ),
            )

        try:
            _was_idle, recovered = await task_tracker.call_if_idle(
                self.ownership.chat_id,
                _recover_while_idle,
            )
            return recovered
        except (StaleStateVersionError, WPlusSopStoreError):
            logger.info(
                "W+ orphan recovery lost a concurrent race session=%s",
                sop_session_id,
            )
            return None
        except Exception:
            logger.exception(
                "Could not inspect or recover W+ Agent task session=%s",
                sop_session_id,
            )
            return None

    def _assert_runtime_claim_active(
        self,
        *,
        sop_session_id: str,
        run_id: str,
        attempt_id: str,
    ) -> None:
        """Revalidate one exact persisted claim before task registration."""

        record = self._owned_record(sop_session_id)
        runtime_state = (
            record.projection.resume_state
            if record.projection.state is SessionState.PENDING_EXIT
            else record.projection.state
        )
        if (
            runtime_state not in _ORPHAN_RECOVERY_STATES
            or record.projection.current_run_id != run_id
        ):
            raise _WPlusRunClaimLostError(
                "W+ run claim is no longer active",
            )
        matches = [
            attempt
            for attempt in record.runs
            if attempt.run_id == run_id
            and attempt.attempt_id == attempt_id
            and attempt.status in {RunStatus.CLAIMED, RunStatus.RUNNING}
        ]
        if len(matches) != 1:
            raise _WPlusRunClaimLostError(
                "W+ run attempt is no longer active",
            )

    def _chat_has_expected_ownership(self, chat: Any) -> bool:
        return bool(
            chat is not None
            and str(getattr(chat, "id", "")) == self.ownership.chat_id
            and str(getattr(chat, "user_id", "")) == self.ownership.user_id
            and str(getattr(chat, "session_id", ""))
            == self.ownership.logical_chat_session_id,
        )

    @staticmethod
    def _entry_proposal_chat_metadata(
        proposal: WPlusEntryProposal,
    ) -> dict[str, Any]:
        receipt = proposal.command_receipt
        return {
            "proposal_id": proposal.proposal_id,
            "mode": proposal.detection_mode.value,
            "status": proposal.status.value,
            "session_id": (
                receipt.sop_session_id if receipt is not None else None
            ),
        }

    async def _verified_owned_chat(self) -> Any:
        """Load the Chat again at run start and fail closed on identity drift."""

        chat = await self.workspace.chat_manager.get_chat(
            self.ownership.chat_id,
        )
        if not self._chat_has_expected_ownership(chat):
            raise WPlusOwnershipError(self.ownership.chat_id)
        return chat

    async def _wait_for_owning_chat_idle(self) -> None:
        """Wait for the prior Agent producer to release the owning Chat."""

        coordinator = getattr(
            self.workspace,
            "answer_turn_coordinator",
            None,
        )
        if coordinator is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CHAT_IDLE_WAIT_TIMEOUT_SECONDS
        while (
            await coordinator.current_identity(self.ownership.chat_id)
            is not None
        ):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise WPlusOwningChatFinalizingError()
            await asyncio.sleep(min(_CHAT_IDLE_POLL_SECONDS, remaining))

    async def project_entry_proposal(
        self,
        proposal: WPlusEntryProposal,
        *,
        verified_chat: Any | None = None,
    ) -> bool:
        """Persist the proposal/control-card lifecycle in owning Chat metadata."""

        try:
            chat = (
                await self._verified_owned_chat()
                if verified_chat is None
                else verified_chat
            )
            if not self._chat_has_expected_ownership(chat):
                raise WPlusOwnershipError(self.ownership.chat_id)
            chat.meta = {
                **(chat.meta or {}),
                "wplus_sop_entry_proposal": (
                    self._entry_proposal_chat_metadata(proposal)
                ),
            }
            await self.workspace.chat_manager.update_chat(chat)
            return True
        except Exception:
            logger.exception(
                "Failed to project W+ entry proposal %s into Chat %s",
                proposal.proposal_id,
                self.ownership.chat_id,
            )
            return False

    async def flush_chat_projection_outbox(self) -> int:
        """Project pending Session updates into Chat metadata, then ack them.

        The durable W+ store remains authoritative. A failed or unavailable
        Chat write leaves every item pending so a later command/event can
        retry without losing the projection.
        """
        async with _outbox_lock(self.store.path, self.ownership.chat_id):
            chat_manager = getattr(self.workspace, "chat_manager", None)
            if chat_manager is None:
                return 0
            pending = self._collect_pending_outbox()
            if not pending:
                return 0
            pending.sort(key=lambda item: item.created_at)
            session_label = pending[-1].sop_session_id
            try:
                return await self._execute_outbox_flush(
                    chat_manager,
                    pending,
                    session_label,
                )
            except Exception:
                logger.exception(
                    "Failed to project W+ SOP Session %s into Chat %s",
                    session_label,
                    self.ownership.chat_id,
                )
                return 0

    def _collect_pending_outbox(self) -> list[ChatProjectionOutboxItem]:
        pending: list[ChatProjectionOutboxItem] = []
        for item in self.store.pending_outbox():
            record = self.store.get_session(item.sop_session_id)
            if record is not None and _same_ownership(
                record.projection.ownership,
                self.ownership,
            ):
                pending.append(item)
        return pending

    async def _execute_outbox_flush(
        self,
        chat_manager: Any,
        pending: list[ChatProjectionOutboxItem],
        session_label: str,
    ) -> int:
        chat = await chat_manager.get_chat(self.ownership.chat_id)
        if not self._chat_has_expected_ownership(chat):
            return 0
        existing_meta = chat.meta or {}
        audit, projected_ids = _build_chat_projection_audit(
            existing_meta,
            pending,
        )
        entry_proposal = self._resolve_outbox_entry_proposal(pending)
        durable_ids = await self._write_chat_projection(
            chat_manager,
            chat,
            existing_meta,
            audit,
            entry_proposal,
            pending,
        )
        return _ack_pending_outbox_items(self.store, pending, durable_ids)

    def _resolve_outbox_entry_proposal(
        self,
        pending: list[ChatProjectionOutboxItem],
    ) -> Any:
        """Find the most recent entry proposal referenced in the outbox."""
        for item in reversed(pending):
            proposal_id = item.payload.get("entry_proposal_id")
            if not isinstance(proposal_id, str) or not proposal_id:
                continue
            candidate = self.store.get_entry_proposal(proposal_id)
            if candidate is not None and _same_ownership(
                candidate.ownership,
                self.ownership,
            ):
                return candidate
        return None

    async def _write_chat_projection(
        self,
        chat_manager: Any,
        chat: Any,
        existing_meta: dict[str, Any],
        audit: list[dict[str, Any]],
        entry_proposal: Any,
        pending: list[ChatProjectionOutboxItem],
    ) -> set[str]:
        """Write session metadata and audit trail into Chat, then return durable IDs."""
        latest = pending[-1]
        record = self.store.get_session(latest.sop_session_id)
        if record is None or not _same_ownership(
            record.projection.ownership,
            self.ownership,
        ):
            raise WPlusOwnershipError(self.ownership.chat_id)
        chat.meta = {
            **existing_meta,
            **(
                {
                    "wplus_sop_entry_proposal": (
                        self._entry_proposal_chat_metadata(entry_proposal)
                    ),
                }
                if entry_proposal is not None
                else {}
            ),
            "wplus_sop_session": {
                "session_id": record.projection.sop_session_id,
                "title": record.projection.title,
                "state": record.projection.state.value,
                "state_version": record.projection.state_version,
                "last_event_kind": latest.kind,
            },
            "wplus_sop_audit": audit,
        }
        persisted = await chat_manager.update_chat(chat)
        persisted_audit = (persisted.meta or {}).get("wplus_sop_audit", [])
        return {
            str(item.get("projection_event_id") or "")
            for item in persisted_audit
            if isinstance(item, dict)
        }

    def create_entry_proposal(
        self,
        *,
        original_text: str,
        mode: str,
        memory_user_scope: str | None = None,
    ) -> WPlusEntryProposal:
        normalized_memory_user_scope = normalize_anonymous_user_scope(
            memory_user_scope,
        )
        digest = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        identity_seed = "|".join(
            (
                self.ownership.active_chat_key,
                self.ownership.logical_chat_session_id,
                digest,
                normalized_memory_user_scope or "",
            ),
        )
        generation = 0
        while True:
            proposal_id = str(
                uuid5(
                    NAMESPACE_URL,
                    (
                        identity_seed
                        if generation == 0
                        else f"{identity_seed}|generation:{generation}"
                    ),
                ),
            )
            existing = self.store.get_entry_proposal(proposal_id)
            if existing is None:
                break
            if existing.status is EntryProposalStatus.PENDING:
                return existing
            generation += 1
        proposal = WPlusEntryProposal(
            proposal_id=proposal_id,
            ownership=self.ownership,
            logical_chat_session_id=self.ownership.logical_chat_session_id,
            original_request={"text": original_text},
            original_request_digest=f"sha256:{digest}",
            memory_user_scope=normalized_memory_user_scope,
            detection_mode=EntryDetectionMode(mode),
        )
        return self.store.create_entry_proposal(proposal)

    def _owned_proposal(self, proposal_id: str) -> WPlusEntryProposal:
        proposal = self.store.get_entry_proposal(proposal_id)
        if proposal is None or not _same_ownership(
            proposal.ownership,
            self.ownership,
        ):
            raise WPlusOwnershipError(proposal_id)
        return proposal

    async def _start_cumulative_continuation(
        self,
        *,
        sop_session_id: str,
        completed_run_id: str,
        completed_attempt_id: str,
    ) -> None:
        """Atomically settle cumulative work and claim its next Agent run."""

        await self._wait_for_owning_chat_idle()
        record = self._owned_record(sop_session_id)
        projection = record.projection
        if (
            projection.current_run_id != completed_run_id
            or not _cumulative_handoff_pending(record)
        ):
            return
        target_state = projection.state
        identity_seed = (
            f"{sop_session_id}|{completed_attempt_id}|cumulative-continuation"
        )
        run_id = f"run_{uuid5(NAMESPACE_URL, identity_seed + '|run').hex}"
        attempt_id = (
            f"attempt_{uuid5(NAMESPACE_URL, identity_seed + '|attempt').hex}"
        )
        command_request_id = f"cmd_cumulative_{completed_attempt_id}"
        result = _CommandResult(
            target_state=target_state,
            kind=EventKind.LIFECYCLE_PROGRESS,
            typed_payload=LifecycleProgressPayload(
                phase="agent_turn_handoff",
                message="上一 Agent 回合已完成，启动下一步。",
                run_id=run_id,
            ),
            starts_run=True,
        )
        self._augment_command_runtime_payload(projection, result)
        event = self._event(
            record,
            EventKind.LIFECYCLE_PROGRESS,
            result.typed_payload.model_dump(mode="json"),
            event_id=f"evt_cumulative_{completed_attempt_id}",
        )
        receipt = CommandReceipt(
            command_request_id=command_request_id,
            command=_CUMULATIVE_CONTINUATION_COMMAND,
            sop_session_id=sop_session_id,
            resulting_state_version=event.state_version,
            starts_run=True,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        attempt = RunAttempt(
            run_id=run_id,
            attempt_id=attempt_id,
            command_request_id=command_request_id,
            command=_CUMULATIVE_CONTINUATION_COMMAND,
            status=RunStatus.CLAIMED,
        )
        mutation = self.store.commit_event(
            sop_session_id,
            expected_state_version=projection.state_version,
            event=event,
            next_state=target_state,
            outbox_item=self._outbox(event),
            command_receipt=receipt,
            run_attempt=attempt,
            run_completion=(
                completed_run_id,
                completed_attempt_id,
                RunStatus.COMPLETED,
            ),
        )
        if mutation.duplicate:
            return
        await self._start_command_run(
            sop_session_id=sop_session_id,
            command=_CUMULATIVE_CONTINUATION_COMMAND,
            run_id=run_id,
            attempt_id=attempt_id,
            target_state=target_state,
            runtime_payload=result.runtime_payload,
            mutation=mutation,
        )

    async def _on_agent_turn_complete(
        self,
        *,
        sop_session_id: str,
        run_id: str,
        attempt_id: str,
        command: str,
    ) -> None:
        """Reconcile a finished TaskTracker run with the durable SOP state."""

        try:
            record = self._owned_record(sop_session_id)
            projection = record.projection
            exact_attempt = next(
                (
                    attempt
                    for attempt in record.runs
                    if attempt.run_id == run_id
                    and attempt.attempt_id == attempt_id
                ),
                None,
            )
            if exact_attempt is not None and exact_attempt.status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                await self.flush_chat_projection_outbox()
                return
            if projection.current_run_id != run_id:
                self.store.finish_run(
                    sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    status=RunStatus.COMPLETED,
                )
                return

            if projection.state is SessionState.PENDING_EXIT:
                terminate = projection.pending_exit_action == "terminate"
                if terminate:
                    target = SessionState.TERMINATED
                    typed_payload: Any = TerminationSummaryPayload(
                        summary=(
                            "用户请求彻底结束；当前 Agent 响应已完整落盘。"
                        ),
                    )
                    changes: dict[str, Any] = {
                        "pending_exit_action": None,
                        "termination_summary": typed_payload,
                    }
                    kind = EventKind.TERMINATION_SUMMARY
                else:
                    target = SessionState.PAUSED
                    typed_payload = SessionStateChangedPayload(
                        previous_state=projection.state,
                        state=target,
                        reason="agent_turn_completed_after_save_and_exit",
                    )
                    changes = {"pending_exit_action": None}
                    kind = EventKind.SESSION_STATE_CHANGED
                event = self._event(
                    record,
                    kind,
                    typed_payload.model_dump(mode="json"),
                    event_id=f"evt_run_boundary_{attempt_id}",
                )
                self.store.commit_event(
                    sop_session_id,
                    expected_state_version=projection.state_version,
                    event=event,
                    next_state=target,
                    projection_changes=changes,
                    outbox_item=self._outbox(event),
                )
                self.store.finish_run(
                    sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    status=(
                        RunStatus.CANCELLED
                        if terminate
                        else RunStatus.COMPLETED
                    ),
                )
            elif _cumulative_handoff_pending(record):
                await self._start_cumulative_continuation(
                    sop_session_id=sop_session_id,
                    completed_run_id=run_id,
                    completed_attempt_id=attempt_id,
                )
            elif projection.state in _ORPHAN_RECOVERY_STATES:
                self._record_runtime_failure(
                    sop_session_id=sop_session_id,
                    summary=(
                        "Agent turn completed without the required "
                        "structured W+ event"
                    ),
                    failed_operation=command,
                    failed_run_id=run_id,
                    failed_attempt_id=attempt_id,
                )
            else:
                status = (
                    RunStatus.FAILED
                    if (
                        projection.state is SessionState.RECOVERABLE_FAILURE
                        and projection.last_error is not None
                        and projection.last_error.failed_run_id == run_id
                    )
                    else RunStatus.COMPLETED
                )
                self.store.finish_run(
                    sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    status=status,
                )
            await self.flush_chat_projection_outbox()
        except Exception:
            logger.exception(
                "Failed to reconcile W+ Agent completion session=%s run=%s",
                sop_session_id,
                run_id,
            )

    async def confirm_entry(
        self,
        *,
        proposal_id: str,
        command_request_id: str,
        skill_snapshot_id: str,
    ) -> StoreMutation:
        proposal = self._owned_proposal(proposal_id)
        if proposal.status is EntryProposalStatus.CONFIRMED:
            existing = proposal.command_receipt
            if (
                existing is not None
                and existing.command_request_id == command_request_id
                and existing.sop_session_id
            ):
                await self.project_entry_proposal(proposal)
                return StoreMutation(
                    record=self._owned_record(existing.sop_session_id),
                    receipt=existing,
                    duplicate=True,
                )

        await self._wait_for_owning_chat_idle()
        chat = await self._verified_owned_chat()
        sop_session_id = f"sop_{uuid4().hex}"
        run_id = f"run_{uuid4().hex}"
        attempt_id = f"attempt_{uuid4().hex}"
        projection = SessionProjection(
            sop_session_id=sop_session_id,
            ownership=self.ownership,
            skill_snapshot_id=skill_snapshot_id,
            state=SessionState.GENERATING_STAGE_PROPOSAL,
            state_version=1,
            title="W+ SOP 澄清",
            memory_user_scope=proposal.memory_user_scope,
            current_run_id=run_id,
        )
        receipt = CommandReceipt(
            command_request_id=command_request_id,
            command="confirm_entry",
            sop_session_id=sop_session_id,
            resulting_state_version=1,
            starts_run=True,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        attempt = RunAttempt(
            run_id=run_id,
            attempt_id=attempt_id,
            command_request_id=command_request_id,
            command="confirm_entry",
            status=RunStatus.CLAIMED,
        )
        initial_event_id = f"evt_session_created_{sop_session_id}"
        initial_outbox = ChatProjectionOutboxItem(
            projection_event_id=f"chatproj_{initial_event_id}",
            sop_session_id=sop_session_id,
            chat_id=self.ownership.chat_id,
            event_id=initial_event_id,
            kind=EventKind.SESSION_STATE_CHANGED.value,
            payload={
                "state_version": projection.state_version,
                "entry_proposal_id": proposal_id,
                "kind": EventKind.SESSION_STATE_CHANGED.value,
                "payload": {
                    "previous_state": None,
                    "state": projection.state.value,
                    "reason": "entry_confirmed",
                },
            },
        )
        mutation = self.store.confirm_entry_proposal(
            proposal_id,
            projection=projection,
            receipt=receipt,
            run_attempt=attempt,
            outbox_item=initial_outbox,
        )
        if mutation.duplicate:
            return mutation

        confirmed_proposal = self.store.get_entry_proposal(proposal_id)
        if confirmed_proposal is not None:
            await self.project_entry_proposal(
                confirmed_proposal,
                verified_chat=chat,
            )
        original_text = str(
            (
                (proposal.original_request or {}).get("text", "")
                if isinstance(proposal.original_request, dict)
                else ""
            ),
        )
        try:
            await start_wplus_chat_turn(
                workspace=self.workspace,
                chat=chat,
                user_id=self.ownership.user_id,
                source_id=self.ownership.source_id,
                sop_session_id=sop_session_id,
                command="propose_stage_queue",
                payload={
                    "original_request": original_text,
                    "memory_user_scope": proposal.memory_user_scope,
                },
                run_id=run_id,
                attempt_id=attempt_id,
                target_state=SessionState.GENERATING_STAGE_PROPOSAL.value,
                on_complete=lambda: self._on_agent_turn_complete(
                    sop_session_id=sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    command="propose_stage_queue",
                ),
                before_start=lambda: self._assert_runtime_claim_active(
                    sop_session_id=sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                ),
            )
        except _WPlusRunClaimLostError:
            return StoreMutation(
                record=self._owned_record(sop_session_id),
                receipt=receipt,
            )
        except (RuntimeError, WPlusChatRunBusyError) as exc:
            self._record_runtime_failure(
                sop_session_id=sop_session_id,
                summary=str(exc),
                failed_operation="propose_stage_queue",
                failed_run_id=run_id,
                failed_attempt_id=attempt_id,
            )
            raise WPlusRuntimeStartError(str(exc)) from exc
        return mutation

    def reject_entry(
        self,
        *,
        proposal_id: str,
        command_request_id: str,
    ) -> WPlusEntryProposal:
        self._owned_proposal(proposal_id)
        token = f"suppress_{uuid4().hex}"
        receipt = CommandReceipt(
            command_request_id=command_request_id,
            command="reject_entry",
            sop_session_id=None,
        )
        return self.store.resolve_entry_proposal(
            proposal_id,
            status=EntryProposalStatus.REJECTED,
            receipt=receipt,
            suppression_token=token,
        )

    def validate_suppression(
        self,
        *,
        proposal_id: str,
        suppression_token: str,
        original_text: str,
    ) -> bool:
        try:
            self._owned_proposal(proposal_id)
        except WPlusOwnershipError:
            return False
        digest = (
            "sha256:"
            + hashlib.sha256(
                original_text.encode("utf-8"),
            ).hexdigest()
        )
        return self.store.suppression_matches(
            proposal_id,
            suppression_token=suppression_token,
            original_request_digest=digest,
            ownership=self.ownership,
        )

    def consume_suppression(
        self,
        *,
        proposal_id: str,
        claim_id: str,
        suppression_token: str,
        original_text: str,
    ) -> bool:
        digest = (
            "sha256:"
            + hashlib.sha256(
                original_text.encode("utf-8"),
            ).hexdigest()
        )
        return self.store.consume_suppression(
            proposal_id,
            claim_id=claim_id,
            suppression_token=suppression_token,
            original_request_digest=digest,
            ownership=self.ownership,
        )

    def claim_suppression(
        self,
        *,
        proposal_id: str,
        suppression_token: str,
        original_text: str,
    ) -> str | None:
        digest = (
            "sha256:"
            + hashlib.sha256(
                original_text.encode("utf-8"),
            ).hexdigest()
        )
        return self.store.claim_suppression(
            proposal_id,
            suppression_token=suppression_token,
            original_request_digest=digest,
            ownership=self.ownership,
        )

    def release_suppression_claim(
        self,
        *,
        proposal_id: str,
        claim_id: str,
    ) -> bool:
        return self.store.release_suppression_claim(
            proposal_id,
            claim_id=claim_id,
            ownership=self.ownership,
        )

    def _record_runtime_failure(
        self,
        *,
        sop_session_id: str,
        summary: str,
        failed_operation: str,
        failed_run_id: str | None,
        failed_attempt_id: str | None,
    ) -> None:
        record = self._owned_record(sop_session_id)
        payload = RecoverableFailurePayload(
            error_code="runtime_start_failed",
            summary=summary or "Agent runtime could not start",
            failed_operation=failed_operation,
            failed_run_id=failed_run_id,
        )
        event = self._event(
            record,
            EventKind.RECOVERABLE_FAILURE,
            payload.model_dump(mode="json"),
        )
        self.store.commit_event(
            sop_session_id,
            expected_state_version=record.projection.state_version,
            event=event,
            next_state=SessionState.RECOVERABLE_FAILURE,
            projection_changes={
                "last_error": payload,
                "resume_state": record.projection.state,
            },
            outbox_item=self._outbox(event),
            run_completion=(
                (
                    failed_run_id,
                    failed_attempt_id,
                    RunStatus.FAILED,
                )
                if failed_run_id is not None and failed_attempt_id is not None
                else None
            ),
        )

    @staticmethod
    def _event(
        record: SessionRecord,
        kind: EventKind,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> StructuredInteractionEnvelope:
        projection = record.projection
        return StructuredInteractionEnvelope(
            event_id=event_id or f"evt_{uuid4().hex}",
            sop_session_id=projection.sop_session_id,
            chat_id=projection.chat_id,
            revision=projection.revision,
            round=projection.round,
            state_version=projection.state_version + 1,
            kind=kind,
            payload=payload,
        )

    @staticmethod
    def _outbox(
        event: StructuredInteractionEnvelope,
    ) -> ChatProjectionOutboxItem:
        return ChatProjectionOutboxItem(
            projection_event_id=f"chatproj_{event.event_id}",
            sop_session_id=event.sop_session_id,
            chat_id=event.chat_id,
            event_id=event.event_id,
            kind=event.kind.value,
            payload={
                "state_version": event.state_version,
                "kind": event.kind.value,
                "payload": event.payload.model_dump(mode="json"),
            },
        )

    @staticmethod
    def _normalize_stages(raw: Any) -> list[Stage]:
        if not isinstance(raw, list):
            raise WPlusCommandError("stages must be an array")
        stages = [
            Stage(
                stage_id=str(item.get("stage_id", "")),
                name=str(item.get("title", item.get("name", ""))),
                description=item.get("description"),
            )
            for item in raw
            if isinstance(item, dict)
        ]
        return StageQueue(stages=stages).stages

    @staticmethod
    def _answers_payload(
        record: SessionRecord,
        payload: dict[str, Any],
    ) -> AnswerAcceptedPayload:
        batch = record.projection.current_question_batch
        if batch is None:
            raise WPlusCommandError("No current question batch")
        raw_answers = payload.get("answers")
        if not isinstance(raw_answers, dict):
            raise WPlusCommandError("answers must be an object")
        answers: list[QuestionAnswer] = []
        for question in batch.questions:
            value = raw_answers.get(question.question_id)
            answer = WPlusSopService._question_answer(question, value)
            answers.append(answer)
        return AnswerAcceptedPayload(
            batch_id=batch.batch_id,
            stage_id=batch.stage_id,
            answers=answers,
        )

    @staticmethod
    def _questions_for_answer_batch(
        record: SessionRecord,
        answer_batch: AnswerBatch,
    ) -> dict[str, Question]:
        """Recover the immutable question contract used by a saved answer."""
        for event in reversed(record.events):
            if (
                event.kind is EventKind.QUESTION_BATCH
                and isinstance(event.payload, QuestionBatchPayload)
                and event.payload.batch_id == answer_batch.batch_id
            ):
                return {
                    question.question_id: question
                    for question in event.payload.questions
                }
        return {}

    @staticmethod
    def _structured_question_answer(
        question_id: str,
        value: dict[str, Any],
    ) -> QuestionAnswer:
        unexpected = set(value) - {"selected_option_ids", "text"}
        if unexpected:
            raise WPlusCommandError(
                "structured answers only allow selected_option_ids and text",
            )
        selected = value.get("selected_option_ids", [])
        if not isinstance(selected, list) or any(
            not isinstance(item, str) or not item for item in selected
        ):
            raise WPlusCommandError(
                "selected_option_ids must be an array of non-empty strings",
            )
        if len(selected) != len(set(selected)):
            raise WPlusCommandError("selected_option_ids must be unique")
        text = value.get("text")
        if text is not None and not isinstance(text, str):
            raise WPlusCommandError("answer text must be a string")
        try:
            return QuestionAnswer(
                question_id=question_id,
                selected_option_ids=selected,
                text=text,
            )
        except ValueError as exc:
            raise WPlusCommandError(str(exc)) from exc

    @staticmethod
    def _validated_structured_question_answer(
        question: Question,
        value: dict[str, Any],
    ) -> QuestionAnswer:
        selected = value.get("selected_option_ids", [])
        if not isinstance(selected, list):
            raise WPlusCommandError(
                "selected_option_ids must be an array of non-empty strings",
            )
        if question.type.value == "single_select" and len(selected) != 1:
            raise WPlusCommandError(
                "single_select answers require exactly one selected option",
            )
        if question.type.value == "multi_select" and not selected:
            raise WPlusCommandError(
                "multi_select answers require at least one selected option",
            )
        answer = WPlusSopService._structured_question_answer(
            question.question_id,
            value,
        )
        option_ids = {option.option_id for option in question.options}
        unknown = set(answer.selected_option_ids) - option_ids
        if unknown:
            raise WPlusCommandError(
                "selected option IDs must belong to the current question",
            )
        if question.type.value == "free_text" and answer.selected_option_ids:
            raise WPlusCommandError(
                "free_text answers cannot contain selected option IDs",
            )
        return answer

    @staticmethod
    def _legacy_question_answer(
        question: Question,
        value: Any,
    ) -> QuestionAnswer:
        if isinstance(value, list):
            return QuestionAnswer(
                question_id=question.question_id,
                selected_option_ids=[str(item) for item in value],
            )
        if question.type.value == "single_select":
            return QuestionAnswer(
                question_id=question.question_id,
                selected_option_ids=[str(value or "")],
            )
        return QuestionAnswer(
            question_id=question.question_id,
            text=str(value or ""),
        )

    @staticmethod
    def _validate_custom_answer_text(
        question: Question,
        answer: QuestionAnswer,
    ) -> None:
        custom_option_ids = {
            option.option_id
            for option in question.options
            if option.requires_custom_input
        }
        if (
            custom_option_ids.intersection(answer.selected_option_ids)
            and not (answer.text or "").strip()
        ):
            raise WPlusCommandError(
                "selected option requires non-empty custom input text",
            )

    @staticmethod
    def _question_answer(question: Question, value: Any) -> QuestionAnswer:
        answer = (
            WPlusSopService._validated_structured_question_answer(
                question,
                value,
            )
            if isinstance(value, dict)
            else WPlusSopService._legacy_question_answer(question, value)
        )
        WPlusSopService._validate_custom_answer_text(question, answer)
        return answer

    async def execute_command(
        self,
        *,
        sop_session_id: str,
        command: str,
        command_request_id: str,
        expected_state_version: int,
        payload: dict[str, Any],
    ) -> StoreMutation:
        record = self._owned_record(sop_session_id)
        projection = record.projection
        existing_receipt = record.command_receipts.get(command_request_id)
        if existing_receipt is not None:
            if existing_receipt.command != command:
                raise WPlusCommandError(
                    "command_request_id was already used for another command",
                )
            return StoreMutation(
                record=record,
                receipt=existing_receipt,
                duplicate=True,
            )
        if projection.state_version != expected_state_version:
            raise StaleStateVersionError(
                f"Expected {expected_state_version}, "
                f"found {projection.state_version}",
            )

        handler = _COMMAND_HANDLERS.get(command)
        if handler is None:
            raise WPlusCommandError(f"Unsupported command: {command}")
        result = handler(self, record, payload, command)
        if result.starts_run and not result.runtime_payload:
            result.runtime_payload = dict(payload)
        self._augment_command_runtime_payload(projection, result)
        return await self._execute_command_run_lifecycle(
            sop_session_id=sop_session_id,
            command=command,
            command_request_id=command_request_id,
            expected_state_version=expected_state_version,
            record=record,
            projection=projection,
            result=result,
            raw_payload=payload,
        )

    # ------------------------------------------------------------------
    # Command dispatch handlers (one per command / command group)
    # ------------------------------------------------------------------

    @staticmethod
    def _dispatch_confirm_stage_queue(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.AWAITING_QUEUE_CONFIRMATION:
            raise WPlusCommandError("Stage queue is not awaiting confirmation")
        stages = svc._normalize_stages(payload.get("stages"))
        stages[0].status = StageStatus.CLARIFYING
        return _CommandResult(
            target_state=SessionState.GENERATING_QUESTIONS,
            kind=EventKind.STAGE_QUEUE_CONFIRMED,
            typed_payload=StageQueueConfirmedPayload(stages=stages),
            changes={
                "stages": stages,
                "current_stage_id": stages[0].stage_id,
            },
            starts_run=True,
        )

    @staticmethod
    def _dispatch_submit_answers(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.AWAITING_ANSWER:
            raise WPlusCommandError("Session is not awaiting answers")
        typed_payload = svc._answers_payload(record, payload)
        return _CommandResult(
            target_state=SessionState.GENERATING_QUESTIONS,
            kind=EventKind.ANSWER_ACCEPTED,
            typed_payload=typed_payload,
            changes={
                "answers": [*projection.answers, typed_payload],
                "round": projection.round + 1,
            },
            starts_run=True,
        )

    @staticmethod
    def _dispatch_submit_trial_feedback(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state not in {
            SessionState.AWAITING_TRIAL_FEEDBACK,
            SessionState.AWAITING_STAGE_CONFIRMATION,
        }:
            raise WPlusCommandError("Session is not awaiting trial feedback")
        feedback = str(payload.get("feedback", "")).strip()
        rerun_of = str(
            payload.get("rerun_of_run_id") or projection.current_run_id or "",
        )
        if not feedback or not rerun_of:
            raise WPlusCommandError("feedback and prior run are required")
        next_action = str(payload.get("next_action") or "rerun").strip()
        if next_action not in {"clarify", "rerun"}:
            raise WPlusCommandError("Unsupported trial feedback next_action")
        target_state = (
            SessionState.GENERATING_QUESTIONS
            if next_action == "clarify"
            else SessionState.GENERATING_TRIAL
        )
        return _CommandResult(
            target_state=target_state,
            kind=EventKind.TRIAL_FEEDBACK_ACCEPTED,
            typed_payload=TrialFeedbackAcceptedPayload(
                feedback=feedback,
                prior_run_id=rerun_of,
                rerun_id="pending",
            ),
            changes={
                "trial_feedback": [*projection.trial_feedback, feedback],
                "trial_result_lists": [],
            },
            rerun_of=rerun_of if next_action == "rerun" else None,
            starts_run=True,
        )

    @staticmethod
    def _dispatch_accept_trial(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.AWAITING_TRIAL_FEEDBACK:
            raise WPlusCommandError("Session is not awaiting trial feedback")
        if not projection.current_stage_id:
            raise WPlusCommandError("Current stage is missing")
        return _CommandResult(
            target_state=SessionState.GENERATING_STAGE_REPORT,
            kind=EventKind.STAGE_CONFIRMATION_REQUIRED,
            typed_payload=StageConfirmationRequiredPayload(
                stage_id=projection.current_stage_id,
                summary="用户接受当前预跑结果，生成环节报告，等待环节确认。",
            ),
            starts_run=True,
        )

    @staticmethod
    def _dispatch_confirm_stage(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.AWAITING_STAGE_CONFIRMATION:
            raise WPlusCommandError("Stage is not awaiting confirmation")
        current_id = projection.current_stage_id
        if not current_id:
            raise WPlusCommandError("Current stage is missing")
        stages = [stage.model_copy(deep=True) for stage in projection.stages]
        current_index = next(
            (
                index
                for index, stage in enumerate(stages)
                if stage.stage_id == current_id
            ),
            -1,
        )
        if current_index < 0:
            raise WPlusCommandError("Current stage is not in the queue")
        candidates = [
            report
            for report in projection.stage_reports
            if (
                report.stage_id == current_id
                and report.revision == projection.revision
                and report.superseded_by is None
            )
        ]
        if not candidates:
            raise WPlusCommandError(
                "Current stage has no acceptable report to confirm",
            )
        latest = max(candidates, key=lambda report: report.report_no)
        json_artifact = next(
            artifact
            for artifact in latest.artifacts
            if artifact.artifact_id == "stage_sop_json"
        )
        stages[current_index].status = StageStatus.CONFIRMED
        is_final = current_index == len(stages) - 1
        next_stage_id = None
        if not is_final:
            stages[current_index + 1].status = StageStatus.CLARIFYING
            next_stage_id = stages[current_index + 1].stage_id
        snapshots = [
            *projection.confirmed_snapshots,
            ConfirmedStageSnapshot(
                stage_id=current_id,
                report_no=latest.report_no,
                revision=latest.revision,
                artifact_sha256=json_artifact.sha256,
            ),
        ]
        return _CommandResult(
            target_state=SessionState.REFRESHING_CUMULATIVE,
            kind=EventKind.STAGE_CONFIRMED,
            typed_payload=StageConfirmedPayload(
                stage_id=current_id,
                next_stage_id=next_stage_id,
                is_final_stage=is_final,
                confirmed_report_no=latest.report_no,
            ),
            changes={
                "stages": stages,
                "current_stage_id": next_stage_id or current_id,
                "current_question_batch": None,
                "confirmed_snapshots": snapshots,
                "cumulative_preview": None,
            },
            starts_run=True,
        )

    @staticmethod
    def _dispatch_revise_answer(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state not in {
            SessionState.AWAITING_ANSWER,
            SessionState.AWAITING_TRIAL_FEEDBACK,
            SessionState.AWAITING_STAGE_CONFIRMATION,
        }:
            raise WPlusCommandError(
                "Answers can only be revised from a stable active state",
            )
        revised_round = int(payload.get("revised_round") or 0)
        if revised_round < 1 or revised_round > len(projection.answers):
            raise WPlusCommandError("Invalid revised_round")
        previous = projection.answers[revised_round - 1]
        current_stage = next(
            (
                stage
                for stage in projection.stages
                if stage.stage_id == projection.current_stage_id
            ),
            None,
        )
        if (
            previous.stage_id != projection.current_stage_id
            or current_stage is None
            or current_stage.status is StageStatus.CONFIRMED
        ):
            raise WPlusCommandError(
                "Answers can only be revised for the current unconfirmed stage",
            )
        raw_answers = payload.get("answers")
        if not isinstance(raw_answers, dict):
            raise WPlusCommandError("answers must be an object")
        replacement_answers = _build_revised_answers(
            svc,
            record,
            previous,
            raw_answers,
        )
        replacement = AnswerBatch(
            batch_id=previous.batch_id,
            stage_id=previous.stage_id,
            answers=replacement_answers,
        )
        invalidated_event_ids = [
            event.event_id
            for event in record.events
            if event.round >= revised_round
        ]
        typed_payload = RevisionAppliedPayload(
            revised_round=revised_round,
            invalidated_event_ids=invalidated_event_ids,
            reason=str(payload.get("reason") or "user_revised_answer"),
        )
        stages = [stage.model_copy(deep=True) for stage in projection.stages]
        revised_stage_seen = False
        for stage in stages:
            if stage.stage_id == previous.stage_id:
                stage.status = StageStatus.CLARIFYING
                revised_stage_seen = True
            elif revised_stage_seen:
                stage.status = StageStatus.PENDING
        return _CommandResult(
            target_state=SessionState.GENERATING_QUESTIONS,
            kind=EventKind.REVISION_APPLIED,
            typed_payload=typed_payload,
            changes={
                "revision": projection.revision + 1,
                "round": revised_round,
                "answers": [
                    *projection.answers[: revised_round - 1],
                    replacement,
                ],
                "invalidated_history": [
                    *projection.invalidated_history,
                    {
                        "revision": projection.revision,
                        "revised_round": revised_round,
                        "invalidated_event_ids": invalidated_event_ids,
                        "answers": [
                            answer.model_dump(mode="json")
                            for answer in projection.answers[
                                revised_round - 1 :
                            ]
                        ],
                        "trial_result_lists": [
                            result.model_dump(mode="json")
                            for result in projection.trial_result_lists
                        ],
                        "final_result": (
                            projection.final_result.model_dump(mode="json")
                            if projection.final_result is not None
                            else None
                        ),
                        "memory_candidates": [
                            candidate.model_dump(mode="json")
                            for candidate in projection.memory_candidates
                        ],
                    },
                ],
                "stages": stages,
                "current_stage_id": previous.stage_id,
                "current_question_batch": None,
                "trial_result_lists": [],
                "trial_feedback": [],
                "confirmed_facts": [],
                "unknowns": [],
                "final_result": None,
                "memory_candidates": [],
                "last_error": None,
            },
            starts_run=True,
        )

    @staticmethod
    def _dispatch_save_and_exit(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        terminate = command == "terminate"
        if projection.state in {
            SessionState.COMPLETED,
            SessionState.TERMINATED,
        }:
            raise WPlusCommandError("Session is already terminal")
        if projection.state is SessionState.PENDING_EXIT:
            raise WPlusCommandError("Session already has a pending exit")
        if (
            command == "save_and_exit"
            and projection.state is SessionState.PAUSED
        ):
            raise WPlusCommandError("Session is already paused")
        generating = projection.state in {
            SessionState.GENERATING_STAGE_PROPOSAL,
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.EXECUTING_TRIAL,
            SessionState.FINALIZING_OUTPUTS,
            SessionState.WRITING_MEMORY,
        }
        target_state = (
            SessionState.PENDING_EXIT
            if generating
            else (
                SessionState.TERMINATED if terminate else SessionState.PAUSED
            )
        )
        kind = (
            EventKind.TERMINATION_SUMMARY
            if target_state is SessionState.TERMINATED
            else EventKind.SESSION_STATE_CHANGED
        )
        if kind is EventKind.TERMINATION_SUMMARY:
            typed_payload: Any = TerminationSummaryPayload(
                summary="用户彻底结束 W+ SOP 会话。",
            )
            changes: dict[str, Any] = {
                "termination_summary": typed_payload,
            }
        else:
            typed_payload = SessionStateChangedPayload(
                previous_state=projection.state,
                state=target_state,
                reason=command,
            )
            changes = {
                "resume_state": projection.state,
                "pending_exit_action": (
                    "terminate"
                    if terminate
                    else (
                        "pause"
                        if target_state is SessionState.PENDING_EXIT
                        else None
                    )
                ),
            }
        return _CommandResult(
            target_state=target_state,
            kind=kind,
            typed_payload=typed_payload,
            changes=changes,
        )

    @staticmethod
    def _dispatch_cancel_run_and_pause(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.PENDING_EXIT:
            raise WPlusCommandError("Session has no pending exit")
        if command == "continue_waiting":
            return _CommandResult(
                target_state=SessionState.PENDING_EXIT,
                kind=EventKind.SESSION_STATE_CHANGED,
                typed_payload=SessionStateChangedPayload(
                    previous_state=projection.state,
                    state=SessionState.PENDING_EXIT,
                    reason="continue_waiting",
                ),
            )
        return _CommandResult(
            target_state=SessionState.PAUSED,
            kind=EventKind.SESSION_STATE_CHANGED,
            typed_payload=SessionStateChangedPayload(
                previous_state=projection.state,
                state=SessionState.PAUSED,
                reason="cancel_run_and_pause",
            ),
            changes={
                "resume_state": (
                    projection.resume_state
                    if projection.resume_state is not SessionState.PENDING_EXIT
                    else SessionState.RECOVERABLE_FAILURE
                ),
                "pending_exit_action": None,
            },
            cancel_active_run=True,
        )

    @staticmethod
    def _dispatch_resume(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.PAUSED:
            raise WPlusCommandError("Session is not paused")
        target_state = (
            projection.resume_state or SessionState.RECOVERABLE_FAILURE
        )
        if target_state is SessionState.PENDING_EXIT:
            target_state = SessionState.RECOVERABLE_FAILURE
        starts_run = target_state in {
            SessionState.GENERATING_STAGE_PROPOSAL,
            SessionState.GENERATING_QUESTIONS,
            SessionState.GENERATING_TRIAL,
            SessionState.FINALIZING_OUTPUTS,
            SessionState.WRITING_MEMORY,
        }
        return _CommandResult(
            target_state=target_state,
            kind=EventKind.SESSION_STATE_CHANGED,
            typed_payload=SessionStateChangedPayload(
                previous_state=projection.state,
                state=target_state,
                reason="resume",
            ),
            changes={"resume_state": None, "pending_exit_action": None},
            starts_run=starts_run,
        )

    @staticmethod
    def _dispatch_retry_current_turn(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.RECOVERABLE_FAILURE:
            raise WPlusCommandError("Session has no recoverable failure")
        target_state = projection.resume_state
        failed_run_id = (
            projection.last_error.failed_run_id
            if projection.last_error is not None
            else None
        )
        failed_attempts = [
            attempt
            for attempt in record.runs
            if attempt.run_id == failed_run_id
            and attempt.status is RunStatus.FAILED
        ]
        if (
            target_state not in _ORPHAN_RECOVERY_STATES
            or failed_run_id is None
            or len(failed_attempts) != 1
        ):
            raise WPlusCommandError(
                "Recoverable failure has no valid server-owned retry target",
            )
        return _CommandResult(
            target_state=target_state,
            kind=EventKind.SESSION_STATE_CHANGED,
            typed_payload=SessionStateChangedPayload(
                previous_state=projection.state,
                state=target_state,
                reason="retry_current_turn",
            ),
            changes={"last_error": None, "resume_state": None},
            retry_of=failed_run_id,
            starts_run=True,
            runtime_payload={
                "target_state": target_state.value,
                "retry_of_run_id": failed_run_id,
            },
        )

    @staticmethod
    def _dispatch_confirm_outputs(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.OUTPUT_REVIEW:
            raise WPlusCommandError(
                "Session outputs are not awaiting confirmation",
            )
        target_state = (
            SessionState.MEMORY_REVIEW
            if projection.memory_candidates
            else SessionState.COMPLETED
        )
        return _CommandResult(
            target_state=target_state,
            kind=EventKind.SESSION_STATE_CHANGED,
            typed_payload=SessionStateChangedPayload(
                previous_state=projection.state,
                state=target_state,
                reason="outputs_confirmed",
            ),
        )

    @staticmethod
    def _dispatch_resolve_or_skip_memory(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        command: str,
    ) -> _CommandResult:
        projection = record.projection
        if projection.state is not SessionState.MEMORY_REVIEW:
            raise WPlusCommandError("Session is not reviewing memory")
        candidates = [
            candidate.model_copy(deep=True)
            for candidate in projection.memory_candidates
        ]
        if command == "skip_memory":
            decisions = {
                candidate.candidate_id: "reject"
                for candidate in candidates
                if candidate.status
                in {
                    MemoryCandidateStatus.PENDING,
                    MemoryCandidateStatus.FAILED,
                }
            }
        else:
            decisions = _parse_memory_decisions(payload, candidates)
        approved_payloads: list[dict[str, Any]] = []
        active_ids: list[str] = []
        for index, candidate in enumerate(candidates):
            decision = decisions.get(candidate.candidate_id)
            if decision is None:
                continue
            if decision == "approve":
                approved_payloads.append(
                    _memory_runtime_candidate(candidate),
                )
                active_ids.append(candidate.candidate_id)
                status = MemoryCandidateStatus.WRITING
            else:
                status = MemoryCandidateStatus.REJECTED
            candidates[index] = candidate.model_copy(
                update={
                    "status": status,
                    "failure_reason": None,
                    "write_receipt": None,
                },
            )
        starts_run = bool(active_ids)
        return _CommandResult(
            target_state=(
                SessionState.WRITING_MEMORY
                if starts_run
                else SessionState.COMPLETED
            ),
            kind=EventKind.MEMORY_CANDIDATES,
            typed_payload=MemoryCandidatesPayload(candidates=candidates),
            changes={
                "memory_candidates": candidates,
                "active_memory_candidate_id": None,
                "active_memory_candidate_ids": active_ids,
            },
            starts_run=starts_run,
            runtime_payload=(
                {"candidates": approved_payloads} if starts_run else None
            ),
        )

    # ------------------------------------------------------------------
    # Post-dispatch augmentation and run lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _augment_command_runtime_payload(
        projection: SessionProjection,
        result: _CommandResult,
    ) -> None:
        """Enrich runtime_payload with stage / memory / finalization hints."""
        if not result.starts_run:
            return
        if result.target_state is SessionState.GENERATING_QUESTIONS:
            current_stage_id = result.changes.get(
                "current_stage_id",
                projection.current_stage_id,
            )
            if not isinstance(current_stage_id, str) or not current_stage_id:
                raise WPlusCommandError(
                    "Question generation requires a current_stage_id",
                )
            result.runtime_payload["current_stage_id"] = current_stage_id
            return
        if result.target_state is SessionState.REFRESHING_CUMULATIVE:
            current_stage_id = result.changes.get(
                "current_stage_id",
                projection.current_stage_id,
            )
            confirmed_snapshots = result.changes.get(
                "confirmed_snapshots",
                projection.confirmed_snapshots,
            )
            result.runtime_payload["current_stage_id"] = current_stage_id
            result.runtime_payload["confirmed_snapshots"] = [
                snapshot.model_dump(mode="json")
                for snapshot in confirmed_snapshots
            ]
            return
        if result.target_state is SessionState.WRITING_MEMORY:
            if "candidates" not in result.runtime_payload:
                active_ids = projection.active_memory_candidate_ids or (
                    [projection.active_memory_candidate_id]
                    if projection.active_memory_candidate_id
                    else []
                )
                active_candidates = [
                    candidate
                    for candidate in projection.memory_candidates
                    if candidate.candidate_id in active_ids
                ]
                if len(active_candidates) != len(active_ids):
                    raise WPlusCommandError(
                        "Memory run has no server-bound candidates",
                    )
                result.runtime_payload["candidates"] = [
                    _memory_runtime_candidate(c) for c in active_candidates
                ]
            return
        if result.target_state is SessionState.FINALIZING_OUTPUTS:
            result.runtime_payload.update(
                {
                    "final_result_persisted": (
                        projection.final_result is not None
                    ),
                    "memory_user_scope_available": bool(
                        projection.memory_user_scope,
                    ),
                },
            )

    async def _execute_command_run_lifecycle(
        self,
        *,
        sop_session_id: str,
        command: str,
        command_request_id: str,
        expected_state_version: int,
        record: SessionRecord,
        projection: SessionProjection,
        result: _CommandResult,
        raw_payload: dict[str, Any],
    ) -> StoreMutation:
        """Build event/receipt, commit, cancel active runs, and start a turn."""
        if result.starts_run:
            await self._wait_for_owning_chat_idle()
            await self._verified_owned_chat()
        run_id, attempt_id = self._init_command_run_ids(result)
        self._apply_command_run_tweaks(result, run_id)

        if result.cancel_active_run:
            await self._cancel_active_chat_run()

        mutation = self._commit_command_event(
            sop_session_id=sop_session_id,
            command=command,
            command_request_id=command_request_id,
            expected_state_version=expected_state_version,
            record=record,
            projection=projection,
            result=result,
            raw_payload=raw_payload,
            run_id=run_id,
            attempt_id=attempt_id,
        )

        if result.cancel_active_run and projection.current_run_id:
            self._cancel_stored_active_run(
                sop_session_id,
                record,
                projection,
            )

        if mutation.duplicate or not result.starts_run:
            return mutation
        if run_id is None or attempt_id is None:
            raise WPlusOwnershipError(self.ownership.chat_id)
        return await self._start_command_run(
            sop_session_id=sop_session_id,
            command=command,
            run_id=run_id,
            attempt_id=attempt_id,
            target_state=result.target_state,
            runtime_payload=result.runtime_payload,
            mutation=mutation,
        )

    def _init_command_run_ids(
        self,
        result: _CommandResult,
    ) -> tuple[str | None, str | None]:
        run_id = f"run_{uuid4().hex}" if result.starts_run else None
        attempt_id = f"attempt_{uuid4().hex}" if result.starts_run else None
        return run_id, attempt_id

    def _apply_command_run_tweaks(
        self,
        result: _CommandResult,
        run_id: str | None,
    ) -> None:
        if run_id is not None:
            result.changes["current_run_id"] = run_id
        if (
            isinstance(result.typed_payload, TrialFeedbackAcceptedPayload)
            and run_id
        ):
            result.typed_payload = result.typed_payload.model_copy(
                update={"rerun_id": run_id},
            )

    def _commit_command_event(
        self,
        *,
        sop_session_id: str,
        command: str,
        command_request_id: str,
        expected_state_version: int,
        record: SessionRecord,
        projection: SessionProjection,
        result: _CommandResult,
        raw_payload: dict[str, Any],
        run_id: str | None,
        attempt_id: str | None,
    ) -> StoreMutation:
        event = self._event(
            record,
            result.kind,
            result.typed_payload.model_dump(mode="json"),
        )
        event = _adjust_command_event(event, command, projection, raw_payload)
        receipt = CommandReceipt(
            command_request_id=command_request_id,
            command=command,
            sop_session_id=sop_session_id,
            resulting_state_version=event.state_version,
            starts_run=result.starts_run,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        attempt = (
            RunAttempt(
                run_id=run_id,
                attempt_id=attempt_id,
                command_request_id=command_request_id,
                command=command,
                status=RunStatus.CLAIMED,
                retry_of_run_id=result.retry_of,
                rerun_of_run_id=result.rerun_of,
            )
            if run_id and attempt_id
            else None
        )
        return self.store.commit_event(
            sop_session_id,
            expected_state_version=expected_state_version,
            event=event,
            next_state=result.target_state,
            projection_changes=result.changes,
            outbox_item=self._outbox(event),
            command_receipt=receipt,
            run_attempt=attempt,
        )

    async def _cancel_active_chat_run(self) -> None:
        try:
            coordinator = getattr(
                self.workspace,
                "answer_turn_coordinator",
                None,
            )
            identity = (
                await coordinator.current_identity(self.ownership.chat_id)
                if coordinator is not None
                else None
            )
            if coordinator is None or identity is None:
                return
            claim = await coordinator.claim_stop(identity, internal=True)
            if not claim.accepted:
                raise RuntimeError("active run is not stoppable")
        except Exception as exc:
            raise WPlusCommandError(
                "The active run could not be cancelled",
            ) from exc

    def _cancel_stored_active_run(
        self,
        sop_session_id: str,
        record: SessionRecord,
        projection: SessionProjection,
    ) -> None:
        active_attempt = next(
            (
                item
                for item in record.runs
                if item.run_id == projection.current_run_id
                and item.status in {RunStatus.CLAIMED, RunStatus.RUNNING}
            ),
            None,
        )
        if active_attempt is None:
            return
        try:
            self.store.finish_run(
                sop_session_id,
                run_id=active_attempt.run_id,
                attempt_id=active_attempt.attempt_id,
                status=RunStatus.CANCELLED,
            )
        except WPlusSopStoreError:
            logger.info(
                "W+ run settled concurrently while cancelling "
                "session=%s run=%s",
                sop_session_id,
                active_attempt.run_id,
            )

    async def _start_command_run(
        self,
        *,
        sop_session_id: str,
        command: str,
        run_id: str,
        attempt_id: str,
        target_state: SessionState,
        runtime_payload: dict[str, Any],
        mutation: StoreMutation,
    ) -> StoreMutation:
        chat = await self._verified_owned_chat()
        try:
            await start_wplus_chat_turn(
                workspace=self.workspace,
                chat=chat,
                user_id=self.ownership.user_id,
                source_id=self.ownership.source_id,
                sop_session_id=sop_session_id,
                command=command,
                payload=runtime_payload,
                run_id=run_id,
                attempt_id=attempt_id,
                target_state=target_state.value,
                on_complete=lambda: self._on_agent_turn_complete(
                    sop_session_id=sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    command=command,
                ),
                before_start=lambda: self._assert_runtime_claim_active(
                    sop_session_id=sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                ),
            )
        except _WPlusRunClaimLostError:
            return StoreMutation(
                record=self._owned_record(sop_session_id),
                receipt=mutation.receipt,
            )
        except (RuntimeError, WPlusChatRunBusyError) as exc:
            self._record_runtime_failure(
                sop_session_id=sop_session_id,
                summary=str(exc),
                failed_operation=command,
                failed_run_id=run_id,
                failed_attempt_id=attempt_id,
            )
            raise WPlusRuntimeStartError(str(exc)) from exc
        return mutation

    def append_agent_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        event_key: str,
        trusted_sop_session_id: str | None = None,
        trusted_run_id: str | None = None,
        trusted_attempt_id: str | None = None,
    ) -> StoreMutation:
        record = self._resolve_agent_event_record(
            trusted_sop_session_id=trusted_sop_session_id,
            trusted_run_id=trusted_run_id,
            trusted_attempt_id=trusted_attempt_id,
        )
        if record is None:
            raise WPlusOwnershipError("No active W+ Session for this Chat")
        try:
            event_kind = EventKind(kind)
        except ValueError as exc:
            raise WPlusCommandError(
                f"Unsupported event kind: {kind}",
            ) from exc

        state = record.projection.state
        effective_state = _effective_agent_event_state(record.projection)

        idem_result = self._check_agent_event_idempotency(
            record,
            event_kind,
            payload,
            effective_state,
            event_key,
        )
        if idem_result is not None:
            return idem_result
        if _cumulative_handoff_pending(record):
            raise WPlusCommandError(
                "Agent run must complete before the next step",
            )

        _validate_agent_event_state(event_kind, effective_state, state)

        result = _AGENT_EVENT_HANDLERS[event_kind](
            self,
            record,
            payload,
            trusted_run_id,
            effective_state,
            event_kind,
        )
        _resolve_pending_exit_target(record, event_kind, result, state)

        event = self._event(
            record,
            event_kind,
            result.typed_payload.model_dump(mode="json"),
            event_id=_stable_agent_event_id(record, event_key),
        )
        return self.store.commit_event(
            record.projection.sop_session_id,
            expected_state_version=record.projection.state_version,
            event=event,
            next_state=result.target,
            projection_changes=result.changes,
            outbox_item=self._outbox(event),
        )

    # ------------------------------------------------------------------
    # Agent event resolution / idempotency / validation
    # ------------------------------------------------------------------

    def _resolve_agent_event_record(
        self,
        *,
        trusted_sop_session_id: str | None,
        trusted_run_id: str | None,
        trusted_attempt_id: str | None,
    ) -> SessionRecord | None:
        trusted_values = (
            trusted_sop_session_id,
            trusted_run_id,
            trusted_attempt_id,
        )
        if not any(trusted_values):
            return self.get_active_session()
        if not all(trusted_values):
            raise WPlusCommandError("Incomplete trusted W+ run identity")
        assert trusted_sop_session_id is not None
        assert trusted_run_id is not None
        assert trusted_attempt_id is not None
        record = self._owned_record(trusted_sop_session_id)
        if record.projection.current_run_id != trusted_run_id:
            raise WPlusCommandError(
                "W+ event run does not match the current claimed run",
            )
        attempt = next(
            (
                item
                for item in record.runs
                if item.run_id == trusted_run_id
                and item.attempt_id == trusted_attempt_id
            ),
            None,
        )
        if attempt is None or attempt.status not in {
            RunStatus.CLAIMED,
            RunStatus.RUNNING,
        }:
            raise WPlusCommandError("W+ event attempt is not active")
        return record

    def _check_agent_event_idempotency(
        self,
        record: SessionRecord,
        event_kind: EventKind,
        payload: dict[str, Any],
        effective_state: SessionState,
        event_key: str,
    ) -> StoreMutation | None:
        stable_event_id = _stable_agent_event_id(record, event_key)
        existing_event = next(
            (
                event
                for event in record.events
                if event.event_id == stable_event_id
            ),
            None,
        )
        if existing_event is None:
            return None

        candidate = StructuredInteractionEnvelope(
            event_id=stable_event_id,
            sop_session_id=record.projection.sop_session_id,
            chat_id=record.projection.chat_id,
            revision=existing_event.revision,
            round=existing_event.round,
            state_version=existing_event.state_version,
            kind=event_kind,
            payload=payload,
        )
        existing_payload = _dict_payload(existing_event.payload)
        candidate_payload = _dict_payload(candidate.payload)
        if (
            existing_event.kind is not event_kind
            or existing_payload != candidate_payload
        ):
            raise WPlusCommandError(
                "event_key was already used for another W+ event",
            )
        if (
            effective_state is SessionState.GENERATING_QUESTIONS
            and event_kind is EventKind.QUESTION_BATCH
        ):
            historical_batch = QuestionBatchPayload.model_validate(
                existing_payload,
            )
            candidate_batch = QuestionBatchPayload.model_validate(
                candidate_payload,
            )
            current_stage_id = record.projection.current_stage_id
            if (
                historical_batch.stage_id != current_stage_id
                or candidate_batch.stage_id != current_stage_id
            ):
                raise WPlusCommandError(
                    f"question_batch stage_id={candidate_batch.stage_id} "
                    f"does not match current_stage_id="
                    f"{current_stage_id or 'missing'}",
                )
        return StoreMutation(record=record, duplicate=True)

    # ------------------------------------------------------------------
    # Agent event-type handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_stage_proposal_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = StageProposalPayload.model_validate(payload)
        if any(
            stage.status is not StageStatus.PENDING for stage in typed.stages
        ):
            raise WPlusCommandError(
                "stage_proposal stages must start as pending",
            )
        return _AgentEventResult(
            target=SessionState.AWAITING_QUEUE_CONFIRMATION,
            typed_payload=typed,
            changes={"stages": typed.stages},
        )

    @staticmethod
    def _handle_question_batch_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = QuestionBatchPayload.model_validate(payload)
        if typed.stage_id != record.projection.current_stage_id:
            raise WPlusCommandError(
                f"question_batch stage_id={typed.stage_id} does not match "
                f"current_stage_id="
                f"{record.projection.current_stage_id or 'missing'}",
            )
        return _AgentEventResult(
            target=SessionState.AWAITING_ANSWER,
            typed_payload=typed,
            changes={
                "current_question_batch": typed,
                "current_stage_id": typed.stage_id,
            },
        )

    @staticmethod
    def _handle_trial_plan_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = TrialPlanPayload.model_validate(payload)
        if trusted_run_id is not None and typed.run_id != trusted_run_id:
            raise WPlusCommandError(
                "trial_plan run_id does not match the trusted run",
            )
        return _AgentEventResult(
            target=SessionState.EXECUTING_TRIAL,
            typed_payload=typed,
            changes={"current_run_id": typed.run_id},
        )

    @staticmethod
    def _handle_trial_execution_progress_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        dummy = _passthrough_event_payload(
            record,
            event_kind,
            payload,
        )
        return _AgentEventResult(
            target=SessionState.EXECUTING_TRIAL,
            typed_payload=dummy,
        )

    @staticmethod
    def _handle_trial_execution_completed_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = TrialExecutionCompletedPayload.model_validate(payload)
        if trusted_run_id is not None and typed.run_id != trusted_run_id:
            raise WPlusCommandError(
                "trial result run_id does not match the trusted run",
            )
        return _AgentEventResult(
            target=SessionState.AWAITING_TRIAL_FEEDBACK,
            typed_payload=typed,
            changes={
                "trial_result_lists": typed.result_lists,
                "confirmed_facts": typed.confirmed_facts,
                "unknowns": typed.unknowns,
            },
        )

    @staticmethod
    def _handle_trial_execution_failed_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        failure = TrialExecutionFailedPayload.model_validate(payload)
        if trusted_run_id is not None and failure.run_id != trusted_run_id:
            raise WPlusCommandError(
                "trial failure run_id does not match the trusted run",
            )
        return _AgentEventResult(
            target=SessionState.RECOVERABLE_FAILURE,
            typed_payload=failure,
            changes={
                "last_error": RecoverableFailurePayload(
                    error_code=failure.error_code,
                    summary=failure.summary,
                    failed_operation="trial_execution",
                    failed_run_id=failure.run_id,
                ),
                "resume_state": effective_state,
            },
        )

    @staticmethod
    def _handle_sop_result_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = SopResultPayload.model_validate(payload)
        _validate_delivered_artifacts(
            workspace_dir=svc.workspace.workspace_dir,
            result=typed.result,
        )
        return _AgentEventResult(
            target=SessionState.FINALIZING_OUTPUTS,
            typed_payload=typed,
            changes={"final_result": typed.result},
        )

    @staticmethod
    def _handle_stage_report_generated_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = StageReportGeneratedPayload.model_validate(payload)
        projection = record.projection
        report = typed.report
        if report.stage_id != projection.current_stage_id:
            raise WPlusCommandError(
                "stage report must belong to the current stage",
            )
        if report.revision != projection.revision:
            raise WPlusCommandError(
                "stage report revision must match the session revision",
            )
        existing = [
            prior
            for prior in projection.stage_reports
            if (
                prior.stage_id == report.stage_id
                and prior.revision == report.revision
            )
        ]
        if any(prior.report_no == report.report_no for prior in existing):
            raise WPlusCommandError("stage report version already exists")
        max_report_no = max(
            (prior.report_no for prior in existing),
            default=0,
        )
        if report.report_no != max_report_no + 1:
            raise WPlusCommandError(
                "stage report version must increment by one",
            )
        reports = [
            prior.model_copy(update={"superseded_by": report.report_no})
            if (
                prior.stage_id == report.stage_id
                and prior.revision == report.revision
                and prior.superseded_by is None
            )
            else prior
            for prior in projection.stage_reports
        ]
        reports.append(report)
        return _AgentEventResult(
            target=SessionState.AWAITING_STAGE_CONFIRMATION,
            typed_payload=typed,
            changes={"stage_reports": reports},
        )

    @staticmethod
    def _handle_stage_report_generation_failed_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = StageReportGenerationFailedPayload.model_validate(payload)
        return _AgentEventResult(
            target=SessionState.RECOVERABLE_FAILURE,
            typed_payload=typed,
            changes={
                "last_error": RecoverableFailurePayload(
                    error_code=typed.error_code,
                    summary=typed.summary,
                    failed_operation="stage_report_generation",
                ),
                "resume_state": effective_state,
            },
        )

    @staticmethod
    def _handle_cumulative_refreshed_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = CumulativeRefreshedPayload.model_validate(payload)
        projection = record.projection
        preview = typed.preview
        if preview.stage_order != [
            snapshot.stage_id for snapshot in projection.confirmed_snapshots
        ]:
            raise WPlusCommandError(
                "cumulative preview does not match confirmed snapshots",
            )
        is_all_confirmed = (
            len(projection.confirmed_snapshots) == len(projection.stages)
        )
        return _AgentEventResult(
            target=(
                SessionState.FINALIZING_OUTPUTS
                if is_all_confirmed
                else SessionState.GENERATING_QUESTIONS
            ),
            typed_payload=typed,
            changes={"cumulative_preview": preview},
        )

    @staticmethod
    def _handle_memory_candidates_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        if record.projection.final_result is None:
            raise WPlusCommandError(
                "memory_candidates requires a persisted final SOP result",
            )
        typed = MemoryCandidatesPayload.model_validate(payload)
        if any(
            candidate.status is not MemoryCandidateStatus.PENDING
            or candidate.legacy_read_only
            or candidate.write_receipt is not None
            or candidate.failure_reason is not None
            or candidate.target_scope is not None
            or candidate.target_file is not None
            for candidate in typed.candidates
        ):
            raise WPlusCommandError(
                "Agent memory candidates must be pending, unwritten, and untargeted",
            )
        targeted = _target_memory_candidates(
            typed.candidates,
            record.projection.memory_user_scope,
        )
        return _AgentEventResult(
            target=SessionState.OUTPUT_REVIEW,
            typed_payload=MemoryCandidatesPayload(candidates=targeted),
            changes={"memory_candidates": targeted},
        )

    @staticmethod
    def _handle_memory_write_batch_result_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = MemoryWriteBatchResultPayload.model_validate(payload)
        candidates, target = _apply_memory_batch_results(
            record.projection,
            typed,
        )
        return _AgentEventResult(
            target=target,
            typed_payload=typed,
            changes={
                "memory_candidates": candidates,
                "active_memory_candidate_id": None,
                "active_memory_candidate_ids": [],
            },
        )

    @staticmethod
    def _handle_memory_write_single_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        candidates = [
            candidate.model_copy(deep=True)
            for candidate in record.projection.memory_candidates
        ]
        active_id = record.projection.active_memory_candidate_id
        match_index = _find_memory_candidate_index(candidates, active_id)
        candidate = candidates[match_index]
        if candidate.status is not MemoryCandidateStatus.WRITING:
            raise WPlusCommandError("Memory candidate is not being written")
        typed, target = _apply_single_memory_result(
            record,
            payload,
            candidate,
            candidates,
            match_index,
            event_kind is EventKind.MEMORY_WRITE_COMPLETED,
        )
        return _AgentEventResult(
            target=target,
            typed_payload=typed,
            changes={
                "memory_candidates": candidates,
                "active_memory_candidate_id": None,
                "active_memory_candidate_ids": [],
            },
        )

    @staticmethod
    def _handle_recoverable_failure_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        typed = RecoverableFailurePayload.model_validate(payload)
        if trusted_run_id is not None and typed.failed_run_id is None:
            typed = typed.model_copy(
                update={"failed_run_id": trusted_run_id},
            )
        return _AgentEventResult(
            target=SessionState.RECOVERABLE_FAILURE,
            typed_payload=typed,
            changes={
                "last_error": typed,
                "resume_state": effective_state,
            },
        )

    @staticmethod
    def _handle_passthrough_agent_event(
        svc: WPlusSopService,
        record: SessionRecord,
        payload: dict[str, Any],
        trusted_run_id: str | None,
        effective_state: SessionState,
        event_kind: EventKind,
    ) -> _AgentEventResult:
        return _AgentEventResult(
            target=effective_state,
            typed_payload=_passthrough_event_payload(
                record,
                EventKind.TRIAL_EXECUTION_STARTED,
                payload,
            ),
        )


# ---------------------------------------------------------------------------
# Module-level agent-event helpers
# ---------------------------------------------------------------------------


class _AgentEventResult:
    __slots__ = ("target", "typed_payload", "changes")

    def __init__(
        self,
        *,
        target: SessionState,
        typed_payload: Any,
        changes: dict[str, Any] | None = None,
    ) -> None:
        self.target = target
        self.typed_payload = typed_payload
        self.changes = changes or {}


def _effective_agent_event_state(
    projection: SessionProjection,
) -> SessionState:
    state = projection.state
    if state is SessionState.PENDING_EXIT:
        return projection.resume_state or state
    return state


def _stable_agent_event_id(
    record: SessionRecord,
    event_key: str,
) -> str:
    stable_id = uuid5(
        NAMESPACE_URL,
        f"{record.projection.sop_session_id}:{event_key}",
    ).hex
    return f"evt_{stable_id}"


def _dict_payload(payload_obj: Any) -> Any:
    if hasattr(payload_obj, "model_dump"):
        return payload_obj.model_dump(mode="json")
    return payload_obj


def _passthrough_event_payload(
    record: SessionRecord,
    kind: EventKind,
    payload: dict[str, Any],
) -> Any:
    event_model = StructuredInteractionEnvelope(
        event_id="evt_validation",
        sop_session_id=record.projection.sop_session_id,
        chat_id=record.projection.chat_id,
        revision=record.projection.revision,
        round=record.projection.round,
        state_version=record.projection.state_version + 1,
        kind=kind,
        payload=payload,
    )
    return event_model.payload


def _validate_agent_event_state(
    event_kind: EventKind,
    effective_state: SessionState,
    state: SessionState,
) -> None:
    allowed_states = _RUN_EVENT_STATES.get(event_kind)
    if effective_state is not None and allowed_states is not None:
        if effective_state in allowed_states:
            return
    allowed_event_kinds = (
        sorted(
            k.value
            for k, s in _RUN_EVENT_STATES.items()
            if effective_state in s
        )
        if effective_state is not None
        else []
    )
    detail = (
        "; allowed agent events: " + ", ".join(allowed_event_kinds)
        if allowed_event_kinds
        else ""
    )
    raise WPlusCommandError(
        f"{event_kind.value} is not allowed while "
        f"{state.value} is active{detail}",
    )


def _resolve_pending_exit_target(
    record: SessionRecord,
    event_kind: EventKind,
    result: _AgentEventResult,
    state: SessionState,
) -> None:
    if state is not SessionState.PENDING_EXIT:
        return
    if event_kind not in _PENDING_EXIT_BOUNDARIES:
        result.target = SessionState.PENDING_EXIT
        return
    if record.projection.pending_exit_action == "terminate":
        result.target = SessionState.TERMINATED
        result.changes.update(
            {
                "pending_exit_action": None,
                "termination_summary": TerminationSummaryPayload(
                    summary="用户请求彻底结束；后台运行已在安全事件边界停止。",
                ),
            },
        )
        return
    if result.target is SessionState.COMPLETED:
        result.changes.update(
            {"pending_exit_action": None, "resume_state": None},
        )
        return
    safe_resume_state = result.target
    result.target = SessionState.PAUSED
    result.changes.update(
        {
            "pending_exit_action": None,
            "resume_state": safe_resume_state,
        },
    )


def _find_memory_candidate_index(
    candidates: list[Any],
    active_id: str | None,
) -> int:
    match_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if candidate.candidate_id == active_id
        ),
        -1,
    )
    if match_index < 0:
        raise WPlusCommandError(
            "Memory write run has no server-bound candidate",
        )
    return match_index


def _apply_single_memory_result(
    record: SessionRecord,
    payload: dict[str, Any],
    candidate: Any,
    candidates: list[Any],
    match_index: int,
    is_completed: bool,
) -> tuple[Any, SessionState]:
    sop_session_id = record.projection.sop_session_id
    if is_completed:
        completed = MemoryWriteCompletedPayload.model_validate(payload)
        if (
            completed.candidate_id != candidate.candidate_id
            or completed.target_scope != candidate.target_scope
            or completed.target_file != candidate.target_file
        ):
            raise WPlusCommandError(
                "Memory write receipt does not match approved candidate",
            )
        candidates[match_index] = candidate.model_copy(
            update={
                "status": MemoryCandidateStatus.APPROVED,
                "failure_reason": None,
                "write_receipt": MemoryWriteReceipt(
                    memory_id=(
                        f"wplus-sop/{sop_session_id}/"
                        f"{candidate.candidate_id}"
                    ),
                    target_scope=completed.target_scope,
                    target_file=completed.target_file,
                    reused_existing=(completed.result == "duplicate"),
                    store_result=completed.result,
                ),
            },
        )
        unresolved = any(
            item.status
            in {MemoryCandidateStatus.PENDING, MemoryCandidateStatus.FAILED}
            for item in candidates
        )
        target = (
            SessionState.MEMORY_REVIEW
            if unresolved
            else SessionState.COMPLETED
        )
        return completed, target
    # EventKind.MEMORY_WRITE_FAILED
    failure = MemoryWriteFailedPayload.model_validate(payload)
    if failure.candidate_id != candidate.candidate_id:
        raise WPlusCommandError(
            "Memory write failure does not match approved candidate",
        )
    candidates[match_index] = candidate.model_copy(
        update={
            "status": MemoryCandidateStatus.FAILED,
            "failure_reason": failure.summary[:500],
            "write_receipt": None,
        },
    )
    return failure, SessionState.MEMORY_REVIEW


def _target_memory_candidates(
    candidates: list[Any],
    memory_user_scope: str | None,
) -> list[Any]:
    targeted: list[Any] = []
    for candidate in candidates:
        if (
            candidate.memory_type is None
            or not isinstance(candidate.value, dict)
            or not (candidate.evidence or "").strip()
        ):
            raise WPlusCommandError(
                "Memory candidates require type, object content, and evidence",
            )
        try:
            target_scope, target_file = resolve_memory_target(
                candidate.memory_type,
                user_scope=memory_user_scope,
            )
        except WPlusMemoryPolicyError as exc:
            raise WPlusCommandError(str(exc)) from exc
        targeted.append(
            candidate.model_copy(
                update={
                    "target_scope": target_scope,
                    "target_file": target_file,
                },
            ),
        )
    return targeted


_AGENT_EVENT_HANDLERS: dict[EventKind, Any] = {
    EventKind.STAGE_PROPOSAL: WPlusSopService._handle_stage_proposal_agent_event,
    EventKind.QUESTION_BATCH: WPlusSopService._handle_question_batch_agent_event,
    EventKind.TRIAL_PLAN: WPlusSopService._handle_trial_plan_agent_event,
    EventKind.TRIAL_EXECUTION_STARTED: WPlusSopService._handle_trial_execution_progress_agent_event,
    EventKind.TRIAL_EXECUTION_PROGRESS: WPlusSopService._handle_trial_execution_progress_agent_event,
    EventKind.TRIAL_EXECUTION_COMPLETED: WPlusSopService._handle_trial_execution_completed_agent_event,
    EventKind.TRIAL_EXECUTION_FAILED: WPlusSopService._handle_trial_execution_failed_agent_event,
    EventKind.SOP_RESULT: WPlusSopService._handle_sop_result_agent_event,
    EventKind.STAGE_REPORT_GENERATED: WPlusSopService._handle_stage_report_generated_agent_event,
    EventKind.STAGE_REPORT_GENERATION_FAILED: WPlusSopService._handle_stage_report_generation_failed_agent_event,
    EventKind.CUMULATIVE_REFRESHED: WPlusSopService._handle_cumulative_refreshed_agent_event,
    EventKind.MEMORY_CANDIDATES: WPlusSopService._handle_memory_candidates_agent_event,
    EventKind.MEMORY_WRITE_BATCH_RESULT: WPlusSopService._handle_memory_write_batch_result_agent_event,
    EventKind.MEMORY_WRITE_COMPLETED: WPlusSopService._handle_memory_write_single_agent_event,
    EventKind.MEMORY_WRITE_FAILED: WPlusSopService._handle_memory_write_single_agent_event,
    EventKind.RECOVERABLE_FAILURE: WPlusSopService._handle_recoverable_failure_agent_event,
}


# ---------------------------------------------------------------------------
# Module-level command dispatch helpers
# ---------------------------------------------------------------------------


def _adjust_command_event(
    event: StructuredInteractionEnvelope,
    command: str,
    projection: SessionProjection,
    raw_payload: dict[str, Any],
) -> StructuredInteractionEnvelope:
    """Apply command-specific round/revision fixups to the event envelope."""
    if command == "submit_answers":
        return event.model_copy(update={"round": projection.round + 1})
    if command == "revise_answer":
        return event.model_copy(
            update={
                "revision": projection.revision + 1,
                "round": int(raw_payload["revised_round"]),
            },
        )
    return event


def _build_revised_answers(
    svc: Any,
    record: SessionRecord,
    previous: AnswerBatch,
    raw_answers: dict[str, Any],
) -> list[QuestionAnswer]:
    """Reconstruct answer list for a revision from raw payload."""
    replacement: list[QuestionAnswer] = []
    original_questions = svc._questions_for_answer_batch(record, previous)
    for prior_answer in previous.answers:
        value = raw_answers.get(prior_answer.question_id)
        original_question = original_questions.get(prior_answer.question_id)
        if original_question is not None:
            replacement.append(
                svc._question_answer(original_question, value),
            )
        elif isinstance(value, dict):
            replacement.append(
                svc._structured_question_answer(
                    prior_answer.question_id,
                    value,
                ),
            )
        elif isinstance(value, list):
            replacement.append(
                QuestionAnswer(
                    question_id=prior_answer.question_id,
                    selected_option_ids=[str(item) for item in value],
                ),
            )
        elif prior_answer.text is not None:
            replacement.append(
                QuestionAnswer(
                    question_id=prior_answer.question_id,
                    text=str(value or ""),
                ),
            )
        else:
            replacement.append(
                QuestionAnswer(
                    question_id=prior_answer.question_id,
                    selected_option_ids=[str(value or "")],
                ),
            )
    return replacement


_COMMAND_HANDLERS: dict[str, Any] = {
    "confirm_stage_queue": WPlusSopService._dispatch_confirm_stage_queue,
    "submit_answers": WPlusSopService._dispatch_submit_answers,
    "submit_trial_feedback": WPlusSopService._dispatch_submit_trial_feedback,
    "accept_trial": WPlusSopService._dispatch_accept_trial,
    "confirm_stage": WPlusSopService._dispatch_confirm_stage,
    "revise_answer": WPlusSopService._dispatch_revise_answer,
    "save_and_exit": WPlusSopService._dispatch_save_and_exit,
    "terminate": WPlusSopService._dispatch_save_and_exit,
    "cancel_run_and_pause": WPlusSopService._dispatch_cancel_run_and_pause,
    "continue_waiting": WPlusSopService._dispatch_cancel_run_and_pause,
    "resume": WPlusSopService._dispatch_resume,
    "retry_current_turn": WPlusSopService._dispatch_retry_current_turn,
    "confirm_outputs": WPlusSopService._dispatch_confirm_outputs,
    "resolve_memory": WPlusSopService._dispatch_resolve_or_skip_memory,
    "skip_memory": WPlusSopService._dispatch_resolve_or_skip_memory,
}
