# -*- coding: utf-8 -*-
"""SQL-authoritative runtime archive façade over existing immutable NAS storage."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4
import json

from ...agents.memory.chat_checkpoint import (
    CheckpointEvent,
    CheckpointRecord,
    PrecompactionCandidate,
    validate_precompaction_candidate,
    validate_checkpoint_record,
)
from ...agents.memory.conversation_archive import (
    CheckpointArchiveState,
    ConversationArchiveBoundary,
)
from .archive import (
    ArchiveInstall,
    CheckpointInstall,
    ContextReset,
    EventInstall,
)
from .archive_binding import ArchiveBinding, attach_bound_memory
from .archive_reader import ArchiveReader, text
from .bundle import BundleBlock, new_operation_key
from .commit_plan import CommitPlan
from .errors import ConflictError, IntegrityError
from .message_installs import MessageInstall
from .publications import PublicationRequest, PublicationStore
from .retention import RetentionStore


def _block(name, kind, value):
    return BundleBlock(
        name,
        kind,
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode(),
    )


def _timestamp(value):
    if value is None:
        return None
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat()


def _compacted_watermark(state, messages):
    if len(state.events) < len(messages):
        return state.record.applied_event_sequence
    for offset, (message, event) in enumerate(zip(messages, state.events), 1):
        if (
            event.sequence != state.record.applied_event_sequence + offset
            or event.type != "message_added"
            or dict(event.facts)
            != {"message_id": message.id, "role": message.role}
            or event.source_refs != (f"message:{message.id}",)
        ):
            return state.record.applied_event_sequence
    return state.events[len(messages) - 1].sequence


@dataclass(frozen=True)
class ArchiveCommit:
    recovery_state: dict
    publication: dict
    boundary: ConversationArchiveBoundary | None = None
    record: CheckpointRecord | None = None
    state: CheckpointArchiveState | None = None


class MySQLArchiveStore:
    """One trusted workspace store; execution bindings remain request-owned."""

    def __init__(self, database, scope, bundles, session_adapter):
        self.db, self.scope, self.bundles = database, scope, bundles
        self.session_adapter = session_adapter
        self.publications = PublicationStore(
            database,
            bundles,
            service_unit_id=scope.service_unit_id,
        )
        self.reader = ArchiveReader(database, scope, bundles)
        self.retentions = RetentionStore(database, scope)

    def attach_memory(self, memory, chat_id):
        return attach_bound_memory(memory, self, chat_id)

    async def prepare_memory(self, memory, chat_id):
        chat = await self.reader.chat(chat_id)
        if chat["epoch"] < 1:
            raise IntegrityError(
                "indexed Chat checkpoint epoch must be positive",
            )
        self.attach_memory(memory, chat_id)
        memory._chat_checkpoint_epoch = chat["epoch"]
        return memory

    def bind_memory(
        self,
        memory,
        chat_id,
        session_execution,
        root_provider,
        authority,
    ):
        self.attach_memory(memory, chat_id)
        memory._conversation_archive_binding = ArchiveBinding(
            self,
            memory,
            chat_id,
            session_execution,
            root_provider,
            authority,
        )
        return memory

    async def read_checkpoint_state(self, chat_id):
        return await self.reader.checkpoint(chat_id)

    async def read_checkpoint_prefix(self, chat_id, limit):
        if type(limit) is not int or not 0 < limit <= 4096:
            raise ValueError("checkpoint prefix must be bounded")
        return await self.reader.checkpoint(chat_id, limit)

    async def read_checkpoint_head(self, chat_id):
        chat = await self.reader.chat(chat_id)
        return {
            "version": 3,
            "epoch": chat["epoch"],
            "last_event_sequence": chat["event_high"],
            "checkpoint_revision": chat["checkpoint_revision"],
        }

    async def read_checkpoint_record(self, chat_id, snapshot):
        state = await self.read_checkpoint_state(chat_id)
        if (
            state.current_epoch != snapshot["epoch"]
            or state.record.revision != snapshot["checkpoint_revision"]
        ):
            raise ConflictError("checkpoint snapshot changed")
        return state.record

    async def recover_evidence(self, *, chat_id, epoch, refs=(), **kwargs):
        return await self.reader.evidence(
            chat_id,
            epoch=epoch,
            refs=refs,
            **kwargs,
        )

    async def _context(self, chat_id, recovery_state, authority):
        if not isinstance(recovery_state, dict):
            raise ValueError(
                "archive publication requires complete recovery state",
            )
        chat = await self.reader.chat(chat_id)
        session = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_sessions WHERE id=%s AND service_unit_key=%s",
            (chat["session_id"], self.scope.sql_values[0]),
        )
        if (
            session is None
            or tuple(
                session[key]
                for key in (
                    "service_unit_key",
                    "tenant_key",
                    "source_key",
                    "agent_key",
                )
            )
            != self.scope.sql_values
        ):
            raise IntegrityError("archive Session scope association mismatch")
        if recovery_state.get("revision", 0) != session["state_revision"]:
            raise ConflictError("archive recovery root revision changed")
        if authority is not None and (
            authority.identity.scope != self.scope
            or authority.session_id != session["id"]
            or authority.chat_id != chat["id"]
        ):
            raise IntegrityError(
                "archive execution authority association mismatch",
            )
        if authority is None and session["active_turn_id"] is not None:
            raise ConflictError("archive Session is busy")
        return session, chat

    async def _publish(
        self,
        session,
        chat,
        state,
        authority,
        kind,
        plan,
        blocks,
    ):
        operation = new_operation_key()
        committed = {**state, "revision": session["state_revision"] + 1}
        request = PublicationRequest(
            operation,
            self.scope,
            kind,
            (
                authority.owner_key
                if authority
                else self.session_adapter.owner_key
            ),
            authority.generation if authority else session["generation"],
            session_id=session["id"],
            chat_id=chat["id"],
            turn_id=authority.turn_id if authority else None,
            base_state_revision=session["state_revision"],
            base_history_revision=chat["history_revision"],
            epoch=chat["epoch"],
        )
        await self.publications.reserve(request, plan)
        await self.publications.write_bundle(
            operation,
            [_block("recovery", "recovery", committed), *blocks],
        )
        publication = await self.publications.commit(operation)
        return ArchiveCommit(committed, publication)

    async def append_message(
        self,
        chat_id,
        message,
        *,
        recovery_state,
        authority,
    ):
        if not hasattr(message, "to_dict"):
            raise TypeError("indexed memory append requires Msg")
        session, chat = await self._context(chat_id, recovery_state, authority)
        event = CheckpointEvent.new(
            sequence=chat["event_high"] + 1,
            epoch=chat["epoch"],
            type="message_added",
            facts={"message_id": message.id, "role": message.role},
            source_refs=(f"message:{message.id}",),
        )
        plan = CommitPlan(
            recovery_block="recovery",
            messages=(
                MessageInstall(
                    "message",
                    "memory",
                    message.role,
                    source_msg_key=message.id,
                    turn_id=authority.turn_id if authority else None,
                    context_epoch=session["context_epoch"],
                    in_context=True,
                    message_timestamp=_timestamp(message.timestamp),
                ),
            ),
            events=EventInstall("events", event.sequence, event.sequence),
        )
        return await self._publish(
            session,
            chat,
            recovery_state,
            authority,
            "state",
            plan,
            [
                _block("message", "message", message.to_dict()),
                _block("events", "events", [event.to_dict()]),
            ],
        )

    async def _source_rows(self, session, messages):
        if len(messages) > 4096:
            raise ValueError(
                "compact source batch exceeds bounded record limit",
            )
        rows = await self.db.fetch_all(
            "SELECT * FROM swe_conversation_messages WHERE session_id=%s AND layer='memory' AND current_visible=1 AND deleted_at IS NULL ORDER BY session_order LIMIT %s",
            (session["id"], len(messages)),
        )
        if len(messages) > len(rows):
            raise ConflictError("compact source prefix is not indexed")
        selected = rows[: len(messages)]
        async with self.db.transaction():
            batch = await self.retentions.pin(
                [row["publication_id"] for row in selected],
                kind="reader",
            )
        try:
            publications = {row["id"]: row for row in batch.publications}
            for row, message in zip(selected, messages):
                publication = publications[row["publication_id"]]
                raw = await batch.read(
                    self.bundles.read_extent,
                    text(publication["relative_path"]),
                    row["byte_offset"],
                    row["byte_length"],
                    expected_length=publication["byte_length"],
                )
                from agentscope.message import Msg

                if (
                    Msg.from_dict(json.loads(raw)).to_dict()
                    != message.to_dict()
                ):
                    raise ConflictError(
                        "compact requires exact indexed source prefix",
                    )
            return selected
        finally:
            await self.retentions.finish_reader(batch)

    async def compact(
        self,
        chat_id,
        messages,
        *,
        recovery_state,
        authority,
        candidate_id=None,
    ):
        from ..runner.utils import agentscope_msg_to_message

        if not messages:
            raise ValueError("compact requires nonempty source occurrences")
        session, chat = await self._context(chat_id, recovery_state, authority)
        sources = await self._source_rows(session, messages)
        active = await self.read_checkpoint_state(chat_id)
        record = replace(
            active.record,
            revision=active.record.revision + 1,
            source_revision=active.record.revision,
            applied_event_sequence=_compacted_watermark(active, messages),
        )
        if candidate_id is not None:
            candidate = await self._candidate(chat_id, candidate_id)
            if (
                tuple(message.id for message in messages)
                != candidate.source_message_ids
                or not validate_precompaction_candidate(
                    candidate,
                    active.record,
                    chat["event_high"],
                ).is_valid
            ):
                raise ConflictError(
                    "checkpoint candidate prefix or revision changed",
                )
            record = candidate.record
        boundary_id = str(uuid4())
        record = replace(record, archived_through=boundary_id)
        event = CheckpointEvent.new(
            sequence=chat["event_high"] + 1,
            epoch=chat["epoch"],
            type="messages_archived",
            facts={"boundary_id": boundary_id},
            source_refs=tuple(f"message:{message.id}" for message in messages),
        )
        occurrences = tuple(
            MessageInstall(
                "source:" + str(row["id"]),
                "archive",
                text(row["role_key"]),
                source_msg_key=text(row["source_msg_key"]),
                source_occurrence_no=row["source_occurrence_no"],
                kind=text(row["kind_key"]),
                task_run_key=text(row["task_run_key"]),
                origin_chat_id=row["origin_chat_id"],
                archive_chat_id=chat["id"],
                turn_id=row["turn_id"],
                context_epoch=row["context_epoch"],
                message_timestamp=_timestamp(message.timestamp),
                source_message_id=row["id"],
            )
            for row, message in zip(sources, messages)
        )
        boundary = ArchiveInstall(
            boundary_id,
            "archive",
            len(messages),
            sum(
                len(agentscope_msg_to_message(message)) for message in messages
            ),
            messages[0].id,
            messages[-1].id,
            _timestamp(messages[0].timestamp),
            _timestamp(messages[-1].timestamp),
        )
        plan = CommitPlan(
            recovery_block="recovery",
            messages=occurrences,
            archive=boundary,
            checkpoint=CheckpointInstall(
                "checkpoint",
                active.record.revision,
                record.revision,
                record.applied_event_sequence,
                sources[-1]["session_order"],
            ),
            events=EventInstall("events", event.sequence, event.sequence),
            prune_ids=tuple(row["id"] for row in sources),
        )
        result = await self._publish(
            session,
            chat,
            recovery_state,
            authority,
            "compact",
            plan,
            [
                _block(
                    "archive",
                    "archive",
                    {
                        "version": 1,
                        "source_message_ids": [row["id"] for row in sources],
                    },
                ),
                _block("checkpoint", "checkpoint", record.to_dict()),
                _block("events", "events", [event.to_dict()]),
            ],
        )
        visible = ConversationArchiveBoundary(
            boundary_id,
            chat_id,
            result.publication["committed_at"]
            .replace(tzinfo=timezone.utc)
            .isoformat(),
            boundary.display_count,
            messages[0].id,
            messages[-1].id,
            messages[0].timestamp,
            messages[-1].timestamp,
        )
        return replace(result, boundary=visible, record=record)

    async def reset(self, chat_id, *, recovery_state, authority, reason):
        if not isinstance(reason, str) or not reason:
            raise ValueError("reset reason is required")
        session, chat = await self._context(chat_id, recovery_state, authority)
        result = await self._publish(
            session,
            chat,
            recovery_state,
            authority,
            "reset",
            CommitPlan(
                recovery_block="recovery",
                reset=ContextReset(session["context_epoch"], chat["epoch"]),
            ),
            [],
        )
        record = CheckpointRecord.new(chat_id=chat_id, epoch=chat["epoch"] + 1)
        return replace(
            result,
            state=CheckpointArchiveState(record, (), record.epoch),
        )

    async def write_active_checkpoint(
        self,
        chat_id,
        record,
        *,
        recovery_state,
        authority,
    ):
        session, chat = await self._context(chat_id, recovery_state, authority)
        if not validate_checkpoint_record(record).is_valid:
            raise ValueError("checkpoint record is invalid")
        plan = CommitPlan(
            recovery_block="recovery",
            checkpoint=CheckpointInstall(
                "checkpoint",
                chat["checkpoint_revision"],
                record.revision,
                record.applied_event_sequence,
                session["history_high"],
            ),
        )
        return await self._publish(
            session,
            chat,
            recovery_state,
            authority,
            "checkpoint",
            plan,
            [_block("checkpoint", "checkpoint", record.to_dict())],
        )

    async def write_pending_candidate(self, chat_id, candidate):
        chat = await self.reader.chat(chat_id)
        if (
            candidate.chat_id != chat_id
            or candidate.epoch != chat["epoch"]
            or not validate_checkpoint_record(candidate.record).is_valid
        ):
            raise ValueError("checkpoint candidate scope or epoch is invalid")
        operation = candidate.id.replace("-", "")
        key = operation + operation
        await PublicationStore._nas_io(
            key,
            self.bundles.publish,
            key,
            [_block("candidate", "candidate", candidate.to_dict())],
            metadata={"operation_key": key},
        )

    async def _candidate(self, chat_id, candidate_id):
        if str(UUID(candidate_id)) != candidate_id:
            raise ValueError("candidate ID must be canonical UUID")
        operation = str(candidate_id).replace("-", "")
        key = operation + operation
        index = await PublicationStore._nas_io(
            key,
            self.bundles.inspect,
            self.bundles.path_for(key),
        )
        extent = index.blocks.get("candidate")
        if extent is None:
            raise IntegrityError("pending checkpoint candidate is missing")
        raw = await PublicationStore._nas_io(
            key,
            self.bundles.read_extent,
            index.relative_path,
            extent.offset,
            extent.length,
            expected_length=index.byte_length,
        )
        candidate = PrecompactionCandidate.from_dict(json.loads(raw))
        if candidate.chat_id != chat_id:
            raise IntegrityError("pending checkpoint candidate Chat mismatch")
        return candidate

    async def read_candidate(self, chat_id, candidate_id):
        return await self._candidate(chat_id, candidate_id)
