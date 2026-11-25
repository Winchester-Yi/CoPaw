# -*- coding: utf-8 -*-
"""SQL Chat directory for the original NAS Session and archive stores."""

from types import SimpleNamespace
from datetime import datetime, timezone
import json
import logging

from pydantic import ValidationError

from ...database.prepared import PreparedInsert
from ..runner.expert_binding import (
    EXPERT_BINDING_META_KEY,
    ExpertBindingConflict,
    read_expert_binding,
)
from ..runner.chat_summary import display_metadata
from ..runner.models import MANUAL_CHAT_TITLE_META_KEY
from ..runner.session import session_filename
from .chat_mutations import (
    ChatMetadataInstall,
    install_chat_metadata,
    load_metadata_json,
)
from .metadata_catalog import (
    CHAT_FROM,
    SELECT_CHAT,
    SELECT_SUMMARY,
    PROJECTED_STATUS,
    STATUS_FROM,
    MetadataCatalog,
    _utc,
    _sql_time,
    _text,
)
from .errors import ConflictError, IntegrityError
from .identity import (
    SessionIdentity,
    WorkspaceScope,
    identity_bytes,
    chat_key_bytes,
)

logger = logging.getLogger(__name__)

WORKSPACES = "swe_conversation_metadata_workspaces"
SOURCES = "swe_conversation_metadata_sources"
SCOPE_PREDICATE = (
    "service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s"
)
SOURCE_PREDICATE = "service_unit_key=%s AND tenant_key=%s AND source_key=%s"
SESSION_PREDICATE = (
    SCOPE_PREDICATE
    + " AND user_key=%s AND channel_key=%s AND external_session_key=%s"
)
SESSION_INSERT = PreparedInsert(
    "INSERT INTO swe_conversation_sessions "
    "(service_unit_key,tenant_key,source_key,agent_key,user_key,channel_key,"
    "external_session_key,nas_filename,created_at,updated_at) VALUES ",
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    " ON DUPLICATE KEY UPDATE id=id",
)


def session_filename_key(identity):
    return identity_bytes(
        session_filename(identity.session_id, identity.user_id),
        255,
    )


def _catalog_select(query):
    return query.replace(
        PROJECTED_STATUS,
        "c.status AS projected_status ",
    ).replace(STATUS_FROM, CHAT_FROM + "WHERE ")


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def metadata_fields(meta):
    # Keep the summary contract independent of the full ChatSpec metadata.
    light = display_metadata(meta)
    extended = {
        key: value
        for key, value in meta.items()
        if key not in light and key != EXPERT_BINDING_META_KEY
    }
    payload = _json(extended)
    if len(payload.encode()) > 16_777_215:
        raise ValueError("Extended Chat metadata exceeds the SQL column size")
    return _json(light), payload


def metadata_install(
    spec,
    meta,
    *,
    revision=None,
    create_if_absent=False,
    target_session_id=None,
):
    light, extended = metadata_fields(meta)
    binding = read_expert_binding(spec) if revision is None else None
    mutation = ChatMetadataInstall(
        external_chat_key=spec.id,
        user_id=spec.user_id,
        channel=spec.channel,
        title=spec.name,
        metadata_text=light,
        created_at=_utc(spec.created_at).isoformat(),
        updated_at=_utc(spec.updated_at).isoformat(),
        expected_metadata_revision=revision,
        expert_id=binding.expert_id if binding is not None else None,
        expert_binding_revision=(
            binding.revision if binding is not None else 0
        ),
        create_if_absent=create_if_absent,
        target_session_id=target_session_id,
    )
    return mutation, extended


