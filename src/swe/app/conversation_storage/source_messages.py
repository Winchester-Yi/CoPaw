# -*- coding: utf-8 -*-
"""Explicit source occurrence locators reused by archive publications."""

from dataclasses import dataclass, field

from .chat_mutations import load_metadata_json, utc_datetime
from .errors import IntegrityError
from .identity import identity_bytes
from .retention import RetentionStore

SOURCE_FIELDS = (
    "id",
    "session_id",
    "origin_chat_id",
    "turn_id",
    "source_msg_key",
    "source_occurrence_no",
    "role_key",
    "kind_key",
    "task_run_key",
    "publication_id",
    "byte_offset",
    "byte_length",
    "context_epoch",
    "message_timestamp",
    "session_order",
    "layer",
    "current_visible",
    "deleted_at",
)


def _text(value):
    return (
        bytes(value).decode()
        if isinstance(value, (bytes, bytearray))
        else value
    )


@dataclass
class SourceMessages:
    rows: dict = field(default_factory=dict)
    payloads: dict = field(default_factory=dict)
    batch: object = None
    dependency_publications: dict = field(default_factory=dict)
    projection_references: object = None

    @property
    def publication_ids(self):
        return sorted(
            {row["publication_id"] for row in self.rows.values()}
            | set(self.dependency_publications),
        )

    async def finish(self, service, scope):
        if self.batch is not None:
            await RetentionStore(service.db, scope).finish_reader(self.batch)
            self.batch = None

    async def revalidate(self, db):
        if self.projection_references is not None:
            await self.projection_references.revalidate(db)
        for identity, expected in sorted(self.rows.items()):
            row = await db.fetch_one(
                "SELECT * FROM swe_conversation_messages WHERE id=%s FOR SHARE",
                (identity,),
            )
            if row is None or any(
                row[key] != expected[key] for key in SOURCE_FIELDS
            ):
                raise IntegrityError("archive source occurrence changed")

    def include_projection(self, references):
        self.dependency_publications.update(references.publications)
        self.projection_references = references

    def include_turns(self, turns):
        if self.projection_references is not None:
            turns.update(self.projection_references.turns)
        return turns

    async def lock_current(self, service, current, operation_key):
        if self.publication_ids:
            return await self.lock_publications(service, current)
        return await service.get(operation_key, lock=True)

    async def install_dependencies(self, service, request, publication):
        await self.revalidate(service.db)
        if self.publication_ids:
            await RetentionStore(service.db, request.scope).add_dependencies(
                publication["id"],
                self.publication_ids,
            )

    async def lock_publications(self, service, current):
        result = None
        expected = (
            {row["id"]: row for row in self.batch.publications}
            if self.batch is not None
            else {}
        )
        expected.update(self.dependency_publications)
        for identity in sorted({*self.publication_ids, current["id"]}):
            row = await service.db.fetch_one(
                "SELECT * FROM swe_conversation_publications WHERE id=%s FOR UPDATE",
                (identity,),
            )
            if identity == current["id"]:
                result = row
            elif (
                row is None
                or row["state"] != "committed"
                or row["gc_state"] != "live"
            ):
                raise IntegrityError(
                    "archive source publication is not committed/live",
                )
            elif identity >= current["id"]:
                raise IntegrityError(
                    "archive dependency must point to an older publication",
                )
            elif identity in expected and any(
                row[key] != expected[identity][key]
                for key in (
                    "session_id",
                    "scope_kind",
                    "tenant_key",
                    "source_key",
                    "agent_key",
                    "relative_path",
                    "byte_length",
                )
            ):
                raise IntegrityError(
                    "archive source publication locator changed",
                )
        return result


def _validate_row(row, message, request):
    if row is None or (
        row["session_id"] != request.session_id
        or row["layer"] == "archive"
        or row["deleted_at"] is not None
        or not row["current_visible"]
    ):
        raise IntegrityError(
            "archive source is not a visible Session occurrence",
        )
    expected = {
        "source_msg_key": (
            identity_bytes(message.source_msg_key, 1024)
            if message.source_msg_key is not None
            else None
        ),
        "source_occurrence_no": message.source_occurrence_no,
        "role_key": identity_bytes(message.role, 32),
        "kind_key": identity_bytes(message.kind, 64),
        "turn_id": message.turn_id,
        "origin_chat_id": message.origin_chat_id,
        "context_epoch": message.context_epoch,
        "task_run_key": (
            identity_bytes(message.task_run_key, 256)
            if message.task_run_key is not None
            else None
        ),
    }
    expected["message_timestamp"] = (
        utc_datetime(message.message_timestamp)
        if message.message_timestamp is not None
        else None
    )
    if any(row[key] != value for key, value in expected.items()):
        raise IntegrityError(
            "archive source typed identity differs from occurrence",
        )


async def snapshot_sources(service, request, plan):
    messages = [
        item for item in plan.messages if item.source_message_id is not None
    ]
    snapshot = SourceMessages()
    if not messages:
        return snapshot
    ids = [item.source_message_id for item in messages]
    if len(ids) != len(set(ids)):
        raise IntegrityError("archive source occurrences cannot repeat")
    retentions = RetentionStore(service.db, request.scope)
    try:
        async with service.db.transaction():
            for message in messages:
                row = await service.db.fetch_one(
                    "SELECT * FROM swe_conversation_messages WHERE id=%s",
                    (message.source_message_id,),
                )
                _validate_row(row, message, request)
                snapshot.rows[row["id"]] = row
            orders = [
                snapshot.rows[identity]["session_order"] for identity in ids
            ]
            if orders != sorted(orders):
                raise IntegrityError(
                    "archive sources must retain Session order",
                )
            snapshot.batch = await retentions.pin(
                snapshot.publication_ids,
                kind="reader",
            )
        publications = {row["id"]: row for row in snapshot.batch.publications}
        for message in messages:
            row = snapshot.rows[message.source_message_id]
            publication = publications[row["publication_id"]]
            if publication["session_id"] != request.session_id:
                raise IntegrityError(
                    "archive source publication Session mismatch",
                )
            raw = await snapshot.batch.read(
                service.bundles.read_extent,
                _text(publication["relative_path"]),
                row["byte_offset"],
                row["byte_length"],
                expected_length=publication["byte_length"],
            )
            snapshot.payloads[message.block_name] = load_metadata_json(raw)
        return snapshot
    except BaseException:
        await snapshot.finish(service, request.scope)
        raise
