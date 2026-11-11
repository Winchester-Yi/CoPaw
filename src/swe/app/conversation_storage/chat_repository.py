# -*- coding: utf-8 -*-
"""Scoped SQL Chat metadata with pinned immutable content reads."""

from contextlib import aclosing
from datetime import timezone
import asyncio
import json
import secrets

from ..channels.schema import DEFAULT_CHANNEL
from ..runner.expert_binding import (
    EXPERT_BINDING_META_KEY,
    ChatExpertBinding,
    ExpertBindingConflict,
    ExpertBindingModeConflict,
    read_expert_binding,
)
from ..runner.models import ChatPage, ChatSpec, ChatsFile
from ..runner.chat_summary import (
    ChatSummary,
    ChatSummaryPage,
    display_metadata,
    validate_summary_page,
)
from ..runner.repo.base import BaseChatRepository
from .collection_queries import (
    CollectionQuery,
    query_collection,
    update_collection,
)
from .errors import ConflictError, IntegrityError, UnknownCommit
from .identity import (
    SessionIdentity,
    WorkspaceScope,
    chat_key_bytes,
    identity_bytes,
)
from .retention import MAX_PIN_BATCH, RetentionStore
from .reference_queries import ReferenceQuery, iter_reference_rows
from .chat_mutations import (
    ChatMetadataInstall,
    install_chat_metadata,
    split_metadata,
    validate_light_metadata,
)
from .bundle import BundleBlock, new_operation_key
from .publications import CommitPlan, PublicationRequest, PublicationStore

TABLE = "swe_conversation_chats"
SCOPE_WHERE = "c.service_unit_key=%s AND c.tenant_key=%s AND c.source_key=%s AND c.agent_key=%s"
CHAT_FROM = (
    "FROM swe_conversation_chats c "
    "JOIN swe_conversation_sessions s ON s.id=c.session_id "
    "AND s.service_unit_key=c.service_unit_key AND s.tenant_key=c.tenant_key AND s.source_key=c.source_key "
    "AND s.agent_key=c.agent_key AND s.user_key=c.user_key "
    "AND s.channel_key=c.channel_key "
)
PROJECTED_STATUS = (
    "CASE WHEN t.chat_id=c.id AND t.state='stopping' THEN 'stopping' "
    "WHEN t.chat_id=c.id AND t.state IN "
    "('reserved','admitted','running','settling') THEN 'running' "
    "ELSE 'idle' END AS projected_status "
)
STATUS_FROM = (
    CHAT_FROM + "LEFT JOIN swe_conversation_turns t ON t.id=s.active_turn_id "
    "AND t.session_id=s.id WHERE "
)
SELECT_CHAT = (
    "SELECT c.*,s.external_session_key," + PROJECTED_STATUS + STATUS_FROM
)
SELECT_SUMMARY = (
    "SELECT c.external_chat_key,c.external_session_key,c.user_key,"
    "c.channel_key,c.title,c.created_at,c.updated_at,c.metadata_text,"
    "c.expert_id,c.expert_binding_revision," + PROJECTED_STATUS + STATUS_FROM
)


def _text(value):
    return (
        bytes(value).decode("utf-8")
        if isinstance(value, (bytes, bytearray))
        else value
    )


def _utc(value):
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _sql_time(value):
    return _utc(value).replace(tzinfo=None)


def _light_meta(text):
    try:
        return validate_light_metadata(text)
    except (ValueError, TypeError) as error:
        raise IntegrityError("Chat metadata is invalid") from error


