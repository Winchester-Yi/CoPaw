# -*- coding: utf-8 -*-
"""Permanent share locators published atomically with immutable NAS content."""

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import json
import secrets

from ..chat_sharing.models import ChatShareRecord, ChatShareSnapshot
from ..chat_sharing.store import ChatShareStore
from .bundle import BundleBlock, new_operation_key
from .chat_mutations import load_metadata_json, utc_datetime
from .errors import ConflictError, IntegrityError, StorageUnavailable
from .identity import WorkspaceScope, identity_bytes, chat_key_bytes
from .retention import RetentionStore


@dataclass(frozen=True)
class ShareInstall:
    token: str
    chat_id: str
    creator_id: str
    tenant_id: str
    snapshot_key: str
    created_at: str
    block_name: str = "share"
    access_count: int = 0
    last_access_at: str | None = None
    selected_question_keys: tuple[str, ...] = ()

    def __post_init__(self):
        for value, limit in (
            (self.token, 128),
            (self.chat_id, 128),
            (self.creator_id, 256),
            (self.tenant_id, 256),
            (self.block_name, 256),
        ):
            identity_bytes(value, limit)
        if (
            not isinstance(self.snapshot_key, str)
            or len(self.snapshot_key) > 512
        ):
            raise ValueError("share snapshot key exceeds legacy column limit")
        if not self.token or not self.block_name:
            raise ValueError("share token and block identity are required")
        if type(self.access_count) is not int or self.access_count < 0:
            raise ValueError("share access count must be nonnegative")
        utc_datetime(self.created_at)
        if self.last_access_at is not None:
            utc_datetime(self.last_access_at)
        if not isinstance(self.selected_question_keys, tuple) or (
            len(self.selected_question_keys) > 4096
        ):
            raise ValueError(
                "selected share questions must be a bounded tuple",
            )
        for key in self.selected_question_keys:
            if not key:
                raise ValueError("selected share question key is required")
            identity_bytes(key, 256)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {
            item.name for item in fields(cls)
        }:
            raise ValueError("unknown share install field")
        try:
            value = dict(value)
            if "selected_question_keys" in value:
                value["selected_question_keys"] = tuple(
                    value["selected_question_keys"],
                )
            return cls(**value)
        except TypeError as error:
            raise ValueError("incomplete share install") from error


def validate_share_request(request, plan):
    if plan is None:
        if request.scope_kind == "legacy_share" or request.kind in (
            "share",
            "share_import",
        ):
            raise ValueError("share publication requires a share plan")
        return
    if request.kind not in ("share", "share_import"):
        raise ValueError("share plan requires a share publication kind")
    if (
        plan.tenant_id != request.scope.tenant_id
        or request.turn_id is not None
    ):
        raise ValueError("share scope or Turn association is invalid")
    if request.scope_kind == "legacy_share":
        if request.kind != "share_import":
            raise ValueError("legacy share requires historical import")
    elif request.session_id is None or request.chat_id is None:
        raise ValueError("workspace share requires real Session and Chat")
    elif request.kind == "share" and not plan.selected_question_keys:
        raise ValueError("new share requires selected source questions")


async def lock_share_selection(db, request, plan):
    if request.kind != "share":
        return
    keys = tuple(
        dict.fromkeys(
            identity_bytes(key, 256) for key in plan.selected_question_keys
        ),
    )
    marks = ",".join(["%s"] * len(keys))
    turns = await db.fetch_all(
        "SELECT * FROM swe_conversation_turns WHERE session_id=%s "
        "AND question_msg_key IN (" + marks + ") ORDER BY id FOR UPDATE",
        (request.session_id, *keys),
    )
    by_key = {row["question_msg_key"]: row for row in turns}
    if len(by_key) != len(keys):
        raise ConflictError("selected share question has no durable Turn")
    for key in keys:
        turn = by_key[key]
        if (
            turn["chat_id"] != request.chat_id
            or turn["state"] != "succeeded"
            or turn["history_status"] != "completed"
        ):
            raise ConflictError("selected share Turn is not completed")
        occurrences = await db.fetch_all(
            "SELECT m.* FROM swe_conversation_messages m WHERE "
            "m.session_id=%s AND m.current_visible=1 AND m.deleted_at IS NULL "
            "AND (m.layer IN ('memory','task','error') OR (m.layer='anchor' "
            "AND (m.origin_chat_id=%s OR m.origin_chat_id IS NULL))) "
            "AND m.source_msg_key=%s ORDER BY m.session_order,m.id FOR UPDATE",
            (request.session_id, request.chat_id, key),
        )
        if (
            not occurrences
            or occurrences[0]["role_key"] != b"user"
            or (occurrences[0]["turn_id"] not in (None, turn["id"]))
        ):
            raise ConflictError("selected share question is no longer visible")
        if occurrences[0].get("layer") == "anchor":
            archived = await db.fetch_all(
                "SELECT id FROM swe_conversation_messages WHERE archive_chat_id=%s "
                "AND source_msg_key=%s AND deleted_at IS NULL ORDER BY id FOR UPDATE",
                (request.chat_id, key),
            )
            if archived:
                raise ConflictError(
                    "selected share question is no longer visible",
                )


