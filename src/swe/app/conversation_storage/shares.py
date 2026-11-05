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
from .collection_queries import CollectionQuery, query_collection
from .reference_queries import ReferenceQuery, fetch_reference_rows
from .errors import ConflictError, IntegrityError, StorageUnavailable
from .identity import (
    WorkspaceScope,
    identity_bytes,
    chat_key_bytes,
    service_unit_bytes,
)
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
    selection_proof: dict | None = None

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
        if self.selection_proof is not None:
            _validate_selection_proof(
                self.selection_proof,
                self.selected_question_keys,
            )

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


def _validate_selection_proof(proof, keys):
    if not isinstance(proof, dict) or set(proof) != {
        "snapshot",
        "questions",
    }:
        raise ValueError("invalid share selection proof")
    snapshot = proof["snapshot"]
    if (
        not isinstance(snapshot, dict)
        or set(snapshot)
        != {
            "session_id",
            "chat_id",
            "state_revision",
            "history_revision",
            "session_history_high",
            "archive_history_high",
            "epoch",
        }
        or any(
            type(value) is not int or value < 0 for value in snapshot.values()
        )
    ):
        raise ValueError("invalid share selection snapshot")
    questions = proof["questions"]
    if not isinstance(questions, dict) or set(questions) != set(keys):
        raise ValueError("invalid share selection questions")
    for item in questions.values():
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "message_id",
                "publication_id",
                "layer",
                "status",
            }
            or any(
                type(item[name]) is not int or item[name] <= 0
                for name in ("message_id", "publication_id")
            )
        ):
            raise ValueError("invalid share source occurrence")
        if item["status"] != "completed" or item["layer"] not in (
            "archive",
            "memory",
            "task",
            "error",
            "anchor",
        ):
            raise ValueError("share selection is not completed")


async def _lock_selection_proof(db, request, proof):
    snapshot = proof["snapshot"]
    if (
        request.base_state_revision != snapshot["state_revision"]
        or request.base_history_revision != snapshot["history_revision"]
    ):
        raise ConflictError("share selection revision is missing")
    current = await db.fetch_one(
        "SELECT s.id AS session_id,c.id AS chat_id,s.state_revision,c.history_revision,"
        "s.history_high AS session_history_high,c.history_high AS archive_history_high,c.epoch "
        "FROM swe_conversation_sessions s JOIN swe_conversation_chats c ON c.session_id=s.id "
        "WHERE s.id=%s AND c.id=%s AND s.service_unit_key=%s "
        "AND c.service_unit_key=s.service_unit_key AND c.deleted_at IS NULL FOR UPDATE",
        (request.session_id, request.chat_id, request.scope.sql_values[0]),
    )
    if current != snapshot:
        raise ConflictError("share selection history changed")


def _validate_proven_occurrence(row, request, key, proof, turn):
    item = proof["questions"][key.decode("utf-8")]
    expected = {
        "service_unit_key": request.scope.sql_values[0],
        "session_id": request.session_id,
        "source_msg_key": key,
        "role_key": b"user",
        "deleted_at": None,
        "publication_id": item["publication_id"],
        "layer": item["layer"],
    }
    if row is None or any(
        row[name] != value for name, value in expected.items()
    ):
        raise ConflictError("selected share source occurrence changed")
    if turn is not None and row["turn_id"] not in (None, turn["id"]):
        raise ConflictError("selected share source occurrence changed")
    if row["layer"] == "archive":
        valid = (
            row["archive_chat_id"] == request.chat_id
            and row["archive_order"]
            <= proof["snapshot"]["archive_history_high"]
        )
    else:
        valid = (
            row["current_visible"]
            and row["session_order"]
            <= proof["snapshot"]["session_history_high"]
            and (
                row["layer"] != "anchor"
                or row["origin_chat_id"] in (None, request.chat_id)
            )
        )
    if not valid:
        raise ConflictError("selected share question is no longer visible")