class MySQLMetadataRepository(MetadataCatalog):
    """Reuse SQL directory operations with a workspace row as the write lock."""

    deferred_resource_cleanup = False
    preserve_failed_scenario_resources = True
    select_chat = _catalog_select(SELECT_CHAT)
    select_summary = _catalog_select(SELECT_SUMMARY)

    def __init__(self, database, scope, *, importing=False):
        self.db = database
        self.scope = scope
        self.importing = importing
        self.path = "<mysql-metadata>"

    async def _assert_writes_allowed(self):
        row = await self.db.fetch_one(
            f"SELECT workspace_root FROM {WORKSPACES} WHERE "
            + SCOPE_PREDICATE
            + " FOR UPDATE",
            self.scope.sql_values,
        )
        if row is None:
            raise IntegrityError("Chat workspace is not registered")
        if not self.importing:
            return
        source = await self.db.fetch_one(
            f"SELECT published FROM {SOURCES} WHERE " + SOURCE_PREDICATE,
            self.scope.sql_values[:3],
        )
        if source is None or source["published"]:
            raise ConflictError("Chat workspace publication state changed")

    def _spec(self, row, content=None):
        try:
            extended = load_metadata_json(
                row["extended_metadata_text"] or "{}",
            )
            if not isinstance(extended, dict):
                raise ValueError("Chat metadata must be an object")
        except (ValueError, TypeError) as error:
            raise IntegrityError(
                "Extended Chat metadata is invalid",
            ) from error
        return super()._spec(row, extended)

    async def _rows(self, where, params, suffix=""):
        rows = await self.db.fetch_all(
            self.select_chat + where + suffix,
            tuple(params),
        )
        return [self._spec(row) for row in rows]

    async def list_recovery_chats_cursor(self, *, page_size, cursor=None):
        """Advance using raw SQL ordering even when one Chat is malformed."""
        if page_size != 1:
            raise ValueError("recovery page size must be one")
        where, params = self._where()
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
        rows = await self.db.fetch_all(
            self.select_chat
            + where
            + f" ORDER BY c.{column} DESC,c.external_chat_key DESC LIMIT %s",
            (*params, 2),
        )
        if not rows:
            return SimpleNamespace(items=[], next_cursor=None)
        row = rows[0]
        # A malformed ordering identity cannot be skipped safely.
        key = _text(row["external_chat_key"])
        chat_key_bytes(key)
        next_cursor = (
            self._encode_cursor((_utc(row[column]), key), version=version)
            if len(rows) > 1
            else None
        )
        try:
            items = [self._spec(row)]
        except (
            IntegrityError,
            ValidationError,
            ValueError,
            TypeError,
            UnicodeError,
            KeyError,
        ):
            logger.warning(
                "Evidence recovery skipped invalid Chat directory entry",
            )
            items = []
        return SimpleNamespace(items=items, next_cursor=next_cursor)

    async def get_recovery_chat(self, chat_id):
        try:
            return await self.get_chat(chat_id)
        except (
            IntegrityError,
            ValidationError,
            ValueError,
            TypeError,
            UnicodeError,
            KeyError,
        ):
            logger.warning(
                "Evidence recovery skipped invalid Chat directory entry",
            )
            return None

    async def _session_for(self, spec):
        return await self.claim_session(
            spec.session_id,
            spec.user_id,
            spec.channel,
        )

    async def claim_session(self, session_id, user_id, channel):
        identity = SessionIdentity(self.scope, user_id, channel, session_id)
        name = session_filename_key(identity)
        async with self.db.transaction():
            await self._assert_writes_allowed()
            await self.db.insert(
                "INSERT INTO swe_conversation_sessions "
                "(service_unit_key,tenant_key,source_key,agent_key,user_key,channel_key,"
                "external_session_key,nas_filename,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) "
                "ON DUPLICATE KEY UPDATE id=id",
                (*identity.sql_values, name),
            )
            row = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_sessions WHERE "
                + SESSION_PREDICATE
                + " FOR UPDATE",
                identity.sql_values,
            )
            if row is None or row["nas_filename"] != name:
                raise ConflictError(
                    "NAS Session filename belongs to another identity",
                )
            return row

    async def ensure_session_path(self, session_id, user_id, channel):
        identity = SessionIdentity(self.scope, user_id, channel, session_id)
        name = session_filename_key(identity)
        # Session filename mappings are immutable. Only a missing mapping
        # needs the write transaction.
        row = await self.db.fetch_one(
            "SELECT nas_filename FROM swe_conversation_sessions "
            "WHERE "
            + SESSION_PREDICATE
            + f" AND EXISTS (SELECT 1 FROM {WORKSPACES} WHERE "
            + SCOPE_PREDICATE
            + ")",
            (
                *identity.sql_values,
                *self.scope.sql_values,
            ),
        )
        if row is None:
            await self.claim_session(session_id, user_id, channel)
        elif row["nas_filename"] != name:
            raise ConflictError(
                "NAS Session filename belongs to another identity",
            )

    async def _publish_metadata(
        self,
        spec,
        session,
        row,
        *,
        create_if_absent=False,
    ):
        async with self.db.transaction():
            await self._assert_writes_allowed()
            return await self._install(
                spec,
                session,
                row,
                create_if_absent=create_if_absent,
            )

    async def _install(self, spec, session, row, *, create_if_absent=False):
        target_id = session["id"]
        source_id = row["session_id"] if row else target_id
        rebind = target_id != source_id
        current = None
        if row is not None:
            old_session, current = await self._locked_chat(spec.id)
            if old_session["id"] != source_id:
                raise ConflictError("Chat Session association changed")
        else:
            old_session = session
            if create_if_absent:
                current = await self.db.fetch_one(
                    "SELECT * FROM swe_conversation_chats WHERE session_id=%s "
                    "AND deleted_at IS NULL ORDER BY registry_order,id LIMIT 1 FOR UPDATE",
                    (source_id,),
                )
                if (
                    current is not None
                    and WorkspaceScope.from_row(current) != self.scope
                ):
                    raise IntegrityError("Draft Chat scope mismatch")
        meta = dict(spec.meta)
        if row is not None and current is not None:
            mode_key = "multi_agent_mode_enabled"
            current_meta = self._spec(current).meta
            meta.pop(mode_key, None)
            if mode_key in current_meta:
                meta[mode_key] = current_meta[mode_key]
        mutation, extended = metadata_install(
            spec,
            meta,
            revision=row["metadata_revision"] if row else None,
            create_if_absent=create_if_absent,
            target_session_id=target_id if rebind else None,
        )
        chat_id = await install_chat_metadata(
            self.db,
            SimpleNamespace(
                scope=self.scope,
                session_id=source_id,
                chat_id=row["id"] if row else None,
                turn_id=None,
            ),
            mutation,
            (old_session, current),
            target_session=session if rebind else None,
            extended_metadata=extended,
        )
        return {"chat_id": chat_id}

    async def update_chat_if_exists(self, spec, *, expected_chat=None):
        async with self.db.transaction():
            await self._assert_writes_allowed()
            row = await self._raw_chat(spec.id)
            if row is None or row["deleted_at"] is not None:
                return None
            existing = self._spec(row)
            if expected_chat is not None and expected_chat != existing:
                raise ConflictError("Chat changed since patch was read")
            if expected_chat is None and existing.meta.get(
                MANUAL_CHAT_TITLE_META_KEY,
            ):
                spec = spec.model_copy(
                    update={
                        "name": existing.name,
                        "meta": {
                            **spec.meta,
                            MANUAL_CHAT_TITLE_META_KEY: True,
                        },
                    },
                )
            session = await self._session_for(spec)
            await self._install(spec, session, row)
            return await self.get_chat(spec.id)

    async def _write_expert_binding(self, row, updated):
        if updated.expert_id is None:
            await super()._write_expert_binding(row, updated)
            return
        extended = load_metadata_json(row["extended_metadata_text"] or "{}")
        extended["multi_agent_mode_enabled"] = False
        _, extended_text = metadata_fields(extended)
        await self.db.execute(
            "UPDATE swe_conversation_chats SET expert_id=%s,"
            "expert_binding_revision=%s,extended_metadata_text=%s,"
            "metadata_revision=metadata_revision+1,updated_at=UTC_TIMESTAMP(6) "
            "WHERE id=%s AND service_unit_key=%s AND deleted_at IS NULL",
            (
                identity_bytes(updated.expert_id, 256),
                updated.revision,
                extended_text,
                row["id"],
                self.scope.sql_values[0],
            ),
        )

    async def set_multi_agent_mode(self, chat_id, enabled, expected_revision):
        async with self.db.transaction():
            await self._assert_writes_allowed()
            _, row = await self._locked_chat(chat_id)
            spec = self._spec(row)
            binding = read_expert_binding(spec)
            if binding.revision != expected_revision:
                raise ExpertBindingConflict(binding)
            spec.meta["multi_agent_mode_enabled"] = enabled
            _, extended = metadata_fields(spec.meta)
            await self.db.execute(
                "UPDATE swe_conversation_chats SET extended_metadata_text=%s,"
                "expert_id=IF(%s,NULL,expert_id),expert_binding_revision=expert_binding_revision+1,"
                "metadata_revision=metadata_revision+1,updated_at=%s WHERE id=%s",
                (
                    extended,
                    enabled,
                    datetime.now(timezone.utc).replace(tzinfo=None),
                    row["id"],
                ),
            )
            return await self.get_chat(chat_id)

    async def delete_chats(self, chat_ids, *, expected_chats=None):
        deleted = []
        async with self.db.transaction():
            await self._assert_writes_allowed()
            for chat_id in sorted(set(chat_ids)):
                try:
                    _, row = await self._locked_chat(chat_id)
                except KeyError:
                    continue
                if expected_chats is not None and self._spec(
                    row,
                ) != expected_chats.get(
                    chat_id,
                ):
                    raise ConflictError("Chat changed since authorization")
                changed = await self.db.execute(
                    "UPDATE swe_conversation_chats SET deleted_at=UTC_TIMESTAMP(6), "
                    "metadata_revision=metadata_revision+1 WHERE id=%s AND deleted_at IS NULL",
                    (row["id"],),
                )
                if changed:
                    deleted.append(row["id"])
                    await self.db.execute(
                        "UPDATE swe_conversation_sessions SET draft_chat_id=NULL "
                        "WHERE id=%s AND draft_chat_id=%s",
                        (row["session_id"], row["id"]),
                    )
        return bool(deleted)

    async def pending_resource_cleanup(
        self,
        *,
        limit=100,
        chat_ids=None,
        after=b"",
    ):
        where = (
            SCOPE_PREDICATE
            + " AND deleted_at IS NOT NULL AND resources_cleaned_at IS NULL"
        )
        where += " AND external_chat_key>%s"
        params = (*self.scope.sql_values, after)
        if chat_ids is not None:
            if not chat_ids:
                return []
            where += (
                " AND external_chat_key IN ("
                + ",".join(["%s"] * len(chat_ids))
                + ")"
            )
            params += tuple(
                identity_bytes(chat_id, 128) for chat_id in chat_ids
            )
        rows = await self.db.fetch_all(
            "SELECT external_chat_key FROM swe_conversation_chats WHERE "
            + where
            + " ORDER BY external_chat_key LIMIT %s",
            (*params, limit),
        )
        return [bytes(row["external_chat_key"]).decode() for row in rows]

    async def finish_resource_cleanup(self, chat_id):
        await self.db.execute(
            "UPDATE swe_conversation_chats SET resources_cleaned_at=UTC_TIMESTAMP(6) WHERE "
            + SCOPE_PREDICATE
            + " AND external_chat_key=%s AND deleted_at IS NOT NULL",
            (*self.scope.sql_values, identity_bytes(chat_id, 128)),
        )
