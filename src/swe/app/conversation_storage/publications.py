# -*- coding: utf-8 -*-
"""NAS-first publication with same-operation commit and explicit reconciliation.

Lock sets are Session -> Chat -> Turn -> publication. No model, tool or NAS
operation runs while SQL locks are held. Transport failures leave the original
operation unknown; they never imply rollback and never unlink final bundles.
"""

import asyncio
from dataclasses import dataclass

from .bundle import BundleBlock, BundleStore
from .errors import ConflictError, IntegrityError, StorageError, UnknownCommit
from .identity import WorkspaceScope, identity_bytes, chat_key_bytes
from .chat_mutations import install_chat_metadata, load_metadata_json
from .retention import RetentionStore
from .message_installs import install_messages, install_turn
from .archive import install_archive
from .archive_payloads import (
    validate_archive_plan,
    validate_archive_payloads,
    validate_request_plan,
)
from .commit_plan import CommitPlan
from .source_messages import SourceMessages, snapshot_sources
from .publication_associations import PublicationAssociations
from .projection_publication import (
    prepare_recovery_blocks,
    effective_recovery_plan,
)
from .projection_references import (
    ProjectionReferences,
    snapshot_projection,
    refresh_projection_cache,
)
from .shares import (
    install_share,
    lock_share_selection,
    validate_share_payload,
    validate_share_request,
)

TABLE = "swe_conversation_publications"
FORMAT_KEY = b"swe-bundle-v1"


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