async def validate_share_payload(service, request, plan, index):
    if plan is None:
        return
    extent = index.blocks.get(plan.block_name)
    if extent is None or extent.kind != "share":
        raise IntegrityError("share snapshot payload block is missing")
    raw = await service._nas_io(
        request.operation_key,
        service.bundles.read_extent,
        index.relative_path,
        extent.offset,
        extent.length,
        expected_length=index.byte_length,
    )
    try:
        ChatShareSnapshot.model_validate(load_metadata_json(raw))
    except (ValueError, TypeError) as error:
        raise IntegrityError("share snapshot payload is invalid") from error


async def install_share(db, request, plan, publication, index):
    if plan.tenant_id != request.scope.tenant_id:
        raise IntegrityError("share tenant differs from publication scope")
    if request.scope_kind == "workspace" and (
        request.session_id is None or request.chat_id is None
    ):
        raise IntegrityError("new share requires real workspace Chat identity")
    if request.scope_kind == "legacy_share" and request.kind != "share_import":
        raise IntegrityError(
            "legacy share is only valid for historical import",
        )
    extent = index.blocks.get(plan.block_name)
    if extent is None or extent.kind != "share":
        raise IntegrityError("share snapshot payload block is missing")
    record = ChatShareRecord(
        token=plan.token,
        chat_id=plan.chat_id,
        creator_id=plan.creator_id,
        tenant_id=plan.tenant_id,
        snapshot_key=plan.snapshot_key,
        created_at=utc_datetime(plan.created_at),
        access_count=plan.access_count,
        last_access_at=(
            utc_datetime(plan.last_access_at) if plan.last_access_at else None
        ),
        publication_id=publication["id"],
        byte_offset=extent.offset,
        byte_length=extent.length,
    )
    await ChatShareStore(db).create(record)


