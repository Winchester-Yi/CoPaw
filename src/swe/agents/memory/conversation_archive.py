# -*- coding: utf-8 -*-
"""Chat-scoped, durable archives for compacted conversation messages."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any, Collection, Sequence

import fcntl

from agentscope.message import Msg

from swe.constant import SECRET_DIR
from swe.app.runner.hidden_context_injection import (
    redact_hidden_context_for_display,
)

from .chat_checkpoint import (
    CheckpointEvent,
    CheckpointRecord,
    CompletedTask,
    EvidenceItem,
    PrecompactionCandidate,
    render_checkpoint_projection,
    validate_checkpoint_record,
    validate_precompaction_candidate,
)

logger = logging.getLogger(__name__)

_MANIFEST_NAME = "manifest.json"
_CHECKPOINT_NAME = "checkpoint.json"
_CHECKPOINT_EVENTS_NAME = "events.jsonl"
_CHECKPOINT_EPOCHS_NAME = "epochs.json"
_CHECKPOINT_EVIDENCE_EPOCHS_NAME = "evidence-epochs.json"
_CHECKPOINT_CANDIDATES_DIR = "candidates"
_MAX_PAGE_SIZE = 50
_CURSOR_SIGNATURE_SIZE = hashlib.sha256().digest_size
_CURSOR_SECRET_ENV_VAR = "SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET"
_CURSOR_SECRET_FILE_NAME = "conversation-archive-cursor-secret"


@cache
def _load_or_create_cursor_secret() -> bytes:
    """Return a private, process-independent archive cursor signing key."""
    configured_secret = os.environ.get(_CURSOR_SECRET_ENV_VAR)
    if configured_secret is not None:
        if not configured_secret:
            raise ValueError(
                "SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET must not be empty",
            )
        return configured_secret.encode("utf-8")

    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = SECRET_DIR / _CURSOR_SECRET_FILE_NAME
    try:
        return secret_path.read_bytes()
    except FileNotFoundError:
        pass

    secret = os.urandom(48)
    temporary_path = SECRET_DIR / (
        f".{_CURSOR_SECRET_FILE_NAME}.{os.getpid()}.{os.urandom(12).hex()}"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.write(descriptor, secret)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary_path, secret_path)
        except FileExistsError:
            pass
        else:
            ConversationArchiveStore._fsync_directory(SECRET_DIR)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
    return secret_path.read_bytes()


@dataclass(frozen=True)
class ConversationArchiveBoundary:
    """Metadata for one immutable archive batch."""

    id: str
    chat_id: str
    created_at: str
    archived_message_count: int
    first_message_id: str
    last_message_id: str
    first_timestamp: str | None
    last_timestamp: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON representation persisted in the manifest."""
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "created_at": self.created_at,
            "archived_message_count": self.archived_message_count,
            "first_message_id": self.first_message_id,
            "last_message_id": self.last_message_id,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConversationArchiveBoundary":
        """Parse one manifest entry."""
        if not isinstance(value, dict):
            raise ValueError("boundary must be a JSON object")
        return cls(
            id=cls._canonical_uuid(value["id"], "id"),
            chat_id=cls._canonical_uuid(value["chat_id"], "chat_id"),
            created_at=cls._timestamp(value["created_at"], "created_at"),
            archived_message_count=cls._positive_int(
                value["archived_message_count"],
                "archived_message_count",
            ),
            first_message_id=cls._non_empty_string(
                value["first_message_id"],
                "first_message_id",
            ),
            last_message_id=cls._non_empty_string(
                value["last_message_id"],
                "last_message_id",
            ),
            first_timestamp=cls._optional_string(value["first_timestamp"]),
            last_timestamp=cls._optional_string(value["last_timestamp"]),
        )

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None or isinstance(value, str):
            return value
        raise ValueError("timestamp must be a string or null")

    @staticmethod
    def _canonical_uuid(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a canonical UUID string")
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{field} must be a canonical UUID string",
            ) from exc
        if str(parsed) != value:
            raise ValueError(f"{field} must be a canonical UUID string")
        return value

    @staticmethod
    def _non_empty_string(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty string")
        return value

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return value

    @staticmethod
    def _timestamp(value: Any, field: str) -> str:
        value = ConversationArchiveBoundary._non_empty_string(value, field)
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
        return value


@dataclass(frozen=True)
class ConversationArchivePage:
    """One chronological page of archived messages and their boundaries."""

    messages: list[Msg]
    boundaries: list[ConversationArchiveBoundary]
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class CheckpointArchiveState:
    """Current checkpoint and the recoverable event delta for one Chat."""

    record: CheckpointRecord
    events: tuple[CheckpointEvent, ...]
    current_epoch: int


@dataclass(frozen=True)
class CheckpointCommitResult:
    """The visible archive boundary and checkpoint installed together."""

    boundary: ConversationArchiveBoundary
    record: CheckpointRecord


@dataclass(frozen=True)
class _EvidenceRecoveryQuery:
    """Normalized selectors used while scanning one archive epoch."""

    requested: frozenset[str]
    semantic_query: str
    kind_filter: frozenset[str]
    time_bounds: tuple[datetime, datetime] | None


class ConversationArchiveStore:
    """Persist and page compacted messages without cross-chat visibility."""

    def __init__(
        self,
        dialog_root: str | Path,
        *,
        cursor_secret: bytes | None = None,
    ) -> None:
        self._dialog_root = Path(dialog_root)
        self._cursor_secret = cursor_secret or _load_or_create_cursor_secret()
        if not self._cursor_secret:
            raise ValueError(
                "Conversation archive cursor secret must not be empty",
            )

    async def commit(
        self,
        chat_id: str,
        messages: Sequence[Msg],
    ) -> ConversationArchiveBoundary:
        """Write one immutable batch, then make it visible through manifest."""
        return await asyncio.to_thread(self._commit, chat_id, messages)

    def _commit(
        self,
        chat_id: str,
        messages: Sequence[Msg],
    ) -> ConversationArchiveBoundary:
        canonical_chat_id = self._validate_chat_id(chat_id)
        if not messages:
            raise ValueError("Cannot archive an empty message batch")

        archived_messages = list(messages)
        if not all(isinstance(message, Msg) for message in archived_messages):
            raise TypeError(
                "Conversation archive messages must be Msg instances",
            )

        chat_dir = self.path_for(canonical_chat_id)
        with self._chat_lock(canonical_chat_id):
            return self._commit_locked(canonical_chat_id, archived_messages)

    def _commit_locked(
        self,
        canonical_chat_id: str,
        messages: Sequence[Msg],
        *,
        checkpoint: CheckpointRecord | None = None,
    ) -> ConversationArchiveBoundary:
        """Archive under an already-held Chat lock.

        If a checkpoint is supplied, write it after the durable batch and before
        the manifest makes that batch visible to archive readers.
        """
        if self._tombstone_path(canonical_chat_id).exists():
            raise ValueError("Conversation archive has been deleted")
        chat_dir = self.path_for(canonical_chat_id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        boundary = ConversationArchiveBoundary(
            id=str(uuid.uuid4()),
            chat_id=canonical_chat_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            archived_message_count=len(messages),
            first_message_id=messages[0].id,
            last_message_id=messages[-1].id,
            first_timestamp=messages[0].timestamp,
            last_timestamp=messages[-1].timestamp,
        )
        self._write_batch(chat_dir / f"{boundary.id}.jsonl", messages)
        self._write_evidence_epochs(
            chat_dir,
            boundary.id,
            messages,
            self._read_current_epoch(chat_dir),
        )
        if checkpoint is not None:
            self._write_checkpoint_locked(
                canonical_chat_id,
                replace(checkpoint, archived_through=boundary.id),
            )
        manifest_path = chat_dir / _MANIFEST_NAME
        manifest = self._read_manifest(manifest_path)
        manifest["boundaries"].append(boundary.to_dict())
        self._replace_manifest(manifest_path, manifest)
        return boundary

    async def advance_archived_message_events(
        self,
        chat_id: str,
        messages: Sequence[Msg],
        boundary_id: str,
    ) -> CheckpointRecord:
        """Advance the event cursor for a durably archived legacy prefix."""
        return await asyncio.to_thread(
            self._advance_archived_message_events,
            chat_id,
            messages,
            boundary_id,
        )

    def _advance_archived_message_events(
        self,
        chat_id: str,
        messages: Sequence[Msg],
        boundary_id: str,
    ) -> CheckpointRecord:
        canonical_chat_id = self._validate_chat_id(chat_id)
        canonical_boundary_id = ConversationArchiveBoundary._canonical_uuid(
            boundary_id,
            "boundary_id",
        )
        with self._chat_lock(canonical_chat_id):
            state = self._read_checkpoint_state_locked(canonical_chat_id)
            if not messages or len(state.events) < len(messages):
                return state.record
            for offset, message in enumerate(messages, start=1):
                event = state.events[offset - 1]
                if (
                    event.sequence
                    != state.record.applied_event_sequence + offset
                    or event.type != "message_added"
                    or dict(event.facts)
                    != {"message_id": message.id, "role": message.role}
                    or event.source_refs != (f"message:{message.id}",)
                ):
                    return state.record
            record = replace(
                state.record,
                applied_event_sequence=state.events[
                    len(messages) - 1
                ].sequence,
                archived_through=canonical_boundary_id,
            )
            self._write_checkpoint_locked(canonical_chat_id, record)
            return record

    async def read_checkpoint_state(
        self,
        chat_id: str,
    ) -> CheckpointArchiveState:
        """Read the active record and only events not yet incorporated by it."""
        return await asyncio.to_thread(self._read_checkpoint_state, chat_id)

    def _read_checkpoint_state(self, chat_id: str) -> CheckpointArchiveState:
        canonical_chat_id = self._validate_chat_id(chat_id)
        with self._chat_lock(canonical_chat_id):
            return self._read_checkpoint_state_locked(canonical_chat_id)

    def _read_checkpoint_state_locked(
        self,
        canonical_chat_id: str,
    ) -> CheckpointArchiveState:
        chat_dir = self.path_for(canonical_chat_id)
        current_epoch = self._read_current_epoch(chat_dir)
        record = self._read_checkpoint_locked(
            canonical_chat_id,
            current_epoch,
        )
        events = tuple(
            event
            for event in self._read_checkpoint_events(chat_dir)
            if event.epoch == current_epoch
            and event.sequence > record.applied_event_sequence
        )
        return CheckpointArchiveState(record, events, current_epoch)

    async def append_checkpoint_event(
        self,
        chat_id: str,
        event: CheckpointEvent,
    ) -> None:
        """Durably append the next deterministic event under the Chat lock."""
        await asyncio.to_thread(self._append_checkpoint_event, chat_id, event)

    def _append_checkpoint_event(
        self,
        chat_id: str,
        event: CheckpointEvent,
    ) -> None:
        canonical_chat_id = self._validate_chat_id(chat_id)
        with self._chat_lock(canonical_chat_id):
            if self._tombstone_path(canonical_chat_id).exists():
                raise ValueError("Conversation archive has been deleted")
            state = self._read_checkpoint_state_locked(canonical_chat_id)
            if event.epoch != state.current_epoch:
                raise ValueError("Checkpoint event epoch is not current")
            last_sequence = max(
                (item.sequence for item in state.events),
                default=state.record.applied_event_sequence,
            )
            if event.sequence != last_sequence + 1:
                raise ValueError(
                    "Checkpoint event sequence must be contiguous",
                )
            validation = validate_checkpoint_record(state.record, (event,))
            if not validation.is_valid:
                raise ValueError(
                    "Invalid checkpoint event: "
                    + "; ".join(validation.errors),
                )
            chat_dir = self.path_for(canonical_chat_id)
            events = [*self._read_checkpoint_events(chat_dir), event]
            self._write_checkpoint_events(chat_dir, events)

    async def write_active_checkpoint(
        self,
        chat_id: str,
        record: CheckpointRecord,
    ) -> None:
        """Write a validated active record without archiving messages."""
        await asyncio.to_thread(self._write_active_checkpoint, chat_id, record)

    def _write_active_checkpoint(
        self,
        chat_id: str,
        record: CheckpointRecord,
    ) -> None:
        canonical_chat_id = self._validate_chat_id(chat_id)
        with self._chat_lock(canonical_chat_id):
            self._write_checkpoint_locked(canonical_chat_id, record)

    async def write_pending_candidate(
        self,
        chat_id: str,
        candidate: PrecompactionCandidate,
    ) -> None:
        """Persist an inactive candidate for later validation and installation."""
        await asyncio.to_thread(
            self._write_pending_candidate,
            chat_id,
            candidate,
        )

    def _write_pending_candidate(
        self,
        chat_id: str,
        candidate: PrecompactionCandidate,
    ) -> None:
        canonical_chat_id = self._validate_chat_id(chat_id)
        with self._chat_lock(canonical_chat_id):
            state = self._read_checkpoint_state_locked(canonical_chat_id)
            if candidate.chat_id != canonical_chat_id:
                raise ValueError("Candidate chat_id does not match archive")
            if candidate.epoch != state.current_epoch:
                raise ValueError("Candidate epoch is not current")
            validation = validate_checkpoint_record(candidate.record)
            if not validation.is_valid:
                raise ValueError(
                    "Invalid checkpoint candidate: "
                    + "; ".join(validation.errors),
                )
            path = self._candidate_path(canonical_chat_id, candidate.id)
            self._atomic_write(
                path,
                json.dumps(
                    candidate.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )

    async def install_ready_candidate(
        self,
        chat_id: str,
    ) -> CheckpointRecord | None:
        """Install the newest still-valid pending candidate, if any."""
        return await asyncio.to_thread(self._install_ready_candidate, chat_id)

    def _install_ready_candidate(
        self,
        chat_id: str,
    ) -> CheckpointRecord | None:
        canonical_chat_id = self._validate_chat_id(chat_id)
        with self._chat_lock(canonical_chat_id):
            state = self._read_checkpoint_state_locked(canonical_chat_id)
            candidates = self._read_candidates(canonical_chat_id)
            current_sequence = max(
                (event.sequence for event in state.events),
                default=state.record.applied_event_sequence,
            )
            for candidate in sorted(
                candidates,
                key=lambda item: item.created_at,
                reverse=True,
            ):
                validation = validate_precompaction_candidate(
                    candidate,
                    state.record,
                    current_sequence,
                )
                if not validation.is_valid:
                    continue
                self._write_checkpoint_locked(
                    canonical_chat_id,
                    candidate.record,
                )
                self._candidate_path(canonical_chat_id, candidate.id).unlink(
                    missing_ok=True,
                )
                return candidate.record
            return None

    async def commit_checkpoint(
        self,
        chat_id: str,
        messages: Sequence[Msg],
        candidate_id: str,
    ) -> CheckpointCommitResult:
        """Archive source messages and activate one validated candidate."""
        return await asyncio.to_thread(
            self._commit_checkpoint,
            chat_id,
            messages,
            candidate_id,
        )

    async def commit_ready_checkpoint(
        self,
        chat_id: str,
        messages: Sequence[Msg],
    ) -> CheckpointCommitResult | None:
        """Atomically archive messages with the newest still-valid candidate."""
        return await asyncio.to_thread(
            self._commit_ready_checkpoint,
            chat_id,
            messages,
        )

    def _commit_ready_checkpoint(
        self,
        chat_id: str,
        messages: Sequence[Msg],
    ) -> CheckpointCommitResult | None:
        canonical_chat_id = self._validate_chat_id(chat_id)
        with self._chat_lock(canonical_chat_id):
            state = self._read_checkpoint_state_locked(canonical_chat_id)
            current_sequence = max(
                (event.sequence for event in state.events),
                default=state.record.applied_event_sequence,
            )
            for candidate in sorted(
                self._read_candidates(canonical_chat_id),
                key=lambda item: item.created_at,
                reverse=True,
            ):
                if tuple(message.id for message in messages) != (
                    candidate.source_message_ids
                ):
                    continue
                if validate_precompaction_candidate(
                    candidate,
                    state.record,
                    current_sequence,
                ).is_valid:
                    candidate_id = candidate.id
                    break
            else:
                return None
        return self._commit_checkpoint(
            canonical_chat_id,
            messages,
            candidate_id,
        )

    def _commit_checkpoint(
        self,
        chat_id: str,
        messages: Sequence[Msg],
        candidate_id: str,
    ) -> CheckpointCommitResult:
        canonical_chat_id = self._validate_chat_id(chat_id)
        archived_messages = list(messages)
        if not archived_messages or not all(
            isinstance(message, Msg) for message in archived_messages
        ):
            raise ValueError(
                "Checkpoint commit requires non-empty Msg messages",
            )
        with self._chat_lock(canonical_chat_id):
            state = self._read_checkpoint_state_locked(canonical_chat_id)
            candidate = self._read_candidate(canonical_chat_id, candidate_id)
            if candidate is None:
                raise ValueError("Checkpoint candidate does not exist")
            if tuple(message.id for message in archived_messages) != (
                candidate.source_message_ids
            ):
                raise ValueError(
                    "Checkpoint source message prefix does not match candidate",
                )
            current_sequence = max(
                (event.sequence for event in state.events),
                default=state.record.applied_event_sequence,
            )
            validation = validate_precompaction_candidate(
                candidate,
                state.record,
                current_sequence,
            )
            if not validation.is_valid:
                raise ValueError(
                    "Checkpoint candidate is not ready: "
                    + "; ".join(validation.errors),
                )
            record = replace(candidate.record, archived_through=None)
            chat_dir = self.path_for(canonical_chat_id)
            checkpoint_before = self._snapshot_file(
                self._checkpoint_path(canonical_chat_id),
            )
            evidence_epochs_before = self._snapshot_file(
                self._evidence_epochs_path(chat_dir),
            )
            try:
                boundary = self._commit_locked(
                    canonical_chat_id,
                    archived_messages,
                    checkpoint=record,
                )
            except Exception:
                # The batch is intentionally still invisible without a manifest
                # entry. Restore the two derived files so the pending candidate
                # remains valid and the same request can be retried safely.
                self._restore_file(
                    self._checkpoint_path(canonical_chat_id),
                    checkpoint_before,
                )
                self._restore_file(
                    self._evidence_epochs_path(chat_dir),
                    evidence_epochs_before,
                )
                raise
            record = replace(record, archived_through=boundary.id)
            self._candidate_path(canonical_chat_id, candidate.id).unlink(
                missing_ok=True,
            )
            return CheckpointCommitResult(boundary, record)

    async def reset_checkpoint_epoch(
        self,
        chat_id: str,
        *,
        reason: str,
    ) -> CheckpointArchiveState:
        """Start a new default-context epoch without deleting Chat evidence."""
        return await asyncio.to_thread(
            self._reset_checkpoint_epoch,
            chat_id,
            reason,
        )

    def _reset_checkpoint_epoch(
        self,
        chat_id: str,
        reason: str,
    ) -> CheckpointArchiveState:
        canonical_chat_id = self._validate_chat_id(chat_id)
        with self._chat_lock(canonical_chat_id):
            chat_dir = self.path_for(canonical_chat_id)
            prior_epoch = self._read_current_epoch(chat_dir)
            prior_record = self._read_checkpoint_locked(
                canonical_chat_id,
                prior_epoch,
            )
            next_epoch = prior_epoch + 1
            epochs_before = self._snapshot_file(
                self._epochs_path(canonical_chat_id),
            )
            self._write_epochs(chat_dir, next_epoch, reason)
            record = CheckpointRecord.new(
                chat_id=canonical_chat_id,
                epoch=next_epoch,
            )
            if reason == "new" and prior_record.current_task.title:
                task_refs = tuple(
                    sorted(
                        {
                            ref
                            for item in (
                                *prior_record.current_task.goal,
                                *prior_record.current_task.acceptance_criteria,
                            )
                            for ref in item.evidence_refs
                        },
                    ),
                )
                record = replace(
                    record,
                    completed_task_index=(
                        *prior_record.completed_task_index,
                        CompletedTask(
                            id=prior_record.current_task.id
                            or prior_record.checkpoint_id,
                            title=prior_record.current_task.title,
                            completed_at=datetime.now(
                                timezone.utc,
                            ).isoformat(),
                            evidence_refs=task_refs,
                        ),
                    ),
                )
            try:
                self._write_checkpoint_locked(canonical_chat_id, record)
            except Exception:
                # A new epoch without its matching record makes the Chat
                # unreadable. Restore the previous epoch metadata before
                # releasing the per-Chat lock.
                self._restore_file(
                    self._epochs_path(canonical_chat_id),
                    epochs_before,
                )
                raise
            candidates_dir = chat_dir / _CHECKPOINT_CANDIDATES_DIR
            if candidates_dir.exists():
                shutil.rmtree(candidates_dir)
                self._fsync_directory(chat_dir)
            return CheckpointArchiveState(record, (), next_epoch)

    async def recover_evidence(
        self,
        chat_id: str,
        *,
        epoch: int,
        refs: Sequence[str],
        query: str | None = None,
        kinds: Sequence[str] | None = None,
        time_range: str | None = None,
        limit: int = 10,
    ) -> list[Msg]:
        """Recover bounded evidence only for the current Context Epoch.

        Exact references are authoritative. Semantic lookup is limited to
        message text, role/name, and an ISO-8601 interval.
        """
        return await asyncio.to_thread(
            self._recover_evidence,
            chat_id,
            epoch,
            refs,
            query,
            kinds,
            time_range,
            limit,
        )

    def _recover_evidence(
        self,
        chat_id: str,
        epoch: int,
        refs: Sequence[str],
        query: str | None,
        kinds: Sequence[str] | None,
        time_range: str | None,
        limit: int,
    ) -> list[Msg]:
        canonical_chat_id = self._validate_chat_id(chat_id)
        if limit <= 0:
            return []
        limit = min(limit, 10)
        recovery_query = self._prepare_evidence_query(
            refs,
            query,
            kinds,
            time_range,
        )
        with self._chat_lock(canonical_chat_id):
            state = self._read_checkpoint_state_locked(canonical_chat_id)
            if epoch != state.current_epoch:
                return []
            if (
                not recovery_query.requested
                and not recovery_query.semantic_query
            ):
                return []
            chat_dir = self.path_for(canonical_chat_id)
            manifest = self._read_manifest(chat_dir / _MANIFEST_NAME)
            evidence_epochs_path = self._evidence_epochs_path(chat_dir)
            evidence_epochs = self._read_evidence_epochs(chat_dir)
            is_legacy_epoch_one = (
                not evidence_epochs_path.exists() and state.current_epoch == 1
            )
            return self._collect_recovered_evidence(
                chat_dir,
                manifest,
                canonical_chat_id,
                epoch,
                evidence_epochs,
                is_legacy_epoch_one,
                recovery_query,
                limit,
            )

    def _prepare_evidence_query(
        self,
        refs: Sequence[str],
        query: str | None,
        kinds: Sequence[str] | None,
        time_range: str | None,
    ) -> _EvidenceRecoveryQuery:
        """Normalize selectors without changing their precedence rules."""
        return _EvidenceRecoveryQuery(
            requested=frozenset(str(ref) for ref in refs if str(ref)),
            semantic_query=query.strip().casefold() if query else "",
            kind_filter=frozenset(
                str(kind).strip().casefold()
                for kind in (kinds or ())
                if str(kind).strip()
            ),
            time_bounds=self._parse_evidence_time_range(time_range),
        )

    def _collect_recovered_evidence(
        self,
        chat_dir: Path,
        manifest: dict[str, Any],
        chat_id: str,
        epoch: int,
        evidence_epochs: dict[str, int],
        is_legacy_epoch_one: bool,
        recovery_query: _EvidenceRecoveryQuery,
        limit: int,
    ) -> list[Msg]:
        """Scan visible batches and return matching bounded evidence."""
        recovered: list[Msg] = []
        for boundary in self._visible_boundaries(manifest, chat_id):
            messages = self._read_batch(chat_dir / f"{boundary.id}.jsonl")
            for message_index, message in enumerate(messages):
                if message is None:
                    continue
                evidence_epoch = self._message_epoch_for_recovery(
                    boundary.id,
                    message_index,
                    message,
                    evidence_epochs,
                    is_legacy_epoch_one,
                )
                if evidence_epoch != epoch:
                    continue
                if not self._matches_evidence_query(
                    message,
                    requested=recovery_query.requested,
                    semantic_query=recovery_query.semantic_query,
                    kind_filter=recovery_query.kind_filter,
                    time_bounds=recovery_query.time_bounds,
                ):
                    continue
                recovered.append(message)
                if len(recovered) >= limit:
                    return recovered
        return recovered

    def _message_epoch_for_recovery(
        self,
        boundary_id: str,
        message_index: int,
        message: Msg,
        evidence_epochs: dict[str, int],
        is_legacy_epoch_one: bool,
    ) -> int | None:
        """Resolve persisted epoch metadata with the legacy epoch-one fallback."""
        evidence_epoch = evidence_epochs.get(
            self._evidence_epoch_key(boundary_id, message_index, message.id),
        )
        if evidence_epoch is None and is_legacy_epoch_one:
            return 1
        return evidence_epoch

    def _matches_evidence_query(
        self,
        message: Msg,
        *,
        requested: Collection[str],
        semantic_query: str,
        kind_filter: Collection[str],
        time_bounds: tuple[datetime, datetime] | None,
    ) -> bool:
        """Match exact references or the bounded semantic evidence selectors."""
        if requested:
            return any(
                message.id in (ref, ref.partition(":")[2]) for ref in requested
            )
        if semantic_query:
            content = message.get_text_content() or ""
            if semantic_query not in content.casefold():
                return False
        if kind_filter and not (
            str(message.role).casefold() in kind_filter
            or str(message.name).casefold() in kind_filter
        ):
            return False
        if time_bounds and not self._message_in_time_range(
            message,
            time_bounds,
        ):
            return False
        return True

    @staticmethod
    def _parse_evidence_time_range(
        value: str | None,
    ) -> tuple[datetime, datetime] | None:
        if not value:
            return None
        if len(value) > 128:
            raise ValueError("Evidence time range is too long")
        start_text, separator, end_text = value.partition("/")
        if not separator:
            raise ValueError("Evidence time range must be start/end")
        try:
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)
        except ValueError as exc:
            raise ValueError(
                "Evidence time range must use ISO timestamps",
            ) from exc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end < start:
            raise ValueError("Evidence time range end precedes start")
        return start, end

    @staticmethod
    def _message_in_time_range(
        message: Msg,
        bounds: tuple[datetime, datetime],
    ) -> bool:
        timestamp = getattr(message, "timestamp", None)
        if not timestamp:
            return False
        try:
            parsed = datetime.fromisoformat(str(timestamp))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        start, end = bounds
        return start <= parsed <= end

    async def read_page(
        self,
        chat_id: str,
        before: str | None = None,
        limit: int = _MAX_PAGE_SIZE,
    ) -> ConversationArchivePage:
        """Read archived messages newest-first, returned in timeline order."""
        return await asyncio.to_thread(self._read_page, chat_id, before, limit)

    def _read_page(
        self,
        chat_id: str,
        before: str | None = None,
        limit: int = _MAX_PAGE_SIZE,
    ) -> ConversationArchivePage:
        canonical_chat_id = self._validate_chat_id(chat_id)
        page_size = self._normalize_page_size(limit)
        chat_dir = self.path_for(canonical_chat_id)
        with self._chat_lock(canonical_chat_id):
            if self._tombstone_path(canonical_chat_id).exists():
                return ConversationArchivePage([], [], False, None)
            manifest = self._read_manifest(chat_dir / _MANIFEST_NAME)
            boundaries = self._visible_boundaries(manifest, canonical_chat_id)
            cursor = (
                self._decode_cursor(
                    before,
                    canonical_chat_id,
                    boundaries,
                    chat_dir,
                )
                if before
                else None
            )
            selected, has_previous = self._select_page(
                chat_dir,
                boundaries,
                cursor,
                page_size,
            )

            messages = [item[2] for item in reversed(selected)]
            page_boundaries = [
                item[0]
                for item in reversed(selected)
                if item[2].id == item[0].last_message_id
            ]
            has_more = has_previous
            next_cursor = None
            if has_more and selected:
                oldest = selected[-1]
                next_cursor = self._encode_cursor(
                    canonical_chat_id,
                    oldest[0].id,
                    oldest[1],
                )
            return ConversationArchivePage(
                messages=messages,
                boundaries=page_boundaries,
                has_more=has_more,
                next_cursor=next_cursor,
            )

    async def delete_chat(self, chat_id: str) -> None:
        """Delete only the validated chat's archive directory."""
        await asyncio.to_thread(self._delete_chat, chat_id)

    def _delete_chat(self, chat_id: str) -> None:
        canonical_chat_id = self._validate_chat_id(chat_id)
        chat_dir = self.path_for(canonical_chat_id)
        with self._chat_lock(canonical_chat_id):
            self._atomic_write(self._tombstone_path(canonical_chat_id), "")
            if chat_dir.exists():
                shutil.rmtree(chat_dir)
                self._fsync_directory(chat_dir.parent)

    def _chat_dir(self, canonical_chat_id: str) -> Path:
        return self._dialog_root / canonical_chat_id

    def _tombstone_path(self, canonical_chat_id: str) -> Path:
        return self._dialog_root / ".deleted" / canonical_chat_id

    def _checkpoint_path(self, canonical_chat_id: str) -> Path:
        return self.path_for(canonical_chat_id) / _CHECKPOINT_NAME

    def _events_path(self, canonical_chat_id: str) -> Path:
        return self.path_for(canonical_chat_id) / _CHECKPOINT_EVENTS_NAME

    def _epochs_path(self, canonical_chat_id: str) -> Path:
        return self.path_for(canonical_chat_id) / _CHECKPOINT_EPOCHS_NAME

    @staticmethod
    def _evidence_epochs_path(chat_dir: Path) -> Path:
        return chat_dir / _CHECKPOINT_EVIDENCE_EPOCHS_NAME

    def _candidate_path(
        self,
        canonical_chat_id: str,
        candidate_id: str,
    ) -> Path:
        canonical_candidate_id = ConversationArchiveBoundary._canonical_uuid(
            candidate_id,
            "candidate_id",
        )
        return (
            self.path_for(canonical_chat_id)
            / _CHECKPOINT_CANDIDATES_DIR
            / f"{canonical_candidate_id}.json"
        )

    @staticmethod
    def _read_current_epoch(chat_dir: Path) -> int:
        path = chat_dir / _CHECKPOINT_EPOCHS_NAME
        if not path.exists():
            return 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            epoch = value["current_epoch"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Checkpoint epoch state is invalid") from exc
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ValueError("Checkpoint epoch state is invalid")
        return epoch

    def _write_epochs(self, chat_dir: Path, epoch: int, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("Checkpoint epoch reset reason must not be empty")
        history: list[dict[str, Any]] = []
        path = chat_dir / _CHECKPOINT_EPOCHS_NAME
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                existing = value.get("history", [])
                if isinstance(existing, list):
                    history = existing
            except (TypeError, ValueError, json.JSONDecodeError):
                history = []
        history.append(
            {
                "epoch": epoch,
                "reason": reason,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._atomic_write(
            path,
            json.dumps(
                {"current_epoch": epoch, "history": history},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def _read_evidence_epochs(self, chat_dir: Path) -> dict[str, int]:
        path = self._evidence_epochs_path(chat_dir)
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Checkpoint evidence epochs are invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("Checkpoint evidence epochs are invalid")
        result: dict[str, int] = {}
        for message_id, epoch in value.items():
            if (
                not isinstance(message_id, str)
                or not isinstance(epoch, int)
                or isinstance(epoch, bool)
                or epoch < 1
            ):
                raise ValueError("Checkpoint evidence epochs are invalid")
            result[message_id] = epoch
        return result

    def _write_evidence_epochs(
        self,
        chat_dir: Path,
        boundary_id: str,
        messages: Sequence[Msg],
        epoch: int,
    ) -> None:
        evidence_epochs = self._read_evidence_epochs(chat_dir)
        evidence_epochs.update(
            {
                self._evidence_epoch_key(boundary_id, index, message.id): epoch
                for index, message in enumerate(messages)
            },
        )
        self._atomic_write(
            self._evidence_epochs_path(chat_dir),
            json.dumps(
                evidence_epochs,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _evidence_epoch_key(
        boundary_id: str,
        message_index: int,
        message_id: str,
    ) -> str:
        """Bind evidence ownership to immutable archive location, not Msg.id."""
        return f"{boundary_id}:{message_index}:{message_id}"

    @staticmethod
    def _snapshot_file(path: Path) -> str | None:
        return path.read_text(encoding="utf-8") if path.exists() else None

    def _restore_file(self, path: Path, payload: str | None) -> None:
        if payload is not None:
            self._atomic_write(path, payload)
            return
        if path.exists():
            path.unlink()
            self._fsync_directory(path.parent)

    def _read_checkpoint_locked(
        self,
        canonical_chat_id: str,
        current_epoch: int,
    ) -> CheckpointRecord:
        path = self._checkpoint_path(canonical_chat_id)
        if not path.exists():
            return CheckpointRecord.new(
                chat_id=canonical_chat_id,
                epoch=current_epoch,
            )
        try:
            record = CheckpointRecord.from_dict(
                json.loads(path.read_text(encoding="utf-8")),
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("Checkpoint record is invalid") from exc
        validation = validate_checkpoint_record(record)
        if not validation.is_valid:
            raise ValueError("Checkpoint record is invalid")
        if (
            record.chat_id != canonical_chat_id
            or record.epoch != current_epoch
        ):
            raise ValueError(
                "Checkpoint record does not match current Chat epoch",
            )
        return record

    def _write_checkpoint_locked(
        self,
        canonical_chat_id: str,
        record: CheckpointRecord,
    ) -> None:
        if record.chat_id != canonical_chat_id:
            raise ValueError("Checkpoint chat_id does not match archive")
        chat_dir = self.path_for(canonical_chat_id)
        current_epoch = self._read_current_epoch(chat_dir)
        if record.epoch != current_epoch:
            raise ValueError("Checkpoint epoch is not current")
        validation = validate_checkpoint_record(record)
        if not validation.is_valid:
            raise ValueError(
                "Invalid checkpoint record: " + "; ".join(validation.errors),
            )
        self._atomic_write(
            self._checkpoint_path(canonical_chat_id),
            record.to_json(),
        )

    @staticmethod
    def _read_checkpoint_events(chat_dir: Path) -> list[CheckpointEvent]:
        path = chat_dir / _CHECKPOINT_EVENTS_NAME
        if not path.exists():
            return []
        events: list[CheckpointEvent] = []
        with path.open("r", encoding="utf-8") as file_handle:
            for line_number, line in enumerate(file_handle, start=1):
                try:
                    events.append(CheckpointEvent.from_dict(json.loads(line)))
                except (
                    TypeError,
                    ValueError,
                    KeyError,
                    json.JSONDecodeError,
                ) as exc:
                    logger.warning(
                        "Skipping malformed checkpoint event %s:%d: %s",
                        path,
                        line_number,
                        exc,
                    )
        return events

    def _write_checkpoint_events(
        self,
        chat_dir: Path,
        events: Sequence[CheckpointEvent],
    ) -> None:
        payload = "".join(
            json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for event in events
        )
        self._atomic_write(chat_dir / _CHECKPOINT_EVENTS_NAME, payload)

    def _read_candidate(
        self,
        canonical_chat_id: str,
        candidate_id: str,
    ) -> PrecompactionCandidate | None:
        path = self._candidate_path(canonical_chat_id, candidate_id)
        if not path.exists():
            return None
        try:
            return PrecompactionCandidate.from_dict(
                json.loads(path.read_text(encoding="utf-8")),
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.warning(
                "Skipping malformed checkpoint candidate %s: %s",
                path,
                exc,
            )
            return None

    def _read_candidates(
        self,
        canonical_chat_id: str,
    ) -> list[PrecompactionCandidate]:
        candidates_dir = (
            self.path_for(canonical_chat_id) / _CHECKPOINT_CANDIDATES_DIR
        )
        if not candidates_dir.exists():
            return []
        candidates: list[PrecompactionCandidate] = []
        for path in candidates_dir.glob("*.json"):
            candidate = self._read_candidate(canonical_chat_id, path.stem)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def path_for(self, chat_id: str) -> Path:
        """Return the validated archive directory for one chat record."""
        return self._chat_dir(self._validate_chat_id(chat_id))

    @staticmethod
    def _validate_chat_id(chat_id: str) -> str:
        if not isinstance(chat_id, str):
            raise ValueError("chat_id must be a canonical UUID string")
        try:
            parsed = uuid.UUID(chat_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                "chat_id must be a canonical UUID string",
            ) from exc
        if str(parsed) != chat_id:
            raise ValueError("chat_id must be a canonical UUID string")
        return chat_id

    @staticmethod
    def _normalize_page_size(limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        return min(limit, _MAX_PAGE_SIZE)

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, list[dict[str, Any]]]:
        if not path.exists():
            return {"boundaries": []}
        with path.open("r", encoding="utf-8") as file_handle:
            value = json.load(file_handle)
        boundaries = (
            value.get("boundaries") if isinstance(value, dict) else None
        )
        if not isinstance(boundaries, list):
            raise ValueError(
                "Conversation archive manifest has invalid boundaries",
            )
        return {"boundaries": boundaries}

    @staticmethod
    def _visible_boundaries(
        manifest: dict[str, list[dict[str, Any]]],
        chat_id: str,
    ) -> list[ConversationArchiveBoundary]:
        boundaries: list[ConversationArchiveBoundary] = []
        for value in manifest["boundaries"]:
            try:
                boundary = ConversationArchiveBoundary.from_dict(value)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping malformed conversation archive boundary: %s",
                    exc,
                )
                continue
            if boundary.chat_id != chat_id:
                logger.warning(
                    "Skipping conversation archive boundary for another chat: %s",
                    boundary.id,
                )
                continue
            boundaries.append(boundary)
        return boundaries

    def _select_page(
        self,
        chat_dir: Path,
        boundaries: list[ConversationArchiveBoundary],
        cursor: tuple[str, int] | None,
        limit: int,
    ) -> tuple[list[tuple[ConversationArchiveBoundary, int, Msg]], bool]:
        selected: list[tuple[ConversationArchiveBoundary, int, Msg]] = []
        before_reached = cursor is None
        for boundary in reversed(boundaries):
            records = self._read_batch(chat_dir / f"{boundary.id}.jsonl")
            if not self._batch_matches_boundary(boundary, records):
                logger.warning(
                    "Skipping inconsistent conversation archive batch: %s",
                    boundary.id,
                )
                continue
            upper_index = len(records) - 1
            if cursor is not None and boundary.id == cursor[0]:
                before_reached = True
                upper_index = min(upper_index, cursor[1] - 1)
            elif cursor is not None and not before_reached:
                continue

            for index in range(upper_index, -1, -1):
                message = records[index]
                if message is None:
                    continue
                selected.append((boundary, index, message))
                if len(selected) == limit + 1:
                    return selected[:limit], True
        return selected, False

    @staticmethod
    def _batch_matches_boundary(
        boundary: ConversationArchiveBoundary,
        records: list[Msg | None],
    ) -> bool:
        if len(records) < boundary.archived_message_count:
            return False
        if not records or records[0] is None or records[-1] is None:
            return False
        first_message = records[0]
        last_message = records[-1]
        return bool(
            first_message.id == boundary.first_message_id
            and last_message.id == boundary.last_message_id
            and first_message.timestamp == boundary.first_timestamp
            and last_message.timestamp == boundary.last_timestamp,
        )

    def _write_batch(self, path: Path, messages: Sequence[Msg]) -> None:
        payload = "".join(
            json.dumps(message.to_dict(), ensure_ascii=False) + "\n"
            for message in messages
        )
        self._atomic_write(path, payload)

    def _replace_manifest(
        self,
        path: Path,
        manifest: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._atomic_write(
            path,
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        )

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as file_handle:
                temporary_path = Path(file_handle.name)
                file_handle.write(payload)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temporary_path, path)
            ConversationArchiveStore._fsync_directory(path.parent)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _chat_lock(self, canonical_chat_id: str):
        lock_path = self._dialog_root / ".locks" / f"{canonical_chat_id}.lock"
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_batch(path: Path) -> list[Msg | None]:
        if not path.is_file():
            logger.warning("Conversation archive batch is missing: %s", path)
            return []
        records: list[Msg | None] = []
        with path.open("r", encoding="utf-8") as file_handle:
            for line_number, line in enumerate(file_handle, start=1):
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("record is not a JSON object")
                    records.append(
                        redact_hidden_context_for_display(
                            Msg.from_dict(value),
                        ),
                    )
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    logger.warning(
                        "Skipping malformed conversation archive record %s:%d: %s",
                        path,
                        line_number,
                        exc,
                    )
                    records.append(None)
        return records

    def _encode_cursor(
        self,
        chat_id: str,
        boundary_id: str,
        message_index: int,
    ) -> str:
        raw = json.dumps(
            {
                "chat_id": chat_id,
                "boundary_id": boundary_id,
                "message_index": message_index,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(
            self._cursor_secret,
            raw,
            hashlib.sha256,
        ).digest()
        return (
            base64.urlsafe_b64encode(raw + signature)
            .decode("ascii")
            .rstrip("=")
        )

    def _decode_cursor(
        self,
        cursor: str,
        chat_id: str,
        boundaries: list[ConversationArchiveBoundary],
        chat_dir: Path,
    ) -> tuple[str, int]:
        if not isinstance(cursor, str):
            raise ValueError("Invalid conversation archive cursor")
        try:
            padding = "=" * (-len(cursor) % 4)
            signed_value = base64.urlsafe_b64decode(cursor + padding)
            raw, signature = (
                signed_value[:-_CURSOR_SIGNATURE_SIZE],
                signed_value[-_CURSOR_SIGNATURE_SIZE:],
            )
            expected_signature = hmac.new(
                self._cursor_secret,
                raw,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError("invalid signature")
            value = json.loads(raw.decode("utf-8"))
            cursor_chat_id = value["chat_id"]
            boundary_id = value["boundary_id"]
            message_index = value["message_index"]
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            base64.binascii.Error,
        ) as exc:
            raise ValueError("Invalid conversation archive cursor") from exc
        if (
            cursor_chat_id != chat_id
            or not isinstance(message_index, int)
            or isinstance(message_index, bool)
            or message_index < 0
        ):
            raise ValueError("Invalid conversation archive cursor")
        try:
            boundary_id = ConversationArchiveBoundary._canonical_uuid(
                boundary_id,
                "boundary_id",
            )
        except ValueError as exc:
            raise ValueError("Invalid conversation archive cursor") from exc
        boundary = next(
            (item for item in boundaries if item.id == boundary_id),
            None,
        )
        if boundary is None:
            raise ValueError("Invalid conversation archive cursor")
        records = self._read_batch(
            chat_dir / f"{boundary.id}.jsonl",
        )
        if message_index >= len(records) or records[message_index] is None:
            raise ValueError("Invalid conversation archive cursor")
        return boundary_id, message_index


# pylint: disable=too-many-statements
def attach_conversation_archive(
    memory: Any,
    dialog_root: str | Path,
    chat_id: str,
) -> Any:
    """Make a ReMe in-memory object archive compactions by chat record ID."""
    archive_store = ConversationArchiveStore(dialog_root)
    canonical_chat_id = archive_store._validate_chat_id(chat_id)
    if getattr(memory, "_chat_checkpoint_chat_id", None) == canonical_chat_id:
        return memory
    if getattr(memory, "_chat_checkpoint_chat_id", None):
        # A test double or custom ReMe backend may reuse one raw object for
        # multiple Chats. Remove the previous instance wrappers before binding
        # the new archive, otherwise B.add would append an event to Chat A.
        for name in (
            "add",
            "clear_content",
            "clear_compressed_summary",
        ):
            original = getattr(memory, f"_checkpoint_original_{name}", None)
            if original is not None:
                setattr(memory, name, original)
    original_add = getattr(memory, "add", None)
    original_get_memory = getattr(memory, "get_memory", None)
    original_clear_content = getattr(memory, "clear_content", None)
    original_clear_summary = getattr(memory, "clear_compressed_summary", None)
    append_lock = asyncio.Lock()

    def archived_messages_are_online_prefix(messages: Sequence[Msg]) -> bool:
        """Return whether selected objects are the current memory prefix."""
        if len(messages) > len(memory.content):
            return False
        if all(
            online is archived
            for (online, _marks), archived in zip(memory.content, messages)
        ):
            return True
        online_ids = [message.id for message, _marks in memory.content]
        return len(online_ids) == len(set(online_ids)) and all(
            online.id == archived.id
            for (online, _marks), archived in zip(memory.content, messages)
        )

    def remove_archived_online_messages(messages: Sequence[Msg]) -> None:
        """Remove only the concrete online occurrences just archived."""
        entries = list(memory.content)
        selected_indexes: set[int] = set()
        unresolved: list[Msg] = []
        for archived in messages:
            for index, (online, _marks) in enumerate(entries):
                if index not in selected_indexes and online is archived:
                    selected_indexes.add(index)
                    break
            else:
                unresolved.append(archived)
        for archived in unresolved:
            candidates = [
                index
                for index, (online, _marks) in enumerate(entries)
                if index not in selected_indexes and online.id == archived.id
            ]
            if len(candidates) == 1:
                selected_indexes.add(candidates[0])
        memory.content = [
            entry
            for index, entry in enumerate(entries)
            if index not in selected_indexes
        ]

    async def add(*args: Any, **kwargs: Any) -> Any:
        """Append an event only after ReMe has accepted the message."""
        if original_add is None:
            raise TypeError("Attached memory must expose add")
        result = original_add(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        message = args[0] if args else kwargs.get("message")
        if not isinstance(message, Msg):
            return result
        async with append_lock:
            state = await archive_store.read_checkpoint_state(
                canonical_chat_id,
            )
            sequence = (
                max(
                    (event.sequence for event in state.events),
                    default=state.record.applied_event_sequence,
                )
                + 1
            )
            await archive_store.append_checkpoint_event(
                canonical_chat_id,
                CheckpointEvent.new(
                    sequence=sequence,
                    epoch=state.current_epoch,
                    type="message_added",
                    facts={
                        "message_id": message.id,
                        "role": message.role,
                    },
                    source_refs=(f"message:{message.id}",),
                ),
            )
        return result

    async def archive_compacted_messages(
        messages: Sequence[Msg],
    ) -> ConversationArchiveBoundary:
        advances_event_cursor = archived_messages_are_online_prefix(messages)
        boundary = await archive_store.commit(canonical_chat_id, messages)
        if advances_event_cursor:
            await archive_store.advance_archived_message_events(
                canonical_chat_id,
                messages,
                boundary.id,
            )
        remove_archived_online_messages(messages)
        return boundary

    async def archive_checkpoint_messages(
        messages: Sequence[Msg],
        candidate_id: str,
    ) -> CheckpointCommitResult:
        """Atomically archive source turns and activate one checkpoint."""
        result = await archive_store.commit_checkpoint(
            canonical_chat_id,
            messages,
            candidate_id,
        )
        remove_archived_online_messages(messages)
        await install_checkpoint_projection(result.record)
        return result

    async def install_checkpoint_projection(
        record: CheckpointRecord | None = None,
    ) -> CheckpointRecord:
        """Expose a bounded Markdown projection, never checkpoint JSON."""
        if record is not None:
            await archive_store.write_active_checkpoint(
                canonical_chat_id,
                record,
            )
        state = await archive_store.read_checkpoint_state(canonical_chat_id)
        memory._compressed_summary = render_checkpoint_projection(
            state.record,
            state.events,
        )
        return state.record

    async def install_ready_precompaction() -> bool:
        record = await archive_store.install_ready_candidate(canonical_chat_id)
        if record is None:
            return False
        await install_checkpoint_projection()
        return True

    async def commit_ready_precompaction(
        messages: Sequence[Msg],
    ) -> bool:
        result = await archive_store.commit_ready_checkpoint(
            canonical_chat_id,
            messages,
        )
        if result is None:
            return False
        remove_archived_online_messages(messages)
        await install_checkpoint_projection()
        return True

    async def install_degraded_checkpoint(
        messages: Sequence[Msg],
    ) -> CheckpointRecord:
        """Persist a deterministic, reference-only emergency checkpoint."""
        state = await archive_store.read_checkpoint_state(canonical_chat_id)
        refs = tuple(f"message:{message.id}" for message in messages)
        record = replace(
            state.record,
            revision=state.record.revision + 1,
            source_revision=state.record.revision,
            confidence="degraded",
            critical_context=(
                (
                    *state.record.critical_context,
                    EvidenceItem(
                        text="Emergency compaction fallback retained message references",
                        evidence_refs=refs,
                    ),
                )
                if refs
                else state.record.critical_context
            ),
        )
        await archive_store.write_active_checkpoint(canonical_chat_id, record)
        return await install_checkpoint_projection()

    async def recover_evidence(
        *,
        epoch: int,
        refs: Sequence[str],
        **kwargs: Any,
    ) -> list[Msg]:
        return await archive_store.recover_evidence(
            canonical_chat_id,
            epoch=epoch,
            refs=refs,
            **kwargs,
        )

    async def reset_context_epoch(
        *,
        reason: str,
    ) -> CheckpointArchiveState:
        state = await archive_store.reset_checkpoint_epoch(
            canonical_chat_id,
            reason=reason,
        )
        memory._chat_checkpoint_epoch = state.current_epoch
        memory._compressed_summary = render_checkpoint_projection(
            state.record,
            state.events,
        )
        return state

    def clear_content() -> None:
        if original_clear_content is not None:
            original_clear_content()
        else:
            memory.content.clear()

    def clear_compressed_summary() -> None:
        if original_clear_summary is not None:
            original_clear_summary()
        else:
            memory._compressed_summary = ""

    memory.conversation_archive_store = archive_store
    memory.chat_checkpoint_store = archive_store
    memory._chat_checkpoint_chat_id = canonical_chat_id
    memory._chat_checkpoint_epoch = archive_store._read_checkpoint_state(
        canonical_chat_id,
    ).current_epoch
    memory._checkpoint_original_add = original_add
    memory._checkpoint_original_get_memory = original_get_memory
    memory._checkpoint_original_clear_content = original_clear_content
    memory._checkpoint_original_clear_compressed_summary = (
        original_clear_summary
    )
    if original_add is not None:
        memory.add = add
    memory.archive_compacted_messages = archive_compacted_messages
    memory.archive_checkpoint_messages = archive_checkpoint_messages
    memory.install_checkpoint_projection = install_checkpoint_projection
    memory.install_ready_precompaction = install_ready_precompaction
    memory.commit_ready_precompaction = commit_ready_precompaction
    memory.install_degraded_checkpoint = install_degraded_checkpoint
    memory.recover_evidence = recover_evidence
    memory.reset_context_epoch = reset_context_epoch
    memory.clear_content = clear_content
    memory.clear_compressed_summary = clear_compressed_summary
    return memory


# pylint: enable=too-many-statements


__all__ = [
    "ConversationArchiveBoundary",
    "ConversationArchivePage",
    "ConversationArchiveStore",
    "attach_conversation_archive",
]