class MySQLChatRepository(BaseChatRepository):
    """All filtering and pagination are performed against one trusted scope."""

    deferred_resource_cleanup = True

    def __init__(self, database, scope: WorkspaceScope, bundles):
        self.db = database
        self.scope = scope
        self.bundles = bundles
        self.retentions = RetentionStore(database, scope)
        self.path = "<mysql-conversation>"
        self.publications = PublicationStore(
            database,
            bundles,
            service_unit_id=scope.service_unit_id,
        )
        self.owner_key = secrets.token_hex(32)

    def _where(self, *, user_id=None, channel=None):
        clauses = [SCOPE_WHERE, "c.deleted_at IS NULL"]
        params = list(self.scope.sql_values)
        if user_id is not None:
            clauses.append("c.user_key=%s")
            params.append(identity_bytes(user_id, 256))
        if channel is not None:
            clauses.append("c.channel_key=%s")
            params.append(identity_bytes(channel, 64))
        return " AND ".join(clauses), params

    def _spec(self, row, content):
        meta = {**content, **_light_meta(row["metadata_text"])}
        if EXPERT_BINDING_META_KEY in meta:
            raise IntegrityError("Chat binding must come from typed metadata")
        if row["expert_binding_revision"] or row["expert_id"] is not None:
            meta[EXPERT_BINDING_META_KEY] = {
                "revision": row["expert_binding_revision"],
                "expert_id": _text(row["expert_id"]),
            }
        return ChatSpec(
            id=_text(row["external_chat_key"]),
            session_id=_text(row["external_session_key"]),
            user_id=_text(row["user_key"]),
            channel=_text(row["channel_key"]),
            name=row["title"],
            meta=meta,
            status=row.get("projected_status", row["status"]),
            created_at=_utc(row["created_at"]),
            updated_at=_utc(row["updated_at"]),
        )

    async def _hydrate(self, rows, batches=()):
        publications = {
            row["id"]: (row, batch)
            for batch in batches
            for row in batch.publications
        }
        result = []
        for row in rows:
            content = {}
            publication_id = row["content_meta_publication_id"]
            if publication_id is not None:
                publication, batch = publications[publication_id]
                payload = await batch.read(
                    self.bundles.read_extent,
                    _text(publication["relative_path"]),
                    row["content_meta_offset"],
                    row["content_meta_length"],
                    expected_length=publication["byte_length"],
                )
                try:
                    content = json.loads(payload)
                    if not isinstance(content, dict):
                        raise ValueError("content metadata is not an object")
                except (ValueError, TypeError) as error:
                    raise IntegrityError(
                        "Chat content metadata is invalid",
                    ) from error
            result.append(self._spec(row, content))
        return result

    async def _rows(self, where, params, suffix=""):
        async with self.db.transaction():
            rows = await self.db.fetch_all(
                SELECT_CHAT + where + suffix,
                tuple(params),
            )
            ids = sorted(
                {
                    row["content_meta_publication_id"]
                    for row in rows
                    if row["content_meta_publication_id"] is not None
                },
            )
            batches = []
            for start in range(0, len(ids), MAX_PIN_BATCH):
                stop = start + MAX_PIN_BATCH
                batches.append(
                    await self.retentions.pin(
                        ids[start:stop],
                        kind="reader",
                    ),
                )
        try:
            result = await self._hydrate(rows, batches)
            await self._revalidate(rows)
            return result
        finally:
            await self._finish_batches(batches)

    async def _finish_batches(self, batches):
        failure = None
        for batch in batches:
            try:
                await self.retentions.finish_reader(batch)
            except (Exception, asyncio.CancelledError) as error:
                failure = error
        if failure is not None:
            raise failure

    async def _revalidate(self, rows):
        if not rows:
            return
        fields = (
            "session_id",
            "metadata_revision",
            "expert_binding_revision",
            "content_meta_publication_id",
            "content_meta_offset",
            "content_meta_length",
            "user_key",
            "channel_key",
        )
        expected = {row["id"]: row for row in rows}
        async with aclosing(
            iter_reference_rows(
                self.db,
                ReferenceQuery.CHAT_CHECK,
                expected,
                self.scope.sql_values[0],
            ),
        ) as batches:
            async for ids, current in batches:
                for identity in ids:
                    row = current.get(identity)
                    if (
                        row is None
                        or row["deleted_at"] is not None
                        or tuple(
                            row[key]
                            for key in (
                                "service_unit_key",
                                "tenant_key",
                                "source_key",
                                "agent_key",
                            )
                        )
                        != self.scope.sql_values
                        or any(
                            row[field] != expected[identity][field]
                            for field in fields
                        )
                    ):
                        raise ConflictError(
                            "Chat changed while metadata was read",
                        )

    async def load(self):
        return ChatsFile(chats=await self.list_chats())

    async def save(self, chats_file):
        raise NotImplementedError(
            "SQL Chat registry replacement is unsupported; "
            "use individual operations",
        )

    async def list_chats(self):
        return await self.filter_chats()

    async def filter_chats(self, user_id=None, channel=None):
        where, params = self._where(user_id=user_id, channel=channel)
        return await self._rows(
            where,
            params,
            " ORDER BY c.registry_order ASC,c.id ASC",
        )

    async def get_chat(self, chat_id):
        where, params = self._where()
        rows = await self._rows(
            where + " AND c.external_chat_key=%s",
            [*params, chat_key_bytes(chat_id)],
            " LIMIT 1",
        )
        return rows[0] if rows else None

    async def get_chat_by_id(
        self,
        session_id,
        user_id,
        channel=DEFAULT_CHANNEL,
    ):
        where, params = self._where(user_id=user_id, channel=channel)
        rows = await self._rows(
            where + " AND s.external_session_key=%s",
            [*params, identity_bytes(session_id, 1024)],
            " ORDER BY c.registry_order ASC,c.id ASC LIMIT 1",
        )
        return rows[0] if rows else None

    async def latest_chat_by_session(self, session_id, channel, user_id=None):
        where, params = self._where(user_id=user_id, channel=channel)
        rows = await self._rows(
            where + " AND s.external_session_key=%s",
            [*params, identity_bytes(session_id, 1024)],
            " ORDER BY c.updated_at DESC,c.registry_order ASC LIMIT 1",
        )
        return rows[0] if rows else None

    async def get_chat_id_by_session(self, session_id, channel):
        chat = await self.latest_chat_by_session(session_id, channel)
        return chat.id if chat else None

    async def _count(self, where, params):
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS total " + CHAT_FROM + "WHERE " + where,
            tuple(params),
        )
        return int(row["total"])

    async def list_summaries(
        self,
        *,
        page_size=100,
        cursor=None,
        user_id=None,
        channel=None,
        session_id=None,
    ):
        """Read one statement snapshot without counts, body pins or NAS IO."""
        validate_summary_page(page_size, cursor)
        where, params = self._where(user_id=user_id, channel=channel)
        if session_id is not None:
            where += " AND c.external_session_key=%s"
            params.append(identity_bytes(session_id, 1024))
        if cursor:
            version, (stamp, key) = self._decode_cursor(cursor)
            if version != "v2":
                raise ValueError("Chat summaries require a v2 cursor")
            where += (
                " AND (c.updated_at<%s OR (c.updated_at=%s "
                "AND c.external_chat_key<%s))"
            )
            params.extend(
                [_sql_time(stamp), _sql_time(stamp), chat_key_bytes(key)],
            )
        rows = await self.db.fetch_all(
            SELECT_SUMMARY
            + where
            + " ORDER BY c.updated_at DESC,c.external_chat_key DESC LIMIT %s",
            (*params, page_size + 1),
        )
        has_more = len(rows) > page_size
        items = [self._summary(row) for row in rows[:page_size]]
        return ChatSummaryPage(
            items=items,
            page_size=page_size,
            has_more=has_more,
            next_cursor=(
                self._encode_cursor(self._recency_sort_key(items[-1]))
                if has_more
                else None
            ),
        )

    def _summary(self, row):
        return ChatSummary(
            id=_text(row["external_chat_key"]),
            session_id=_text(row["external_session_key"]),
            user_id=_text(row["user_key"]),
            channel=_text(row["channel_key"]),
            name=row["title"],
            created_at=_utc(row["created_at"]),
            updated_at=_utc(row["updated_at"]),
            status=row["projected_status"],
            display_metadata=display_metadata(
                _light_meta(row["metadata_text"]),
            ),
            expert_binding=(
                {
                    "revision": row["expert_binding_revision"],
                    "expert_id": _text(row["expert_id"]),
                }
                if row["expert_binding_revision"]
                or row["expert_id"] is not None
                else None
            ),
        )

    async def paginate_chats(
        self,
        *,
        page,
        page_size,
        user_id=None,
        channel=None,
    ):
        if page < 1 or page_size < 1:
            raise ValueError(
                "Chat pagination requires positive page and page_size",
            )
        where, params = self._where(user_id=user_id, channel=channel)
        total = await self._count(where, params)
        offset = (page - 1) * page_size
        items = await self._rows(
            where,
            [*params, page_size, offset],
            " ORDER BY c.updated_at DESC,c.external_chat_key DESC LIMIT %s "
            "OFFSET %s",
        )
        return ChatPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_more=offset + len(items) < total,
        )

    async def paginate_chats_cursor(
        self,
        *,
        page_size,
        cursor=None,
        user_id=None,
        channel=None,
    ):
        if page_size < 1:
            raise ValueError("Chat pagination requires positive page_size")
        where, params = self._where(user_id=user_id, channel=channel)
        total = await self._count(where, params)
        version, boundary = (
            self._decode_cursor(cursor) if cursor else ("v2", None)
        )
        column = "created_at" if version == "v1" else "updated_at"
        if boundary:
            stamp, key = boundary
            where += (
                f" AND (c.{column}<%s OR (c.{column}=%s "
                "AND c.external_chat_key<%s))"
            )
            params.extend(
                [_sql_time(stamp), _sql_time(stamp), chat_key_bytes(key)],
            )
        items = await self._rows(
            where,
            [*params, page_size + 1],
            f" ORDER BY c.{column} DESC,c.external_chat_key DESC LIMIT %s",
        )
        has_more = len(items) > page_size
        items = items[:page_size]
        sort_key = (
            self._creation_sort_key
            if version == "v1"
            else self._recency_sort_key
        )
        next_cursor = (
            self._encode_cursor(sort_key(items[-1]), version=version)
            if has_more and items
            else None
        )
        return ChatPage(
            items=items,
            total=total,
            page=1,
            page_size=page_size,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    async def _locked_chat(self, chat_id):
        params = (*self.scope.sql_values, chat_key_bytes(chat_id))
        row = await self.db.fetch_one(
            SELECT_CHAT + SCOPE_WHERE + " AND c.external_chat_key=%s",
            params,
        )
        if row is None:
            raise KeyError(chat_id)
        session = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_sessions WHERE id=%s AND service_unit_key=%s FOR UPDATE",
            (row["session_id"], self.scope.sql_values[0]),
        )
        if session is None or WorkspaceScope.from_row(session) != self.scope:
            raise IntegrityError("Chat Session service scope mismatch")
        locked = await self.db.fetch_one(
            f"SELECT * FROM {TABLE} c WHERE "
            + SCOPE_WHERE
            + " AND c.external_chat_key=%s FOR UPDATE",
            params,
        )
        if locked is None or locked["deleted_at"] is not None:
            raise KeyError(chat_id)
        if locked["session_id"] != session["id"]:
            raise ConflictError("Chat Session association changed")
        return session, locked

    async def _binding(
        self,
        chat_id,
        expected_revision,
        expert_id,
        *,
        release=False,
    ):
        if expert_id is not None:
            identity_bytes(expert_id, 256)
        async with self.db.transaction():
            await self.retentions.assert_writes_allowed()
            session, row = await self._locked_chat(chat_id)
            if session["active_turn_id"] is not None:
                raise ConflictError("Session is busy")
            binding = ChatExpertBinding(
                revision=row["expert_binding_revision"],
                expert_id=_text(row["expert_id"]),
            )
            if not release and binding.revision != expected_revision:
                raise ExpertBindingConflict(binding)
            if release and binding.expert_id is None:
                return binding
            if expert_id is not None and any(
                _light_meta(row["metadata_text"]).get(key) is True
                for key in ("plan_mode_enabled", "goal_mode_enabled")
            ):
                raise ExpertBindingModeConflict(
                    "Expert main execution requires ordinary Chat",
                )
            updated = ChatExpertBinding(
                revision=binding.revision + 1,
                expert_id=expert_id,
            )
            await self.db.execute(
                "UPDATE swe_conversation_chats SET "
                "expert_id=%s,expert_binding_revision=%s,"
                "updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND service_unit_key=%s "
                "AND deleted_at IS NULL",
                (
                    (
                        identity_bytes(expert_id, 256)
                        if expert_id is not None
                        else None
                    ),
                    updated.revision,
                    row["id"],
                    self.scope.sql_values[0],
                ),
            )
            return updated

    async def compare_and_set_expert_binding(
        self,
        chat_id,
        expected_revision,
        expert_id,
    ):
        return await self._binding(chat_id, expected_revision, expert_id)

    async def release_current_expert_binding(self, chat_id):
        return await self._binding(chat_id, None, None, release=True)

    async def update_title(self, chat_id, title, *, expected_revision):
        async with self.db.transaction():
            await self.retentions.assert_writes_allowed()
            try:
                _, row = await self._locked_chat(chat_id)
            except KeyError:
                return False
            if row["metadata_revision"] != expected_revision:
                raise ConflictError("Chat metadata revision changed")
            changed = await self.db.execute(
                "UPDATE swe_conversation_chats SET "
                "title=%s,metadata_revision=metadata_revision+1,"
                "updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND service_unit_key=%s "
                "AND deleted_at IS NULL AND metadata_revision=%s",
                (
                    title,
                    row["id"],
                    self.scope.sql_values[0],
                    expected_revision,
                ),
            )
            return bool(changed)

    async def delete_chats(self, chat_ids):
        keys = sorted({chat_key_bytes(value) for value in chat_ids})
        if not keys:
            return False
        async with self.db.transaction():
            await self.retentions.assert_writes_allowed()
            rows = await query_collection(
                self.db,
                CollectionQuery.DELETE_CANDIDATES,
                keys,
                self.scope.sql_values,
            )
            rows.sort(key=lambda row: row["id"])
            if not rows:
                return False
            session_ids = {row["session_id"] for row in rows}
            await query_collection(
                self.db,
                CollectionQuery.SESSION_LOCK,
                session_ids,
                (self.scope.sql_values[0],),
            )
            ids = [row["id"] for row in rows]
            current = await query_collection(
                self.db,
                CollectionQuery.CHAT_LOCK,
                ids,
                (self.scope.sql_values[0],),
            )
            by_id = {row["id"]: row for row in current}
            if any(
                row["id"] not in by_id
                or by_id[row["id"]]["session_id"] != row["session_id"]
                for row in rows
            ):
                raise ConflictError("Chat Session association changed")
            changed = await update_collection(
                self.db,
                CollectionQuery.DELETE_CHATS,
                ids,
                self.scope.sql_values,
            )
            return bool(changed)

    async def _session_for(self, spec):
        identity = SessionIdentity(
            self.scope,
            spec.user_id,
            spec.channel,
            spec.session_id,
        )
        async with self.db.transaction():
            await self.retentions.assert_writes_allowed()
            await self.db.insert(
                "INSERT INTO swe_conversation_sessions "
                "(service_unit_key,tenant_key,source_key,agent_key,user_key,channel_key,"
                "external_session_key,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,"
                "UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) "
                "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",
                identity.sql_values,
            )
            return await self.db.fetch_one(
                "SELECT * FROM swe_conversation_sessions WHERE "
                "service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s AND "
                "user_key=%s AND channel_key=%s AND external_session_key=%s",
                identity.sql_values,
            )

    async def _raw_chat(self, chat_id):
        return await self.db.fetch_one(
            SELECT_CHAT + SCOPE_WHERE + " AND c.external_chat_key=%s",
            (*self.scope.sql_values, chat_key_bytes(chat_id)),
        )

    async def _publish_metadata(
        self,
        spec,
        session,
        row,
        *,
        create_if_absent=False,
    ):
        source_session = row["session_id"] if row else session["id"]
        light, content = split_metadata(spec.meta)
        binding = (
            read_expert_binding(spec)
            if row is None
            else ChatExpertBinding(
                revision=row["expert_binding_revision"],
                expert_id=_text(row["expert_id"]),
            )
        )
        fields = {
            "external_chat_key": spec.id,
            "user_id": spec.user_id,
            "channel": spec.channel,
            "title": spec.name,
            "metadata_text": json.dumps(
                light,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "created_at": _utc(spec.created_at).isoformat(),
            "updated_at": _utc(spec.updated_at).isoformat(),
            "content_meta_block": "content_meta" if content else None,
            "expected_metadata_revision": (
                row["metadata_revision"] if row else None
            ),
            "expert_id": binding.expert_id,
            "expert_binding_revision": binding.revision,
            "status": "idle",
            "create_if_absent": create_if_absent,
        }
        if row is not None and row["session_id"] != session["id"]:
            fields["target_session_id"] = session["id"]
        mutation = ChatMetadataInstall(**fields)
        request = PublicationRequest(
            new_operation_key(),
            self.scope,
            "chat_metadata",
            self.owner_key,
            session["generation"],
            session_id=source_session,
            chat_id=row["id"] if row else None,
        )
        if not content:
            return await self._write_sql_metadata(request, mutation)
        await self._ensure_no_pending_metadata(source_session)
        plan = CommitPlan(chat_metadata=mutation)
        await self.publications.reserve(request, plan)
        await self.publications.write_bundle(
            request.operation_key,
            [
                BundleBlock(
                    "content_meta",
                    "content_meta",
                    json.dumps(
                        content,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8"),
                ),
            ],
        )
        try:
            return await self.publications.commit(request.operation_key)
        except ConflictError:
            await self.publications.abort(request, owner_stopped=True)
            raise

    async def _write_sql_metadata(self, request, mutation):
        """Install body-free metadata under the same locks as publication."""
        async with self.db.transaction():
            await self.retentions.assert_writes_allowed()
            associations, target = (
                await self.publications._lock_metadata_associations(
                    request,
                    mutation,
                )
            )
            self.publications._validate_associations(request, *associations)
            await self._ensure_no_pending_metadata(request.session_id)
            if target is not None:
                await self._ensure_no_pending_metadata(target["id"])
            chat_id = await install_chat_metadata(
                self.db,
                request,
                mutation,
                None,
                None,
                associations,
                target_session=target,
            )
            return {"chat_id": chat_id}

    async def _ensure_no_pending_metadata(self, session_id):
        rows = await self.db.fetch_all(
            "SELECT operation_key FROM swe_conversation_publications "
            "WHERE session_id=%s AND service_unit_key=%s AND tenant_key=%s AND source_key=%s "
            "AND agent_key=%s AND kind='chat_metadata' "
            "AND state='prepared' ORDER BY id LIMIT 1",
            (session_id, *self.scope.sql_values),
        )
        if rows:
            raise UnknownCommit(_text(rows[0]["operation_key"]))

    async def upsert_chat(self, spec):
        row = await self._raw_chat(spec.id)
        if row is not None:
            if row["deleted_at"] is not None:
                raise ConflictError("deleted Chat cannot be recreated")
            await self.update_chat_if_exists(spec)
            return
        session = await self._session_for(spec)
        await self._publish_metadata(spec, session, None)

    async def update_chat_if_unchanged(self, spec, expected_chat):
        return await self.update_chat_if_exists(
            spec,
            expected_chat=expected_chat,
        )

    async def update_chat_if_exists(self, spec, *, expected_chat=None):
        row = await self._raw_chat(spec.id)
        if row is None or row["deleted_at"] is not None:
            return None
        if expected_chat is not None:
            current = await self.get_chat(spec.id)
            if current is None:
                return None
            if current != expected_chat:
                raise ConflictError("Chat changed since patch was read")
        _, content = split_metadata(spec.meta)
        if not content:
            session = await self._session_for(spec)
            await self._publish_metadata(spec, session, row)
            return await self.get_chat(spec.id)
        current = await self.get_chat(spec.id)
        if current is None:
            return None
        incoming_meta = {
            key: value
            for key, value in spec.meta.items()
            if key != EXPERT_BINDING_META_KEY
        }
        current_meta = {
            key: value
            for key, value in current.meta.items()
            if key != EXPERT_BINDING_META_KEY
        }
        if (spec.session_id, spec.user_id, spec.channel) == (
            current.session_id,
            current.user_id,
            current.channel,
        ) and incoming_meta == current_meta:
            if not await self.update_title(
                spec.id,
                spec.name,
                expected_revision=row["metadata_revision"],
            ):
                return None
            return await self.get_chat(spec.id)
        session = await self._session_for(spec)
        await self._publish_metadata(spec, session, row)
        return await self.get_chat(spec.id)

    async def create_chat_if_absent_by_session(self, spec):
        session = await self._session_for(spec)
        existing_key = await self._freeze_draft_choice(session["id"])
        if existing_key is not None:
            return await self._draft_result(existing_key, created=False)
        try:
            committed = await self._publish_metadata(
                spec,
                session,
                None,
                create_if_absent=True,
            )
        except ConflictError:
            existing_key = await self._freeze_draft_choice(session["id"])
            if existing_key is None:
                raise
            return await self._draft_result(existing_key, created=False)
        final = await self.db.fetch_one(
            f"SELECT external_chat_key FROM {TABLE} c WHERE "
            + SCOPE_WHERE
            + " AND c.id=%s",
            (*self.scope.sql_values, committed["chat_id"]),
        )
        if final is None:
            raise ConflictError("draft Chat was deleted before returning")
        key = _text(final["external_chat_key"])
        return await self._draft_result(key, created=key == spec.id)

    async def _draft_result(self, chat_key, *, created):
        chat = await self.get_chat(chat_key)
        if chat is None:
            raise ConflictError("draft Chat was deleted before returning")
        return chat, created

    async def _freeze_draft_choice(self, session_id):
        async with self.db.transaction():
            await self.retentions.assert_writes_allowed()
            session = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_sessions "
                "WHERE id=%s AND service_unit_key=%s FOR UPDATE",
                (session_id, self.scope.sql_values[0]),
            )
            if (
                tuple(
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
                raise IntegrityError("draft Session scope mismatch")
            if session["draft_chat_id"] is not None:
                row = await self.db.fetch_one(
                    f"SELECT * FROM {TABLE} WHERE id=%s AND service_unit_key=%s FOR UPDATE",
                    (session["draft_chat_id"], self.scope.sql_values[0]),
                )
                if (
                    row is None
                    or row["deleted_at"] is not None
                    or row["session_id"] != session_id
                ):
                    raise ConflictError(
                        "draft Chat identity is deleted or missing",
                    )
                if tuple(
                    row[key]
                    for key in (
                        "service_unit_key",
                        "tenant_key",
                        "source_key",
                        "agent_key",
                        "user_key",
                        "channel_key",
                    )
                ) != (
                    *self.scope.sql_values,
                    session["user_key"],
                    session["channel_key"],
                ):
                    raise IntegrityError("draft Chat identity scope mismatch")
                return _text(row["external_chat_key"])
            row = await self.db.fetch_one(
                f"SELECT * FROM {TABLE} WHERE session_id=%s "
                "AND deleted_at IS NULL ORDER BY registry_order,id "
                "LIMIT 1 FOR UPDATE",
                (session_id,),
            )
            if row is None:
                return None
            if tuple(
                row[key]
                for key in (
                    "service_unit_key",
                    "tenant_key",
                    "source_key",
                    "agent_key",
                    "user_key",
                    "channel_key",
                )
            ) != (
                *self.scope.sql_values,
                session["user_key"],
                session["channel_key"],
            ):
                raise IntegrityError("draft Chat identity scope mismatch")
            await self.db.execute(
                "UPDATE swe_conversation_sessions SET draft_chat_id=%s "
                "WHERE id=%s",
                (row["id"], session_id),
            )
            return _text(row["external_chat_key"])
