# -*- coding: utf-8 -*-
"""NAS-first publication with same-operation commit and explicit reconciliation.

Lock sets are Session -> Chat -> Turn -> publication. No model, tool or NAS
operation runs while SQL locks are held. Transport failures leave the original
operation unknown; they never imply rollback and never unlink final bundles.
"""

import asyncio
from dataclasses import dataclass
import json

from .bundle import BundleBlock, BundleStore
from .errors import ConflictError, IntegrityError, StorageError, UnknownCommit
from .identity import WorkspaceScope, identity_bytes
from .chat_mutations import (
    ChatMetadataInstall,
    install_chat_metadata,
    load_metadata_json,
)
from .retention import RetentionStore

TABLE = "swe_conversation_publications"
FORMAT_KEY = b"swe-bundle-v1"
MAX_PLAN_BYTES = 1024 * 1024


def _text(value):
    return (
        bytes(value).decode("utf-8")
        if isinstance(value, (bytes, bytearray))
        else value
    )


@dataclass(frozen=True)
class PublicationRequest:
    operation_key: str
    scope: WorkspaceScope
    kind: str
    owner_key: str
    generation: int
    session_id: int | None = None
    chat_id: int | None = None
    turn_id: int | None = None
    base_state_revision: int | None = None
    base_history_revision: int | None = None
    epoch: int | None = None
    scope_kind: str = "workspace"

    def __post_init__(self):
        BundleStore.path_for(self.operation_key)
        if not isinstance(self.scope, WorkspaceScope):
            raise TypeError("publication scope must be a WorkspaceScope")
        identity_bytes(self.owner_key, 128)
        identity_bytes(self.kind, 32)
        if not self.owner_key or not self.kind:
            raise ValueError("publication owner and kind are required")
        self._validate_scope()
        self._validate_numbers()
        self._validate_associations()

    def _validate_scope(self):
        if self.scope_kind not in ("workspace", "legacy_share"):
            raise ValueError("invalid publication scope kind")
        legacy_identity = (self.scope.source_id, self.scope.agent_id)
        associations = (self.session_id, self.chat_id, self.turn_id)
        if self.scope_kind == "legacy_share" and (
            any(legacy_identity)
            or any(value is not None for value in associations)
        ):
            raise ValueError(
                "legacy share cannot create workspace associations",
            )

    def _validate_numbers(self):
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError(
                "publication generation must be a nonnegative integer",
            )
        for field in (
            "generation",
            "base_state_revision",
            "base_history_revision",
            "epoch",
        ):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(
                    "publication revision must be a nonnegative integer",
                )
        for field in ("session_id", "chat_id", "turn_id"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(
                    "publication association must be a positive integer",
                )

    def _validate_associations(self):
        if self.chat_id is not None and self.session_id is None:
            raise ValueError("Chat publication requires its Session")
        if self.turn_id is not None and self.chat_id is None:
            raise ValueError("Turn publication requires its Chat")

    def fields(self):
        tenant, source, agent = self.scope.sql_values
        return {
            "operation_key": self.operation_key.encode("ascii"),
            "scope_kind": self.scope_kind,
            "tenant_key": tenant,
            "source_key": source,
            "agent_key": agent,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "generation": self.generation,
            "owner_key": self.owner_key.encode("utf-8"),
            "base_state_revision": self.base_state_revision,
            "base_history_revision": self.base_history_revision,
            "epoch": self.epoch,
            "relative_path": BundleStore.path_for(self.operation_key).encode(
                "ascii",
            ),
            "format_key": FORMAT_KEY,
        }


@dataclass(frozen=True)
class CommitPlan:
    """Body-free installation plan persisted before any NAS write."""

    recovery_block: str | None = None
    result_ids: tuple[int, ...] = ()
    chat_metadata: ChatMetadataInstall | None = None

    def serialize(self):
        if self.recovery_block is not None:
            identity_bytes(self.recovery_block, 256)
            if not self.recovery_block:
                raise ValueError("recovery block must be nonempty")
        if len(self.result_ids) > 4096 or any(
            type(value) is not int or value <= 0 for value in self.result_ids
        ):
            raise ValueError(
                "publication result IDs must be bounded positive integers",
            )
        value = {
            "version": 1,
            "recovery_block": self.recovery_block,
            "result_ids": self.result_ids,
        }
        if self.chat_metadata is not None:
            if self.recovery_block is not None:
                raise ValueError(
                    "Chat metadata cannot replace a recovery root",
                )
            value["chat_metadata"] = self.chat_metadata.to_dict()
        return json.dumps(value, separators=(",", ":"))

    @classmethod
    def parse(cls, text):
        try:
            if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
                raise ValueError("oversized commit plan")
            value = load_metadata_json(text)
            expected = {"version", "recovery_block", "result_ids"}
            if (
                set(value) not in (expected, expected | {"chat_metadata"})
                or value.pop("version") != 1
            ):
                raise ValueError("unknown commit plan version")
            value["result_ids"] = tuple(value["result_ids"])
            if "chat_metadata" in value:
                value["chat_metadata"] = ChatMetadataInstall.from_dict(
                    value["chat_metadata"],
                )
            plan = cls(**value)
            plan.serialize()
            return plan
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            raise IntegrityError(
                "publication commit plan is invalid",
            ) from error


def _request(row):
    return PublicationRequest(
        operation_key=_text(row["operation_key"]),
        scope=WorkspaceScope(
            *(
                _text(row[field])
                for field in ("tenant_key", "source_key", "agent_key")
            ),
        ),
        kind=row["kind"],
        owner_key=_text(row["owner_key"]),
        generation=row["generation"],
        session_id=row["session_id"],
        chat_id=row["chat_id"],
        turn_id=row["turn_id"],
        base_state_revision=row["base_state_revision"],
        base_history_revision=row["base_history_revision"],
        epoch=row["epoch"],
        scope_kind=row["scope_kind"],
    )


class PublicationStore:
    """Short SQL state transitions; immutable IO is outside transactions."""

    def __init__(self, database, bundles: BundleStore):
        self.db = database
        self.bundles = bundles

    @staticmethod
    async def _nas_io(operation_key, function, *args, **kwargs):
        """Never report a stopped caller while its NAS thread can still write."""
        worker = asyncio.create_task(
            asyncio.to_thread(function, *args, **kwargs),
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Consume a worker exception before exposing the uncertain operation.
            if not worker.cancelled():
                worker.exception()
            raise UnknownCommit(operation_key) from error

    async def get(self, operation_key, *, lock=False):
        BundleStore.path_for(operation_key)
        return await self.db.fetch_one(
            f"SELECT * FROM {TABLE} WHERE operation_key=%s"
            + (" FOR UPDATE" if lock else ""),
            (operation_key.encode("ascii"),),
        )

    async def _insert(self, request, plan_text, state):
        fields = {
            **request.fields(),
            "state": state,
            "result_metadata_text": plan_text,
            "boundary_key": None,
            "boundary_order": None,
            "checkpoint_revision": None,
            "event_first": None,
            "event_last": None,
        }
        columns = ",".join(fields)
        placeholders = ",".join(["%s"] * len(fields))
        await self.db.insert(
            f"INSERT INTO {TABLE} ({columns},created_at) VALUES ({placeholders},UTC_TIMESTAMP(6)) "
            "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",
            tuple(fields.values()),
        )

    @staticmethod
    def _matches(row, request, plan_text=None):
        expected = request.fields()
        if (
            row is not None
            and row["state"] == "committed"
            and request.kind == "chat_metadata"
            and request.chat_id is None
        ):
            expected.pop("chat_id")
        if row is None or any(
            row[key] != value for key, value in expected.items()
        ):
            raise ConflictError(
                "operation identity or publication association changed",
            )
        if plan_text is not None and row["result_metadata_text"] != plan_text:
            raise ConflictError("operation commit plan changed")

    async def reserve(self, request: PublicationRequest, plan: CommitPlan):
        plan_text = plan.serialize()
        try:
            async with self.db.transaction():
                await self._insert(request, plan_text, "prepared")
                row = await self.get(request.operation_key, lock=True)
                self._matches(row, request)
                if row["state"] == "aborted":
                    raise ConflictError("publication operation is aborted")
                self._matches(row, request, plan_text)
            return row
        except (StorageError, ValueError):
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(request.operation_key) from error

    async def write_bundle(
        self,
        operation_key: str,
        blocks: list[BundleBlock],
    ):
        try:
            row = await self.get(operation_key)
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error
        if row is None or row["state"] != "prepared":
            raise ConflictError(
                "publication must be prepared before writing NAS",
            )
        index = await self._nas_io(
            operation_key,
            self.bundles.publish,
            operation_key,
            blocks,
            metadata={"operation_key": operation_key},
        )
        await self._mark_durable(operation_key, index)
        return index

    async def confirm_bundle(self, operation_key: str):
        """Confirm the original file, never regenerate model/tool output."""
        row = await self.get(operation_key)
        if row is None or row["state"] != "prepared":
            raise ConflictError(
                "publication must be prepared before confirmation",
            )
        index = await self._nas_io(
            operation_key,
            self.bundles.confirm_durable,
            _text(row["relative_path"]),
        )
        if index.metadata.get("operation_key") != operation_key:
            raise IntegrityError("bundle operation identity mismatch")
        await self._mark_durable(operation_key, index)
        return index

    async def reconcile_metadata(
        self,
        operation_key: str,
        *,
        owner_stopped: bool,
    ):
        """Resolve only the original structural mutation after its writer stops."""
        if not owner_stopped:
            raise ConflictError("original owner must be confirmed stopped")
        row = await self.get(operation_key)
        if row is None:
            raise UnknownCommit(operation_key)
        if row["kind"] != "chat_metadata":
            raise ConflictError(
                "metadata reconciliation requires a metadata operation",
            )
        if row["state"] == "committed":
            return row
        if row["state"] != "prepared":
            raise ConflictError("publication operation is aborted")
        await self.confirm_bundle(operation_key)
        try:
            return await self.commit(operation_key)
        except ConflictError:
            await self.abort(_request(row), owner_stopped=True)
            raise

    async def _mark_durable(self, operation_key, index):
        try:
            async with self.db.transaction():
                current = await self.get(operation_key, lock=True)
                if current is None or current["state"] != "prepared":
                    raise ConflictError("publication ended during NAS write")
                await self.db.execute(
                    f"UPDATE {TABLE} SET byte_length=%s,durable_at=UTC_TIMESTAMP(6) WHERE id=%s",
                    (index.byte_length, current["id"]),
                )
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error

    async def _lock_associations(self, request, *, session_rows=None):
        session = chat = turn = None
        if request.session_id is not None:
            if session_rows is None:
                session = await self.db.fetch_one(
                    "SELECT *,lease_until>UTC_TIMESTAMP(6) AS lease_valid "
                    "FROM swe_conversation_sessions WHERE id=%s FOR UPDATE",
                    (request.session_id,),
                )
            else:
                session = session_rows.get(request.session_id)
        if request.chat_id is not None:
            chat = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_chats WHERE id=%s FOR UPDATE",
                (request.chat_id,),
            )
        if request.turn_id is not None:
            turn = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_turns WHERE id=%s FOR UPDATE",
                (request.turn_id,),
            )
        return session, chat, turn

    async def _lock_metadata_associations(self, request, plan):
        if (
            request.kind != "chat_metadata"
            or request.session_id is None
            or request.turn_id is not None
        ):
            raise IntegrityError(
                "invalid Chat metadata publication association",
            )
        if request.chat_id is None:
            await RetentionStore(self.db, request.scope).lock_guard()
        ids = sorted(
            {
                value
                for value in (request.session_id, plan.target_session_id)
                if value is not None
            },
        )
        session_rows = {}
        for identity in ids:
            session_rows[identity] = await self.db.fetch_one(
                "SELECT *,lease_until>UTC_TIMESTAMP(6) AS lease_valid "
                "FROM swe_conversation_sessions WHERE id=%s FOR UPDATE",
                (identity,),
            )
        target = (
            session_rows.get(plan.target_session_id)
            if plan.target_session_id is not None
            else None
        )
        if plan.target_session_id is not None:
            if (
                target is None
                or tuple(
                    target[key]
                    for key in ("tenant_key", "source_key", "agent_key")
                )
                != request.scope.sql_values
            ):
                raise IntegrityError(
                    "Chat metadata target Session scope mismatch",
                )
        associations = await self._lock_associations(
            request,
            session_rows=session_rows,
        )
        if request.chat_id is None and plan.create_if_absent:
            winner = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_chats WHERE session_id=%s "
                "AND deleted_at IS NULL ORDER BY registry_order,id LIMIT 1 FOR UPDATE",
                (request.session_id,),
            )
            if (
                winner is not None
                and tuple(
                    winner[key]
                    for key in ("tenant_key", "source_key", "agent_key")
                )
                != request.scope.sql_values
            ):
                raise IntegrityError("draft Chat scope association mismatch")
            associations = (associations[0], winner, associations[2])
        return associations, target

    @staticmethod
    def _validate_associations(request, session, chat, turn):
        for row, identity in (
            (session, request.session_id),
            (chat, request.chat_id),
        ):
            if identity is None:
                continue
            if row is None or row["id"] != identity:
                raise IntegrityError("publication association is missing")
            if (
                tuple(
                    row[key]
                    for key in ("tenant_key", "source_key", "agent_key")
                )
                != request.scope.sql_values
            ):
                raise IntegrityError("publication scope association mismatch")
        if chat is not None and chat["session_id"] != request.session_id:
            raise IntegrityError("publication Chat association mismatch")
        if request.turn_id is not None:
            if turn is None or turn["id"] != request.turn_id:
                raise IntegrityError("publication Turn association is missing")
            if (
                turn["session_id"] != request.session_id
                or turn["chat_id"] != request.chat_id
            ):
                raise IntegrityError("publication Turn association mismatch")

    @staticmethod
    def _validate_revisions(request, session, chat):
        if chat is not None:
            if (
                chat["session_id"] != request.session_id
                or chat["deleted_at"] is not None
            ):
                raise ConflictError("Chat association changed or deleted")
            if (
                request.base_history_revision is not None
                and chat["history_revision"] != request.base_history_revision
            ):
                raise ConflictError("Chat history revision changed")
            if request.epoch is not None and chat["epoch"] != request.epoch:
                raise ConflictError("Chat checkpoint epoch changed")
        if (
            session is not None
            and request.base_state_revision is not None
            and session["state_revision"] != request.base_state_revision
        ):
            raise ConflictError("Session state revision changed")

    @staticmethod
    def _validate_authority(request, session, chat, turn):
        PublicationStore._validate_associations(request, session, chat, turn)
        PublicationStore._validate_revisions(request, session, chat)
        if request.turn_id is not None:
            PublicationStore._validate_turn_authority(request, session, turn)
        elif session is not None:
            if (
                session["active_turn_id"] is not None
                or session["generation"] != request.generation
            ):
                raise ConflictError("idle publication authority changed")
            if request.base_state_revision is None:
                raise ConflictError(
                    "idle publication authority requires state revision",
                )

    @staticmethod
    def _validate_turn_authority(request, session, turn):
        if (
            turn is None
            or turn["session_id"] != request.session_id
            or turn["chat_id"] != request.chat_id
        ):
            raise IntegrityError("publication Turn association mismatch")
        current = (
            session["generation"],
            session["owner_key"],
            session["active_turn_id"],
        )
        expected = (
            request.generation,
            request.owner_key.encode("utf-8"),
            request.turn_id,
        )
        if current != expected or not session["lease_valid"]:
            raise ConflictError("publication execution authority expired")
        if turn["generation"] != request.generation or turn["state"] not in (
            "reserved",
            "admitted",
            "running",
            "stopping",
            "settling",
        ):
            raise ConflictError("publication Turn authority changed")

    async def _install(
        self,
        row,
        plan,
        index,
        *,
        associations=None,
        target_session=None,
    ):
        if plan.chat_metadata is not None:
            request = _request(row)
            if associations is None:
                raise IntegrityError(
                    "Chat metadata association locks are missing",
                )
            chat_id = await install_chat_metadata(
                self.db,
                request,
                plan.chat_metadata,
                row,
                index,
                associations,
                target_session=target_session,
            )
            await self.db.execute(
                f"UPDATE {TABLE} SET chat_id=%s WHERE id=%s",
                (chat_id, row["id"]),
            )
        if plan.recovery_block is not None:
            if row["base_state_revision"] is None:
                raise ConflictError(
                    "recovery publication authority requires state revision",
                )
            extent = index.blocks.get(plan.recovery_block)
            if (
                extent is None
                or extent.kind != "recovery"
                or row["session_id"] is None
            ):
                raise IntegrityError(
                    "publication recovery block is missing or invalid",
                )
            await self.db.execute(
                "UPDATE swe_conversation_sessions SET recovery_publication_id=%s,"
                "recovery_offset=%s,recovery_length=%s,state_revision=state_revision+1,"
                "updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                (row["id"], extent.offset, extent.length, row["session_id"]),
            )

    async def commit(self, operation_key: str):
        try:
            initial = await self.get(operation_key)
            if initial is None:
                raise UnknownCommit(operation_key)
            if initial["state"] == "committed":
                return initial
            if initial["state"] != "prepared":
                raise ConflictError("publication operation is aborted")
            if not initial.get("durable_at") or initial["byte_length"] is None:
                raise ConflictError(
                    "publication has no confirmed durable NAS body",
                )
            request = _request(initial)
            plan = CommitPlan.parse(initial["result_metadata_text"])
            index = await self._nas_io(
                operation_key,
                self.bundles.inspect,
                _text(initial["relative_path"]),
                expected_length=initial["byte_length"],
            )
            if index.metadata.get("operation_key") != operation_key:
                raise IntegrityError("bundle operation identity mismatch")
            async with self.db.transaction():
                target_session = None
                if plan.chat_metadata is not None:
                    associations, target_session = (
                        await self._lock_metadata_associations(
                            request,
                            plan.chat_metadata,
                        )
                    )
                else:
                    associations = await self._lock_associations(request)
                row = await self.get(operation_key, lock=True)
                self._matches(row, request, initial["result_metadata_text"])
                if row["state"] == "committed":
                    return row
                if row["state"] != "prepared":
                    raise ConflictError("publication operation is aborted")
                if plan.chat_metadata is not None:
                    self._validate_associations(request, *associations)
                    if (
                        associations[1] is not None
                        and associations[1]["deleted_at"] is not None
                    ):
                        raise ConflictError("Chat was deleted")
                else:
                    self._validate_authority(request, *associations)
                await self._install(
                    row,
                    plan,
                    index,
                    associations=associations,
                    target_session=target_session,
                )
                await self.db.execute(
                    f"UPDATE {TABLE} SET state='committed',committed_at=UTC_TIMESTAMP(6) "
                    "WHERE state='prepared' AND id=%s",
                    (row["id"],),
                )
            return await self.get(operation_key)
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error

    async def abort(self, request: PublicationRequest, *, owner_stopped: bool):
        """Fence a positively stopped writer; retain the unique tombstone forever."""
        if not owner_stopped:
            raise ConflictError("original owner must be confirmed stopped")
        try:
            initial = await self.get(request.operation_key)
            metadata_plan = None
            if initial is not None:
                self._matches(initial, request)
                metadata_plan = CommitPlan.parse(
                    initial["result_metadata_text"],
                ).chat_metadata
            async with self.db.transaction():
                if metadata_plan is not None:
                    associations, _ = await self._lock_metadata_associations(
                        request,
                        metadata_plan,
                    )
                    session, chat, turn = associations
                else:
                    session, chat, turn = await self._lock_associations(
                        request,
                    )
                self._validate_associations(request, session, chat, turn)
                await self._insert(
                    request,
                    CommitPlan().serialize(),
                    "aborted",
                )
                row = await self.get(request.operation_key, lock=True)
                self._matches(row, request)
                if row["state"] == "committed":
                    return row
                if (
                    session is not None
                    and session["generation"] == request.generation
                    and request.kind != "chat_metadata"
                ):
                    await self.db.execute(
                        "UPDATE swe_conversation_sessions SET generation=generation+1,"
                        "owner_key=NULL,lease_until=NULL WHERE id=%s",
                        (request.session_id,),
                    )
                await self.db.execute(
                    f"UPDATE {TABLE} SET state='aborted',aborted_at=UTC_TIMESTAMP(6) WHERE id=%s",
                    (row["id"],),
                )
            return await self.get(request.operation_key)
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(request.operation_key) from error

    @staticmethod
    def _validate_reconciliation(request, associations, expected_generation):
        session, chat, turn = associations
        PublicationStore._validate_associations(request, *associations)
        PublicationStore._validate_revisions(request, session, chat)
        if (
            session is None
            or turn is None
            or request.base_state_revision is None
        ):
            raise ConflictError(
                "reconciliation requires an admitted Turn and state revision",
            )
        if (
            type(expected_generation) is not int
            or session["generation"] != expected_generation
        ):
            raise ConflictError("reconciliation generation changed")
        if expected_generation < request.generation:
            raise ConflictError(
                "reconciliation generation predates publication",
            )
        allowed_owners = (None, request.owner_key.encode("utf-8"))
        if session["owner_key"] not in allowed_owners:
            raise ConflictError("reconciliation generation has another owner")
        if (
            expected_generation > request.generation
            and session["owner_key"] is not None
        ):
            raise ConflictError(
                "reconciliation newer generation must have no owner",
            )
        if (
            session["active_turn_id"] != request.turn_id
            or turn["generation"] != request.generation
        ):
            raise ConflictError("reconciliation Turn authority changed")
        if turn["state"] not in (
            "admitted",
            "running",
            "stopping",
            "settling",
        ):
            raise ConflictError("reconciliation Turn is no longer admitted")

    async def reconcile(
        self,
        operation_key: str,
        *,
        owner_stopped: bool,
        expected_generation: int,
    ):
        """Settle one durable result after observed owner exit; never resume Agent IO."""
        if not owner_stopped:
            raise ConflictError("original owner must be confirmed stopped")
        try:
            initial = await self.get(operation_key)
            if initial is None:
                raise UnknownCommit(operation_key)
            if initial["state"] == "committed":
                return initial
            if initial["state"] != "prepared" or not initial.get("durable_at"):
                raise ConflictError(
                    "reconciliation requires a durable prepared publication",
                )
            request = _request(initial)
            index = await self._nas_io(
                operation_key,
                self.bundles.inspect,
                _text(initial["relative_path"]),
                expected_length=initial["byte_length"],
            )
            if index.metadata.get("operation_key") != operation_key:
                raise IntegrityError("bundle operation identity mismatch")
            async with self.db.transaction():
                associations = await self._lock_associations(request)
                row = await self.get(operation_key, lock=True)
                self._matches(row, request, initial["result_metadata_text"])
                if row["state"] == "committed":
                    return row
                if row["state"] != "prepared":
                    raise ConflictError("publication operation is aborted")
                self._validate_reconciliation(
                    request,
                    associations,
                    expected_generation,
                )
                await self.db.execute(
                    "UPDATE swe_conversation_sessions SET generation=generation+1,owner_key=NULL,lease_until=NULL WHERE id=%s",
                    (request.session_id,),
                )
                await self.db.execute(
                    "UPDATE swe_conversation_turns SET state='settling' WHERE id=%s",
                    (request.turn_id,),
                )
                await self._install(
                    row,
                    CommitPlan.parse(row["result_metadata_text"]),
                    index,
                )
                await self.db.execute(
                    f"UPDATE {TABLE} SET state='committed',committed_at=UTC_TIMESTAMP(6) WHERE state='prepared' AND id=%s",
                    (row["id"],),
                )
            return await self.get(operation_key)
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error