async def lock_share_selection(db, request, plan):
    if request.kind != "share":
        return
    keys = tuple(
        dict.fromkeys(
            identity_bytes(key, 256) for key in plan.selected_question_keys
        ),
    )
    turns = await query_collection(
        db,
        CollectionQuery.SHARE_TURNS,
        keys,
        (request.scope.sql_values[0], request.session_id),
    )
    by_key = {row["question_msg_key"]: row for row in turns}
    proof = plan.selection_proof
    if proof is not None:
        await _lock_selection_proof(db, request, proof)
        proven = await fetch_reference_rows(
            db,
            ReferenceQuery.MESSAGE_PROOF,
            [item["message_id"] for item in proof["questions"].values()],
            request.scope.sql_values[0],
        )
    elif len(by_key) != len(keys):
        raise ConflictError("selected share question has no durable Turn")
    for key in keys:
        turn = by_key.get(key)
        if turn is not None and (
            turn["chat_id"] != request.chat_id
            or turn["state"] != "succeeded"
            or turn["history_status"] != "completed"
        ):
            raise ConflictError("selected share Turn is not completed")
        if proof is not None:
            item = proof["questions"][key.decode("utf-8")]
            _validate_proven_occurrence(
                proven.get(item["message_id"]),
                request,
                key,
                proof,
                turn,
            )
            continue
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
    if publication["service_unit_key"] != request.scope.sql_values[0]:
        raise IntegrityError("share publication service mismatch")
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
        service_unit_id=request.scope.service_unit_id,
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
    await ChatShareStore(
        db,
        service_unit_id=request.scope.service_unit_id,
    ).create(record)


