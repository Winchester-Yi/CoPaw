# -*- coding: utf-8 -*-
"""Offline-only typed installs through the normal publication commit protocol."""

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import json
import re

from ..bundle import BundleBlock, BundleIndex, BundleStore
from ..chat_mutations import load_metadata_json
from ..errors import ConflictError, IntegrityError, UnknownCommit
from ..publications import (
    CommitPlan,
    PublicationStore,
    PublicationRequest,
    _request as PublicationRequestFrom,
)
from ..retention import RetentionStore
from ..publication_associations import PublicationAssociations
from .parallel import MigrationIO

COLUMNS = {
    "sessions": {
        "id",
        "service_unit_key",
        "tenant_key",
        "source_key",
        "agent_key",
        "user_key",
        "channel_key",
        "external_session_key",
        "generation",
        "state_revision",
        "history_high",
        "context_epoch",
        "context_usage_text",
        "context_usage_invalid",
        "recovery_publication_id",
        "recovery_offset",
        "recovery_length",
        "created_at",
        "updated_at",
    },
    "chats": {
        "id",
        "session_id",
        "external_chat_key",
        "external_session_key",
        "registry_order",
        "service_unit_key",
        "tenant_key",
        "source_key",
        "agent_key",
        "user_key",
        "channel_key",
        "title",
        "metadata_text",
        "metadata_revision",
        "expert_binding_revision",
        "expert_id",
        "content_meta_publication_id",
        "content_meta_offset",
        "content_meta_length",
        "history_revision",
        "history_high",
        "next_turn_order",
        "epoch",
        "event_high",
        "next_boundary_order",
        "active_checkpoint_publication_id",
        "checkpoint_revision",
        "status",
        "created_at",
        "updated_at",
        "deleted_at",
    },
    "turns": {
        "id",
        "service_unit_key",
        "chat_id",
        "session_id",
        "question_msg_key",
        "runtime_turn_key",
        "turn_order",
        "context_epoch",
        "generation",
        "state",
        "history_status",
        "admission_publication_id",
        "terminal_publication_id",
        "execution_identity_text",
        "admitted_at",
        "terminal_at",
    },
    "messages": {
        "id",
        "service_unit_key",
        "session_id",
        "origin_chat_id",
        "archive_chat_id",
        "layer",
        "session_order",
        "archive_order",
        "turn_id",
        "source_msg_key",
        "source_occurrence_no",
        "role_key",
        "kind_key",
        "task_run_key",
        "publication_id",
        "byte_offset",
        "byte_length",
        "archive_publication_id",
        "context_epoch",
        "in_context",
        "current_visible",
        "message_timestamp",
        "deleted_at",
    },
    "shares": {
        "service_unit_key",
        "token",
        "chat_id",
        "creator_id",
        "tenant_id",
        "snapshot_key",
        "created_at",
        "access_count",
        "last_access_at",
        "publication_id",
        "byte_offset",
        "byte_length",
    },
    "publications": {
        "id",
        "session_id",
        "chat_id",
        "turn_id",
        "epoch",
        "boundary_key",
        "boundary_order",
        "archive_first_order",
        "archive_last_order",
        "archive_source_count",
        "archive_display_count",
        "archive_first_source_msg_key",
        "archive_last_source_msg_key",
        "archive_first_timestamp",
        "archive_last_timestamp",
        "archive_offset",
        "archive_length",
        "checkpoint_revision",
        "checkpoint_base_revision",
        "checkpoint_event_watermark",
        "checkpoint_source_high",
        "checkpoint_offset",
        "checkpoint_length",
        "event_first",
        "event_last",
        "event_offset",
        "event_length",
        "retained_history",
        "committed_at",
    },
}
TABLES = {key: "swe_conversation_" + key for key in COLUMNS}
TABLES["shares"] = "swe_chat_shares"
BINARY = {
    key
    for columns in COLUMNS.values()
    for key in columns
    if key.endswith("_key")
} | {"expert_id"}
TIME = {
    key
    for columns in COLUMNS.values()
    for key in columns
    if key.endswith(("_at", "_timestamp"))
}
IMMUTABLE = {
    "sessions": {
        "id",
        "service_unit_key",
        "tenant_key",
        "source_key",
        "agent_key",
        "user_key",
        "channel_key",
        "external_session_key",
        "created_at",
    },
    "chats": {
        "id",
        "session_id",
        "external_chat_key",
        "external_session_key",
        "registry_order",
        "service_unit_key",
        "tenant_key",
        "source_key",
        "agent_key",
        "user_key",
        "channel_key",
        "created_at",
    },
    "turns": {
        "id",
        "service_unit_key",
        "session_id",
        "chat_id",
        "question_msg_key",
        "turn_order",
        "context_epoch",
        "generation",
    },
    "messages": COLUMNS["messages"],
    "publications": {
        "id",
        "session_id",
        "chat_id",
        "turn_id",
        "epoch",
        "boundary_key",
        "boundary_order",
        "archive_first_order",
        "archive_last_order",
        "archive_source_count",
        "archive_display_count",
        "archive_first_source_msg_key",
        "archive_last_source_msg_key",
        "archive_first_timestamp",
        "archive_last_timestamp",
        "archive_offset",
        "archive_length",
        "checkpoint_revision",
        "checkpoint_base_revision",
        "checkpoint_event_watermark",
        "checkpoint_source_high",
        "checkpoint_offset",
        "checkpoint_length",
        "event_first",
        "event_last",
        "event_offset",
        "event_length",
        "retained_history",
    },
}
_PLAN: ContextVar[dict | None] = ContextVar(
    "offline_conversation_import_plan",
    default=None,
)
_LOCKED: ContextVar[dict | None] = ContextVar(
    "offline_conversation_import_rows",
    default=None,
)