class PublicationStore(PublicationAssociations):
    """Short SQL state transitions; immutable IO is outside transactions."""

    def __init__(self, database, bundles: BundleStore):
        self.db = database
        self.bundles = bundles

    async def _assert_writes_allowed(self, request):
        await RetentionStore(
            self.db,
            request.scope,
            scope_kind=request.scope_kind,
        ).assert_writes_allowed()

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
        validate_share_request(request, plan.share)
        validate_request_plan(request, plan)
        plan_text = plan.serialize()
        try:
            async with self.db.transaction():
                await self._assert_writes_allowed(request)
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
            raise UnknownCommit(request.operation_key, request) from error

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
        blocks = await prepare_recovery_blocks(
            self,
            row,
            CommitPlan.parse(row["result_metadata_text"]),
            blocks,
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
            initial = await self.get(operation_key)
            if initial is None:
                raise UnknownCommit(operation_key)
            request = _request(initial)
            async with self.db.transaction():
                await self._assert_writes_allowed(request)
                current = await self.get(operation_key, lock=True)
                if current is None or current["state"] != "prepared":
                    raise ConflictError("publication ended during NAS write")
                self._matches(current, request)
                await self.db.execute(
                    f"UPDATE {TABLE} SET byte_length=%s,durable_at=UTC_TIMESTAMP(6) WHERE id=%s",
                    (index.byte_length, current["id"]),
                )
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error

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
    def _validate_share_authority(request, plan, associations):
        PublicationStore._validate_associations(request, *associations)
        if request.scope_kind == "legacy_share":
            return
        PublicationStore._validate_revisions(request, *associations[:2])
        chat = associations[1]
        if chat["deleted_at"] is not None:
            raise ConflictError("share Chat was deleted")
        if chat["external_chat_key"] != chat_key_bytes(plan.chat_id) or chat[
            "user_key"
        ] != identity_bytes(plan.creator_id, 256):
            raise ConflictError("share Chat or creator identity changed")

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
        sources=None,
    ):
        try:
            validate_archive_plan(plan)
        except ValueError as error:
            raise IntegrityError(str(error)) from error
        if plan.share is not None:
            await install_share(self.db, _request(row), plan.share, row, index)
        if (
            plan.turn_install is not None
            and plan.turn_install.phase == "admission"
        ):
            self._validate_admission_anchor(_request(row), plan, associations)
        await self._install_archive_plan(row, plan, index, associations)
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
        if plan.messages:
            if associations is None:
                raise IntegrityError("Message association locks are missing")
            await install_messages(
                self.db,
                _request(row),
                plan.messages,
                row,
                index,
                associations,
                sources=sources.rows if sources is not None else None,
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
        if plan.turn_install is not None:
            if associations is None:
                raise IntegrityError("Turn association locks are missing")
            await install_turn(
                self.db,
                _request(row),
                plan.turn_install,
                row,
                associations,
            )

    async def _install_archive_plan(self, row, plan, index, associations):
        if any(
            (
                plan.archive,
                plan.checkpoint,
                plan.events,
                plan.reset,
                plan.prune_ids,
            ),
        ):
            if associations is None:
                raise IntegrityError("archive association locks are missing")
            await install_archive(
                self.db,
                _request(row),
                row,
                index,
                associations,
                archive=plan.archive,
                checkpoint=plan.checkpoint,
                events=plan.events,
                reset=plan.reset,
                prune_ids=plan.prune_ids,
            )

    @staticmethod
    def _validate_admission_anchor(request, plan, associations):
        if associations is None or associations[2] is None:
            raise IntegrityError("admission Turn association is missing")
        turn = associations[2]
        anchors = [
            message
            for message in plan.messages
            if message.layer == "anchor" and message.role == "user"
        ]
        if len(anchors) != 1:
            raise IntegrityError("admission requires exactly one user anchor")
        anchor = anchors[0]
        if (
            anchor.source_msg_key is None
            or identity_bytes(anchor.source_msg_key, 256)
            != turn["question_msg_key"]
        ):
            raise IntegrityError("admission anchor question identity mismatch")
        if (
            anchor.turn_id != request.turn_id
            or anchor.origin_chat_id != request.chat_id
        ):
            raise IntegrityError(
                "admission anchor Turn or Chat identity mismatch",
            )

    async def _lock_commit_associations(self, request, plan, archive_turns):
        target_session = None
        if plan.chat_metadata is not None:
            associations, target_session = (
                await self._lock_metadata_associations(
                    request,
                    plan.chat_metadata,
                )
            )
        else:
            associations = await self._lock_associations(
                request,
                archive_turns=archive_turns,
                archive_chat_ids={
                    item.origin_chat_id
                    for item in plan.messages
                    if item.layer == "archive"
                    and item.origin_chat_id is not None
                },
            )
        if plan.share is not None:
            self._validate_share_authority(
                request,
                plan.share,
                associations,
            )
            await lock_share_selection(self.db, request, plan.share)
        return associations, target_session

    async def commit(self, operation_key: str, *, _settlement_generation=None):
        sources, projected_refs = SourceMessages(), ProjectionReferences()
        request = None
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
            validate_share_request(request, plan.share)
            validate_request_plan(request, plan)
            index = await self._nas_io(
                operation_key,
                self.bundles.inspect,
                _text(initial["relative_path"]),
                expected_length=initial["byte_length"],
            )
            if index.metadata.get("operation_key") != operation_key:
                raise IntegrityError("bundle operation identity mismatch")
            plan, projection = await effective_recovery_plan(
                self,
                request,
                initial,
                plan,
                index,
            )
            await self._validate_admission_body(request, plan, index)
            await validate_share_payload(self, request, plan.share, index)
            sources = await snapshot_sources(self, request, plan)
            projected_refs = await snapshot_projection(
                self,
                request,
                projection,
                index,
                current_id=initial["id"],
            )
            sources.include_projection(projected_refs)
            archive_payloads = await validate_archive_payloads(
                self,
                request,
                plan,
                index,
                sources=sources,
            )
            archive_turns = sources.include_turns(
                await self._archive_turns(request, plan),
            )
            async with self.db.transaction():
                await self._assert_writes_allowed(request)
                associations, target_session = (
                    await self._lock_commit_associations(
                        request,
                        plan,
                        archive_turns,
                    )
                )
                row = await sources.lock_current(self, initial, operation_key)
                self._matches(row, request, initial["result_metadata_text"])
                if row["state"] == "committed":
                    return row
                if row["state"] != "prepared":
                    raise ConflictError("publication operation is aborted")
                await self._validate_commit_mode(
                    request,
                    plan,
                    associations,
                    _settlement_generation,
                )
                archive_payloads.validate_chat(associations[1])
                await sources.install_dependencies(self, request, row)
                await self._install(
                    row,
                    plan,
                    index,
                    associations=associations,
                    target_session=target_session,
                    sources=sources,
                )
                await self.db.execute(
                    f"UPDATE {TABLE} SET state='committed',committed_at=COALESCE(committed_at,UTC_TIMESTAMP(6)) "
                    "WHERE state='prepared' AND id=%s",
                    (row["id"],),
                )
            return await self._committed_projection(
                operation_key,
                projection,
                projected_refs,
            )
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key, request) from error
        finally:
            if request is not None:
                try:
                    await sources.finish(self, request.scope)
                finally:
                    await projected_refs.finish(
                        RetentionStore(self.db, request.scope),
                    )

    async def _committed_projection(
        self,
        operation_key,
        projection,
        projected_refs,
    ):
        committed = await self.get(operation_key)
        await refresh_projection_cache(
            self,
            committed,
            projection,
            projected_refs,
        )
        return committed

    async def _validate_commit_mode(
        self,
        request,
        plan,
        associations,
        generation,
    ):
        if generation is None:
            self._validate_commit_authority(request, plan, associations)
            return
        self._validate_reconciliation(request, associations, generation)
        await self.db.execute(
            "UPDATE swe_conversation_sessions SET generation=generation+1,owner_key=NULL,lease_until=NULL WHERE id=%s",
            (request.session_id,),
        )
        if (
            plan.turn_install is None
            and associations[2]["state"] != "stopping"
        ):
            await self.db.execute(
                "UPDATE swe_conversation_turns SET state='settling' WHERE id=%s",
                (request.turn_id,),
            )

    def _validate_commit_authority(self, request, plan, associations):
        if plan.share is not None:
            self._validate_share_authority(
                request,
                plan.share,
                associations,
            )
        elif plan.chat_metadata is not None:
            self._validate_associations(request, *associations)
            if (
                associations[1] is not None
                and associations[1]["deleted_at"] is not None
            ):
                raise ConflictError("Chat was deleted")
        else:
            self._validate_authority(request, *associations)

    async def _validate_admission_body(self, request, plan, index):
        if plan.turn_install is None or plan.turn_install.phase != "admission":
            return
        anchors = [
            message
            for message in plan.messages
            if message.layer == "anchor" and message.role == "user"
        ]
        if len(anchors) != 1:
            raise IntegrityError("admission requires a user anchor")
        anchor = anchors[0]
        extent = index.blocks.get(anchor.block_name)
        if extent is None:
            raise IntegrityError("admission question block is missing")
        payload = await self._nas_io(
            request.operation_key,
            self.bundles.read_extent,
            index.relative_path,
            extent.offset,
            extent.length,
            expected_length=index.byte_length,
        )
        value = load_metadata_json(payload)
        if (
            not isinstance(value, dict)
            or value.get("role") != "user"
            or value.get("id") != anchor.source_msg_key
        ):
            raise IntegrityError("admission question body identity mismatch")

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
                await self._assert_writes_allowed(request)
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
                    and request.kind
                    not in (
                        "chat_metadata",
                        "share",
                        "share_import",
                    )
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
            raise UnknownCommit(request.operation_key, request) from error

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
        allowed_states = {"admitted", "running", "stopping", "settling"}
        if request.kind == "admission":
            allowed_states = {"reserved"}
        if turn["state"] not in allowed_states:
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
        if type(expected_generation) is not int or expected_generation < 0:
            raise ConflictError(
                "reconciliation generation must be a nonnegative integer",
            )
        return await self.commit(
            operation_key,
            _settlement_generation=expected_generation,
        )