class ShareStorage:
    """Token reads derive their namespace solely from publication metadata."""

    def __init__(
        self,
        database,
        bundles,
        *,
        scope=None,
        service_unit_id=None,
        retention_factory=RetentionStore,
    ):
        self.db = database
        self.bundles = bundles
        self.scope = scope
        if scope is not None:
            if (
                service_unit_id is not None
                and service_unit_id != scope.service_unit_id
            ):
                raise ValueError("share service scope mismatch")
            service_unit_id = scope.service_unit_id
        self.service_unit_key = service_unit_bytes(service_unit_id)
        self.service_unit_id = service_unit_id
        self.retention_factory = retention_factory
        self.store = ChatShareStore(database, service_unit_id=service_unit_id)
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

    async def selection_snapshot(self, chat_id):
        """Capture revisions before reading any Session or history payload."""
        row = await self.db.fetch_one(
            "SELECT s.id AS session_id,c.id AS chat_id,s.state_revision,c.history_revision,"
            "s.history_high AS session_history_high,c.history_high AS archive_history_high,c.epoch "
            "FROM swe_conversation_sessions s JOIN swe_conversation_chats c ON c.session_id=s.id "
            "WHERE c.service_unit_key=%s AND c.tenant_key=%s AND c.source_key=%s AND c.agent_key=%s "
            "AND c.external_chat_key=%s AND c.deleted_at IS NULL",
            (*self.scope.sql_values, chat_key_bytes(chat_id)),
        )
        if row is None:
            raise KeyError(chat_id)
        return row

    async def prove_selection(
        self,
        chat_id,
        snapshot,
        keys,
        messages,
        statuses,
    ):
        """Bind the legacy slice to real source occurrences and fixed revisions."""
        from ..runner.api import _slice_answer_turn

        keys = tuple(dict.fromkeys(keys))
        for key in keys:
            if (
                statuses.get(key) != "completed"
                or _slice_answer_turn(messages, msgid=key) is None
            ):
                raise ValueError("Only completed answer turns can be shared")
        questions = await self._selection_occurrences(chat_id, snapshot, keys)
        if await self.selection_snapshot(chat_id) != snapshot:
            raise ConflictError("share history changed while reading")
        proof = {"snapshot": snapshot, "questions": questions}
        _validate_selection_proof(proof, keys)
        return proof

    async def _selection_occurrences(self, chat_id, snapshot, keys):
        from ..runner import api
        from .history import HistorySnapshot, HistoryStore

        history = HistoryStore(
            self.db,
            self.scope,
            self.bundles,
            cursor_secret=b"share-selection",
        )
        encoded = tuple(identity_bytes(key, 256) for key in keys)
        if not encoded:
            return {}
        async with self.db.transaction():
            chat = await history._chat(chat_id)
            rows = await query_collection(
                self.db,
                CollectionQuery.SHARE_OCCURRENCES,
                encoded,
                (
                    self.scope.sql_values[0],
                    chat["id"],
                    snapshot["archive_history_high"],
                    chat["session_id"],
                    chat["id"],
                    snapshot["session_history_high"],
                ),
            )
            # SQL batches have disjoint source keys; restore the original global
            # MySQL ascending order (NULL sorts before numeric values).
            rows.sort(
                key=lambda row: (
                    row["archive_order"] is not None,
                    row["archive_order"] or 0,
                    row["session_order"] is not None,
                    row["session_order"] or 0,
                    row["id"],
                ),
            )
            batches = await history._pins(chat, rows, [])
            view = HistorySnapshot(chat, rows, [], batches)
        try:
            payloads = await history._payloads(view)
            entries = []
            primary = {
                row["source_msg_key"]
                for row in rows
                if row["layer"] != "anchor"
            }
            for row, payload in zip(rows, payloads):
                if (
                    row["layer"] == "anchor"
                    and row["source_msg_key"] in primary
                ):
                    continue
                converted = await history._convert(
                    [row],
                    [payload],
                    (
                        chat_id
                        if row["layer"] == "archive"
                        else chat["external_session_key"].decode("utf-8")
                    ),
                )
                if converted:
                    entries.append((row, converted[0]))
            archived = [
                item for item in entries if item[0]["layer"] == "archive"
            ]
            current = [
                item for item in entries if item[0]["layer"] != "archive"
            ]
            current.sort(
                key=lambda item: (
                    item[0]["layer"] == "anchor",
                    item[0]["session_order"],
                    item[0]["id"],
                ),
            )
            current.sort(key=lambda item: api._message_sort_key(item[1]))
            result = {}
            for row, message in [*archived, *current]:
                key = row["source_msg_key"].decode("utf-8")
                if key in result:
                    continue
                if message.role != "user":
                    raise ValueError("Selected answer turn was not found")
                result[key] = {
                    "message_id": row["id"],
                    "publication_id": row["publication_id"],
                    "layer": row["layer"],
                    "status": "completed",
                }
            return result
        finally:
            await history._finish(view)

    async def create_snapshot(
        self,
        *,
        chat_id,
        creator_id,
        tenant_id,
        payload,
        selected_question_keys=(),
        selection_proof=None,
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
            "SELECT * FROM swe_conversation_chats WHERE service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s AND external_chat_key=%s AND deleted_at IS NULL",
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
            selection_proof=selection_proof,
        )
        request = PublicationRequest(
            operation,
            self.scope,
            "share",
            self.owner_key,
            0,
            session_id=chat["session_id"],
            chat_id=chat["id"],
            base_state_revision=(
                selection_proof["snapshot"]["state_revision"]
                if selection_proof
                else None
            ),
            base_history_revision=(
                selection_proof["snapshot"]["history_revision"]
                if selection_proof
                else None
            ),
        )
        publications = PublicationStore(
            self.db,
            self.bundles,
            service_unit_id=self.service_unit_id,
        )
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
                    "SELECT * FROM swe_conversation_publications WHERE id=%s AND service_unit_key=%s",
                    (record.publication_id, self.service_unit_key),
                )
                if publication is None:
                    raise IntegrityError("share publication is missing")
                scope = WorkspaceScope.from_row(publication)
                if (
                    scope.service_unit_id != self.service_unit_id
                    or record.service_unit_id != self.service_unit_id
                ):
                    raise IntegrityError("share publication service mismatch")
                if scope.tenant_id != record.tenant_id:
                    raise IntegrityError("share publication tenant mismatch")
                retentions = self.retention_factory(
                    self.db,
                    scope,
                    scope_kind=publication["scope_kind"],
                )
                batch = await retentions.pin(
                    [record.publication_id],
                    kind="reader",
                )
            pinned = batch.publications[0]
            if any(
                publication[name] != value for name, value in pinned.items()
            ):
                raise IntegrityError("share publication changed while pinning")
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
                "service_unit_id",
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