@dataclass(frozen=True)
class OfflineTarget:
    schema: str
    application_schema: str
    migration_id: str

    def __post_init__(self):
        if self.schema == self.application_schema or not re.fullmatch(
            r"(?:test_conversation_[a-zA-Z0-9_]+|conversation_staging_[a-zA-Z0-9_]+)",
            self.schema,
        ):
            raise ValueError(
                "offline migration requires a dedicated staging schema",
            )
        if not self.migration_id:
            raise ValueError("migration identity is required")


@dataclass(frozen=True)
class OfflineConfirmTicket:
    request: PublicationRequest
    publication_id: int
    relative_path: str
    plan_text: str


@dataclass(frozen=True)
class OfflineConfirmReceipt:
    ticket: OfflineConfirmTicket
    index: BundleIndex


def _table(name):
    if name not in TABLES:
        raise IntegrityError("unknown offline import table")
    return TABLES[name]


def _sql_value(key, value):
    if value is not None and key in BINARY:
        return value.encode("utf-8")
    if value is not None and key in TIME:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise IntegrityError(
                "offline timestamp requires explicit timezone",
            )
        return stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class OfflineImportStore(PublicationStore):
    def __init__(self, database, bundles, target, *, service_unit_id):
        super().__init__(database, bundles, service_unit_id=service_unit_id)
        self.target = target
        self._reservation_ids = {}
        self._confirm_tickets = {}
        self._confirm_receipts = {}

    def _check_confirm_ticket(self, ticket):
        if self._confirm_tickets.get(id(ticket)) is not ticket:
            raise IntegrityError("unknown offline confirmation ticket")
        self._validate_service(ticket.request)
        if ticket.relative_path != BundleStore.path_for(
            ticket.request.operation_key,
            ticket.request.scope,
            scope_kind=ticket.request.scope_kind,
        ):
            raise IntegrityError("offline confirmation ticket path mismatch")

    async def prepare_confirm_bundle(self, operation_key):
        """SQL-only admission for confirming an existing migration bundle."""
        row = await self.get(operation_key)
        if row is None or row["state"] != "prepared":
            raise ConflictError(
                "publication must be prepared before confirmation",
            )
        await self._validate_prepared_references(row)
        request = PublicationRequestFrom(row)
        self._validate_service(request)
        ticket = OfflineConfirmTicket(
            request,
            row["id"],
            BundleStore.validate_publication_locator(row),
            row["result_metadata_text"],
        )
        self._confirm_tickets[id(ticket)] = ticket
        return ticket

    async def confirm_bundle_nas(self, ticket, io):
        """NAS-only confirmation; never access a database from an IO worker."""
        self._check_confirm_ticket(ticket)
        try:
            index = await io.call(
                self.bundles.confirm_durable,
                ticket.relative_path,
            )
        except asyncio.CancelledError as error:
            raise UnknownCommit(ticket.request.operation_key) from error
        if (
            index.relative_path != ticket.relative_path
            or index.metadata.get("operation_key")
            != ticket.request.operation_key
        ):
            raise IntegrityError("bundle operation identity or path mismatch")
        receipt = OfflineConfirmReceipt(ticket, index)
        self._confirm_receipts[id(receipt)] = (receipt, deepcopy(index))
        return receipt

    async def commit_confirm_bundle(self, receipt):
        """SQL-only durable update after revalidating the frozen NAS receipt."""
        known = self._confirm_receipts.get(id(receipt))
        if (
            known is None
            or known[0] is not receipt
            or known[1] != receipt.index
        ):
            raise IntegrityError(
                "unknown or altered offline confirmation receipt",
            )
        ticket = receipt.ticket
        self._check_confirm_ticket(ticket)
        async with self.db.transaction():
            row = await self.get(ticket.request.operation_key)
            if row is None or row["state"] != "prepared":
                raise ConflictError(
                    "publication ended during NAS confirmation",
                )
            await self._validate_prepared_references(row)
            current = await self.get(ticket.request.operation_key, lock=True)
            self._matches(current, ticket.request, ticket.plan_text)
            if (
                current["id"] != ticket.publication_id
                or BundleStore.validate_publication_locator(current)
                != ticket.relative_path
            ):
                raise IntegrityError(
                    "offline confirmation publication changed",
                )
            await self._mark_durable(ticket.request.operation_key, known[1])
        self._confirm_receipts.pop(id(receipt))
        self._confirm_tickets.pop(id(ticket))
        return receipt.index

    async def confirm_bundle(self, operation_key):
        ticket = await self.prepare_confirm_bundle(operation_key)
        async with MigrationIO(workers=1) as io:
            receipt = await self.confirm_bundle_nas(ticket, io)
        return await self.commit_confirm_bundle(receipt)

    async def _validate_reference_ownership(self, request, plan):
        # Import rows may be created by this batch. The typed import plan is
        # loaded only at commit; existing request parents are checked before IO.
        if _PLAN.get() is not None:
            return await super()._validate_reference_ownership(request, plan)
        self._validate_service(request)
        associations = await PublicationAssociations._lock_associations(
            self,
            request,
        )
        PublicationStore._validate_associations(request, *associations)

    async def validate_target(self):
        row = await self.db.fetch_one("SELECT DATABASE() AS schema_name")
        if (
            not row
            or row["schema_name"] != self.target.schema
            or self.db.config.database != self.target.schema
        ):
            raise IntegrityError(
                "offline import target differs from verified staging schema",
            )

    async def reserve_batch(self, request, publication_id, plan=None):
        await self.validate_target()
        self._reservation_ids[request.operation_key] = publication_id
        return await self.reserve(
            request,
            plan or CommitPlan(result_ids=(publication_id,)),
        )

    async def verify_batch(self, request, publication_id, plan, expected):
        current = await self.get(request.operation_key)
        self._matches(current, request, plan.serialize())
        if current["id"] != publication_id:
            raise IntegrityError(
                "committed publication ID differs from manifest",
            )
        stored = await self._load_plan(current)
        if stored != expected:
            raise IntegrityError(
                "original import plan differs from current source mapping",
            )
        await self._verify_records(current, expected)
        if plan.share is not None:
            await self._verify_share_record(plan.share)
        return current

    async def _verify_share_record(self, plan):
        from ..chat_mutations import utc_datetime

        actual = await self.db.fetch_one(
            "SELECT * FROM swe_chat_shares WHERE token=%s",
            (plan.token,),
        )
        if actual is None:
            raise IntegrityError("committed imported share row is missing")
        for key in (
            "token",
            "chat_id",
            "creator_id",
            "tenant_id",
            "snapshot_key",
            "created_at",
            "access_count",
            "last_access_at",
        ):
            value = getattr(plan, key)
            if key in ("created_at", "last_access_at") and value is not None:
                value = utc_datetime(value)
            if actual[key] != value:
                raise IntegrityError(
                    "committed imported share identity or counter changed",
                )

    async def _verify_records(self, publication, plan):
        index = await self._nas_io(
            publication["operation_key"].decode(),
            self.bundles.inspect,
            BundleStore.validate_publication_locator(publication),
            expected_length=publication["byte_length"],
        )
        for item in [*plan["rows"], *plan["updates"]]:
            table = item["table"]
            values = self._resolve(item["values"], publication, index)
            actual = await self.db.fetch_one(
                "SELECT * FROM " + _table(table) + " WHERE id=%s",
                (values["id"],),
            )
            mismatch = (
                []
                if actual is None
                else [
                    key
                    for key, value in values.items()
                    if key in IMMUTABLE[table] and actual[key] != value
                ]
            )
            if actual is None or mismatch:
                raise IntegrityError(
                    "committed target mapping or immutable typed row differs: "
                    + table
                    + ":"
                    + ",".join(mismatch),
                )

    async def verify_blocks(self, row, blocks):
        operation = row["operation_key"].decode()
        index = await self._nas_io(
            operation,
            self.bundles.inspect,
            BundleStore.validate_publication_locator(row),
            expected_length=row["byte_length"],
        )
        for block in blocks:
            extent = index.blocks.get(block.name)
            if extent is None or extent.kind != block.kind:
                raise IntegrityError(
                    "original migration bundle block identity differs",
                )
            raw = await self._nas_io(
                operation,
                self.bundles.read_extent,
                index.relative_path,
                extent.offset,
                extent.length,
                expected_length=index.byte_length,
            )
            if raw != block.content:
                if block.kind != "recovery" or not await self._same_recovery(
                    row,
                    index,
                    raw,
                    block.content,
                ):
                    raise IntegrityError(
                        "original migration bundle content differs from frozen source",
                    )

    async def _same_recovery(self, row, index, raw, expected):
        from ..projection_references import snapshot_projection, ref_key
        from ..session_projection import hydrate_state, is_projection
        from ..identity import WorkspaceScope

        value = load_metadata_json(raw)
        if not is_projection(value):
            return False
        request = PublicationRequestFrom(row)
        snapshot = await snapshot_projection(
            self,
            request,
            value,
            index,
            current_id=row["id"],
        )
        try:
            actual = hydrate_state(
                value,
                lambda ref: snapshot.payloads[ref_key(ref)],
            )
            return actual == load_metadata_json(expected)
        finally:
            await snapshot.finish(RetentionStore(self.db, request.scope))

    async def _load_plan(self, row):
        operation = row["operation_key"].decode()
        index = await self._nas_io(
            operation,
            self.bundles.inspect,
            BundleStore.validate_publication_locator(row),
            expected_length=row["byte_length"],
        )
        extent = index.blocks.get("import_plan")
        if extent is None or extent.kind != "import_plan":
            raise IntegrityError("offline import plan block is missing")
        raw = await self._nas_io(
            operation,
            self.bundles.read_extent,
            index.relative_path,
            extent.offset,
            extent.length,
            expected_length=index.byte_length,
        )
        plan = load_metadata_json(raw)
        self._validate_plan(plan)
        return plan

    async def _insert(self, request, plan_text, state):
        identity = self._reservation_ids.get(request.operation_key)
        if identity is None:
            raise IntegrityError(
                "offline publication ID must be persisted in manifest first",
            )
        values = {
            "id": identity,
            **request.fields(),
            "state": state,
            "result_metadata_text": plan_text,
        }
        await self.db.insert(
            "INSERT INTO swe_conversation_publications ("
            + ",".join(values)
            + ",created_at) VALUES ("
            + ",".join(["%s"] * len(values))
            + ",UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",
            tuple(values.values()),
        )
        current = await self.get(request.operation_key)
        if current is None or current["id"] != identity:
            raise IntegrityError(
                "offline publication mapping conflicts with existing target identity",
            )

    async def commit(self, operation_key):
        await self.validate_target()
        initial = await self.get(operation_key)
        if initial and initial["state"] == "committed":
            return initial
        if initial is None or not initial.get("durable_at"):
            raise ConflictError(
                "offline publication requires a durable original bundle",
            )
        await self._validate_reference_ownership(
            PublicationRequestFrom(initial),
            CommitPlan.parse(initial["result_metadata_text"]),
        )
        plan = await self._load_plan(initial)
        token = _PLAN.set(plan)
        locked_token = _LOCKED.set(
            {table: {} for table in ("sessions", "chats", "turns")},
        )
        try:
            return await super().commit(operation_key)
        finally:
            _LOCKED.reset(locked_token)
            _PLAN.reset(token)

    def _validate_plan(self, plan):
        if (
            not isinstance(plan, dict)
            or set(plan)
            != {
                "version",
                "migration_id",
                "schema",
                "rows",
                "updates",
                "locks",
            }
            or plan["version"] != 1
            or plan["migration_id"] != self.target.migration_id
            or plan["schema"] != self.target.schema
        ):
            raise IntegrityError("offline installation plan identity mismatch")
        self._validate_collections(plan)
        for item in [*plan["rows"], *plan["updates"]]:
            self._validate_typed_record(item)

    @staticmethod
    def _validate_collections(plan):
        if (
            not isinstance(plan["rows"], list)
            or not isinstance(plan["updates"], list)
            or not isinstance(plan["locks"], dict)
            or set(plan["locks"]) - {"sessions", "chats", "turns"}
        ):
            raise IntegrityError(
                "offline installation collections are invalid",
            )
        for identities in plan["locks"].values():
            if not isinstance(identities, list) or any(
                type(value) is not int or value <= 0 for value in identities
            ):
                raise IntegrityError("offline lock identity is invalid")

    @staticmethod
    def _validate_typed_record(item):
        if not isinstance(item, dict) or set(item) != {"table", "values"}:
            raise IntegrityError("offline typed record shape is invalid")
        table = item.get("table")
        values = item.get("values")
        if (
            table not in COLUMNS
            or not isinstance(values, dict)
            or set(values) - COLUMNS[table]
        ):
            raise IntegrityError(
                "offline installation uses an unknown typed field",
            )
        if table == "shares":
            raise IntegrityError("offline shares require typed ShareInstall")
        if type(values.get("id")) is not int or values["id"] <= 0:
            raise IntegrityError(
                "offline installation requires preassigned identity",
            )

    async def _lock_associations(
        self,
        request,
        *,
        session_rows=None,
        archive_turns=None,
        archive_chat_ids=(),
    ):
        await RetentionStore(
            self.db,
            request.scope,
            scope_kind=request.scope_kind,
        ).lock_guard()
        plan = _PLAN.get()
        cached = _LOCKED.get()
        declared = {
            table: set(plan["locks"].get(table, [])) for table in cached
        }
        for item in [*plan["rows"], *plan["updates"]]:
            if item["table"] in declared:
                declared[item["table"]].add(item["values"]["id"])
        associations = (
            ("sessions", request.session_id),
            ("chats", request.chat_id),
            ("turns", request.turn_id),
        )
        for table, identity in associations:
            if identity is not None and identity not in declared[table]:
                raise IntegrityError(
                    "offline request association is not declared in sorted lock set",
                )
        for table, identities in declared.items():
            for identity in sorted(identities):
                cached[table][identity] = await self.db.fetch_one(
                    "SELECT * FROM "
                    + _table(table)
                    + " WHERE id=%s FOR UPDATE",
                    (identity,),
                )
        for table, values in cached.items():
            for row in values.values():
                if row is not None:
                    self._validate_relation(table, row, request.fields())
        return tuple(
            None if identity is None else cached[table][identity]
            for table, identity in associations
        )

    @staticmethod
    def _validate_authority(request, session, chat, turn):
        if request.kind != "offline_import" or _PLAN.get() is None:
            raise IntegrityError("offline installation plan is not loaded")
        PublicationStore._validate_associations(request, session, chat, turn)
        if session is not None and session["active_turn_id"] is not None:
            raise ConflictError(
                "offline import cannot mutate active execution",
            )

    @staticmethod
    def _validate_share_authority(request, plan, associations):
        if request.kind != "share_import" or _PLAN.get() is None:
            PublicationStore._validate_share_authority(
                request,
                plan,
                associations,
            )
            return
        PublicationStore._validate_associations(request, *associations)
        if (
            request.scope_kind == "workspace"
            and associations[1]["external_chat_key"] != plan.chat_id.encode()
        ):
            raise IntegrityError("imported share Chat identity mismatch")

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
        if plan.messages:
            raise IntegrityError(
                "offline projection additions require explicit preassigned Message rows",
            )
        imported = _PLAN.get()
        for item in imported["rows"]:
            await self._apply_record(item, row, index, update=False)
        for item in imported["updates"]:
            await self._apply_record(item, row, index, update=True)
        if plan.share is not None:
            await self._install_share(
                PublicationRequestFrom(row),
                plan.share,
                row,
                index,
            )

    async def _install_share(self, request, plan, row, index):
        from ..shares import install_share

        await install_share(self.db, request, plan, row, index)

    def _resolve(self, values, publication, index):
        result = {}
        for key, value in values.items():
            if isinstance(value, dict):
                if value == {"publication": True}:
                    result[key] = publication["id"]
                elif set(value) == {"block", "part"} and value["part"] in (
                    "offset",
                    "length",
                ):
                    extent = index.blocks.get(value["block"])
                    if extent is None:
                        raise IntegrityError(
                            "import locator references a missing block",
                        )
                    result[key] = getattr(extent, value["part"])
                else:
                    raise IntegrityError(
                        "unsupported offline locator expression",
                    )
            else:
                result[key] = _sql_value(key, value)
        return result

    def _related(self, table, identity):
        cached = _LOCKED.get()
        if cached is None or identity not in cached[table]:
            raise IntegrityError(
                "offline relation is not declared in sorted lock set",
            )
        row = cached[table][identity]
        if row is None:
            raise IntegrityError("offline related identity is missing")
        return row

    def _validate_relation(self, table, values, publication):
        scope = ("service_unit_key", "tenant_key", "source_key", "agent_key")
        if table in ("sessions", "chats") and any(
            values.get(key) != publication[key] for key in scope
        ):
            raise IntegrityError("offline record scope mismatch")
        if (
            table in ("turns", "messages")
            and values.get("service_unit_key")
            != publication["service_unit_key"]
        ):
            raise IntegrityError("offline record service scope mismatch")
        if table in ("chats", "turns", "messages"):
            self._validate_record_relations(values, publication)
        if table == "publications":
            if values["id"] != publication["id"]:
                raise IntegrityError(
                    "offline update cannot mutate another publication",
                )
            for key in ("session_id", "chat_id", "turn_id", "epoch"):
                if key in values and values[key] != publication[key]:
                    raise IntegrityError(
                        "offline publication immutable association changed",
                    )
        if table == "shares":
            raise IntegrityError(
                "offline shares require the typed ShareInstall protocol",
            )

    def _validate_record_relations(self, values, publication):
        scope = ("service_unit_key", "tenant_key", "source_key", "agent_key")
        session = self._related("sessions", values["session_id"])
        if any(session[key] != publication[key] for key in scope):
            raise IntegrityError("offline related Session scope mismatch")
        if (
            "external_session_key" in values
            and values["external_session_key"]
            != session["external_session_key"]
        ):
            raise IntegrityError(
                "offline Chat Session key projection mismatch",
            )
        for key in ("chat_id", "origin_chat_id", "archive_chat_id"):
            if values.get(key) is not None:
                chat = self._related("chats", values[key])
                if chat["session_id"] != session["id"] or any(
                    chat[name] != publication[name] for name in scope
                ):
                    raise IntegrityError(
                        "offline Chat Session relation mismatch",
                    )
        if values.get("turn_id") is not None:
            turn = self._related("turns", values["turn_id"])
            origin = values.get("origin_chat_id")
            if (
                turn["service_unit_key"] != publication["service_unit_key"]
                or turn["session_id"] != session["id"]
                or (origin is not None and origin != turn["chat_id"])
            ):
                raise IntegrityError("offline Message Turn relation mismatch")

    async def _apply_record(self, item, publication, index, *, update):
        table = item["table"]
        values = self._resolve(item["values"], publication, index)
        key = "token" if table == "shares" else "id"
        cached = _LOCKED.get()
        if table in ("sessions", "chats", "turns"):
            if cached is None or values[key] not in cached[table]:
                raise IntegrityError("offline mutation target was not locked")
            current = cached[table][values[key]]
        elif table == "publications":
            current = publication if values[key] == publication["id"] else None
        else:
            current = None
            if update:
                raise IntegrityError("offline mutable row is not supported")
        if update and current is None:
            raise IntegrityError("offline update target is missing")
        if not update and current is not None:
            raise IntegrityError(
                "offline preassigned target identity already exists",
            )
        merged = {**(current or {}), **values}
        self._validate_relation(table, merged, publication)
        if update:
            await self._update_record(table, key, values, current)
        else:
            await self.db.insert(
                "INSERT INTO "
                + _table(table)
                + " ("
                + ",".join(values)
                + ") VALUES ("
                + ",".join(["%s"] * len(values))
                + ")",
                tuple(values.values()),
            )
        if table in ("sessions", "chats", "turns"):
            cached[table][merged["id"]] = merged

    async def _update_record(self, table, key, values, current):
        identity = values.pop(key)
        changed = await self.db.execute(
            "UPDATE "
            + _table(table)
            + " SET "
            + ",".join(name + "=%s" for name in values)
            + " WHERE "
            + key
            + "=%s",
            (*values.values(), identity),
        )
        if changed not in (0, 1):
            raise IntegrityError(
                "offline update affected an unexpected row count",
            )
        if changed == 0 and any(
            current.get(name) != value for name, value in values.items()
        ):
            raise ConflictError(
                "offline update did not install expected values",
            )


def plan_block(target, rows, updates=(), locks=None):
    value = {
        "version": 1,
        "migration_id": target.migration_id,
        "schema": target.schema,
        "rows": list(rows),
        "updates": list(updates),
        "locks": locks or {},
    }
    return BundleBlock(
        "import_plan",
        "import_plan",
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode(),
    )