class ShareStorage:
    """Token reads derive their namespace solely from publication metadata."""

    def __init__(self, database, bundles, *, scope=None):
        self.db = database
        self.bundles = bundles
        self.scope = scope
        self.store = ChatShareStore(database)
        self.owner_key = new_operation_key()

    @staticmethod
    def _validate_record_publication(record, publication):
        from .publications import CommitPlan

        plan = CommitPlan.parse(publication["result_metadata_text"]).share
        if plan is None:
            raise IntegrityError("share publication has no token identity")
        for key in (
            "token",
            "chat_id",
            "creator_id",
            "tenant_id",
            "snapshot_key",
        ):
            if getattr(plan, key) != getattr(record, key):
                raise IntegrityError(
                    "share token identity differs from publication",
                )
        created = record.created_at
        if created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        if created != utc_datetime(plan.created_at):
            raise IntegrityError(
                "share creation time differs from publication",
            )

    async def create_snapshot(
        self,
        *,
        chat_id,
        creator_id,
        tenant_id,
        payload,
        selected_question_keys=(),
    ):
        from .publications import (
            CommitPlan,
            PublicationRequest,
            PublicationStore,
        )

        if self.scope is None or tenant_id != self.scope.tenant_id:
            raise IntegrityError(
                "share creation requires trusted workspace scope",
            )
        validated = ChatShareSnapshot.model_validate(payload).model_dump(
            mode="json",
        )
        chat = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_chats WHERE tenant_key=%s AND source_key=%s AND agent_key=%s AND external_chat_key=%s AND deleted_at IS NULL",
            (*self.scope.sql_values, chat_key_bytes(chat_id)),
        )
        if chat is None:
            raise KeyError(chat_id)
        if chat["user_key"] != identity_bytes(creator_id, 256):
            raise ConflictError("share creator no longer owns Chat")
        operation = new_operation_key()
        token = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        plan = ShareInstall(
            token,
            chat_id,
            creator_id,
            tenant_id,
            f"{tenant_id}/chat_shares/{token}.json",
            created.isoformat(),
            selected_question_keys=tuple(selected_question_keys),
        )
        request = PublicationRequest(
            operation,
            self.scope,
            "share",
            self.owner_key,
            0,
            session_id=chat["session_id"],
            chat_id=chat["id"],
        )
        publications = PublicationStore(self.db, self.bundles)
        await publications.reserve(request, CommitPlan(share=plan))
        await publications.write_bundle(
            operation,
            [
                BundleBlock(
                    "share",
                    "share",
                    json.dumps(
                        validated,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8"),
                ),
            ],
        )
        await publications.commit(operation)
        return await self.store.get(token)

    async def read_snapshot(self, token, *, record_access):
        batch = None
        retentions = None
        missing_token = False
        try:
            async with self.db.transaction():
                record = await self.store.get(token)
                if record is None:
                    missing_token = True
                    raise KeyError("Share not found")
                if any(
                    value is None
                    for value in (
                        record.publication_id,
                        record.byte_offset,
                        record.byte_length,
                    )
                ):
                    raise IntegrityError("share locator is not migrated")
                publication = await self.db.fetch_one(
                    "SELECT * FROM swe_conversation_publications WHERE id=%s",
                    (record.publication_id,),
                )
                if publication is None:
                    raise IntegrityError("share publication is missing")
                scope = WorkspaceScope(
                    *(
                        value.decode("utf-8")
                        for value in (
                            publication["tenant_key"],
                            publication["source_key"],
                            publication["agent_key"],
                        )
                    ),
                )
                if scope.tenant_id != record.tenant_id:
                    raise IntegrityError("share publication tenant mismatch")
                retentions = RetentionStore(
                    self.db,
                    scope,
                    scope_kind=publication["scope_kind"],
                )
                batch = await retentions.pin(
                    [record.publication_id],
                    kind="reader",
                )
            publication = batch.publications[0]
            self._validate_record_publication(record, publication)
            if publication["kind"] not in ("share", "share_import"):
                raise IntegrityError(
                    "share locator references a non-share publication",
                )
            if publication["scope_kind"] == "legacy_share" and any(
                publication[key] is not None
                for key in ("session_id", "chat_id", "turn_id")
            ):
                raise IntegrityError(
                    "legacy share publication has workspace associations",
                )
            index = await batch.read(
                self.bundles.inspect,
                publication["relative_path"].decode("utf-8"),
                expected_length=publication["byte_length"],
            )
            if not any(
                block.kind == "share"
                and block.offset == record.byte_offset
                and block.length == record.byte_length
                for block in index.blocks.values()
            ):
                raise IntegrityError(
                    "share locator is not an exact snapshot block",
                )
            payload = await batch.read(
                self.bundles.read_extent,
                publication["relative_path"].decode("utf-8"),
                record.byte_offset,
                record.byte_length,
                expected_length=publication["byte_length"],
            )
            snapshot = load_metadata_json(payload)
            ChatShareSnapshot.model_validate(snapshot)
            await self._finish_snapshot_read(
                token,
                record,
                retentions,
                record_access,
            )
            return snapshot
        except KeyError as error:
            if missing_token:
                raise
            raise StorageUnavailable(
                "Chat share reference is invalid",
            ) from error
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            raise StorageUnavailable(
                "Chat share snapshot is unavailable",
            ) from error
        finally:
            if batch is not None:
                await retentions.finish_reader(batch)

    async def _finish_snapshot_read(
        self,
        token,
        record,
        retentions,
        record_access,
    ):
        async with self.db.transaction():
            if record_access:
                await retentions.assert_writes_allowed()
            current = await self.store.get(token, lock=True)
            stable = (
                "publication_id",
                "byte_offset",
                "byte_length",
                "tenant_id",
                "snapshot_key",
                "chat_id",
            )
            if current is None or any(
                getattr(current, key) != getattr(record, key) for key in stable
            ):
                raise ConflictError("share locator changed while reading")
            if record_access:
                await self.store.record_access(token)

    async def get_snapshot(self, token):
        return await self.read_snapshot(token, record_access=True)

    async def verify_snapshot(self, token):
        return await self.read_snapshot(token, record_access=False)
