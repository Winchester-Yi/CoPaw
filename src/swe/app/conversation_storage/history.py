# -*- coding: utf-8 -*-
"""Indexed committed history with pinned immutable source-message extents."""

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from datetime import timezone

from agentscope.message import Msg

from ..runner.models import (
    ChatArchiveMetadata,
    ChatArchivePage,
    ChatCompactionBoundary,
    ChatHistory,
)
from .bundle import _load_json
from .chat_repository import (
    MySQLChatRepository,
    SELECT_CHAT,
    SCOPE_WHERE,
    _text,
)
from .errors import ConflictError, IntegrityError
from .identity import chat_key_bytes, identity_bytes
from .retention import MAX_PIN_BATCH, RetentionStore

MESSAGE_SELECT = "SELECT m.* FROM swe_conversation_messages m WHERE "
VISIBLE_ONLINE = (
    "m.session_id=%s AND m.current_visible=1 AND m.deleted_at IS NULL "
    "AND (m.layer IN ('memory','task','error') OR (m.layer='anchor' "
    "AND (m.origin_chat_id=%s OR m.origin_chat_id IS NULL)))"
)


@dataclass
class HistorySnapshot:
    chat: dict
    rows: list
    boundaries: list
    batches: list
    turn_status: str | None = None


class HistoryStore:
    """One trusted workspace; callers authorize the current Chat."""

    def __init__(self, database, scope, bundles, *, cursor_secret):
        if not isinstance(cursor_secret, bytes) or not cursor_secret:
            raise ValueError("history cursor secret must be nonempty bytes")
        self.db = database
        self.scope = scope
        self.bundles = bundles
        self.cursor_secret = cursor_secret
        self.retentions = RetentionStore(database, scope)
        self.chats = MySQLChatRepository(database, scope, bundles)

    def _signature(self, raw):
        namespace = json.dumps(
            self.scope.sql_values,
            default=lambda v: v.hex(),
            separators=(",", ":"),
        ).encode()
        return hmac.new(
            self.cursor_secret,
            namespace + b"\0" + raw,
            hashlib.sha256,
        ).digest()

    def _encode_cursor(self, chat, *, epoch, high, before):
        raw = json.dumps(
            {
                "v": 2,
                "chat": chat,
                "epoch": epoch,
                "high": high,
                "before": before,
            },
            separators=(",", ":"),
        ).encode()
        return (
            base64.urlsafe_b64encode(raw + self._signature(raw))
            .decode()
            .rstrip("=")
        )

    def _decode_cursor(self, token, chat) -> dict:
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError("invalid token")
            value = base64.b64decode(
                token + "=" * (-len(token) % 4),
                altchars=b"-_",
                validate=True,
            )
            raw, signature = value[:-32], value[-32:]
            if not hmac.compare_digest(self._signature(raw), signature):
                raise ValueError("invalid signature")
            body = _load_json(raw)
            if (
                set(body) != {"v", "chat", "epoch", "high", "before"}
                or body["v"] != 2
                or body["chat"] != chat
            ):
                raise ValueError("invalid identity")
            if (
                any(
                    type(body[key]) is not int or body[key] < 0
                    for key in ("epoch", "high", "before")
                )
                or body["before"] > body["high"] + 1
            ):
                raise ValueError("invalid boundary")
            return body
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as error:
            raise ValueError("Invalid conversation archive cursor") from error

    async def _convert(self, rows, payloads, session_id):
        from ..runner import api

        messages = []
        for row, payload in zip(rows, payloads):
            layer = row["layer"]
            projection = None
            if set(payload) == {"version", "message", "history_projection"}:
                if payload["version"] != 1 or not isinstance(
                    payload["message"],
                    dict,
                ):
                    raise IntegrityError(
                        "history projection envelope is invalid",
                    )
                projection = payload["history_projection"]
                payload = payload["message"]
            if layer in ("memory", "archive"):
                try:
                    msg = Msg.from_dict(payload)
                except (ValueError, TypeError, KeyError) as error:
                    raise IntegrityError(
                        "history source message is invalid",
                    ) from error
                converted = api._history_msg_to_messages(
                    [msg],
                    session_id=session_id,
                    position_offset=row["source_occurrence_no"],
                )
                if row.get("task_run_key") is not None:
                    projected = (
                        projection
                        if projection is not None
                        else payload.get("metadata", {})
                    )
                    if (
                        not isinstance(projected, dict)
                        or type(projected.get("task_run_index")) is not int
                        or projected["task_run_index"] < 0
                        or projected.get("task_run_section")
                        not in ("step", "final")
                        or projected.get("task_run_id")
                        != _text(row["task_run_key"])
                    ):
                        raise IntegrityError(
                            "task history projection is missing or invalid",
                        )
                    converted = [
                        api._with_task_run_metadata(
                            message,
                            run_id=projected["task_run_id"],
                            run_index=projected["task_run_index"],
                            section=projected["task_run_section"],
                        )
                        for message in converted
                    ]
            elif layer == "task":
                converted = api._task_session_messages_from_state(
                    {api.TASK_MESSAGES_STATE_KEY: [payload]},
                )
            elif layer == "error":
                converted = api._model_call_failed_messages_from_state(
                    {api.MODEL_CALL_FAILED_MESSAGES_STATE_KEY: [payload]},
                )
            elif layer == "anchor":
                message = api._turn_state_to_message(
                    payload.get("id"),
                    {"message": payload},
                    chat_id=None,
                )
                converted = [message] if message is not None else []
            else:
                raise IntegrityError("unknown history message layer")
            messages.extend(converted)
        return messages

    async def _chat(self, chat_id):
        row = await self.db.fetch_one(
            SELECT_CHAT
            + SCOPE_WHERE
            + " AND c.external_chat_key=%s AND c.deleted_at IS NULL",
            (*self.scope.sql_values, chat_key_bytes(chat_id)),
        )
        if row is None:
            raise KeyError(chat_id)
        return row

    async def _pins(self, chat, rows, boundaries):
        service = self.scope.sql_values[0]
        if any(
            row["service_unit_key"] != service
            for row in [chat, *rows, *boundaries]
        ):
            raise IntegrityError("history service association mismatch")
        ids = {row["publication_id"] for row in rows}
        ids.update(row["id"] for row in boundaries)
        if chat["content_meta_publication_id"] is not None:
            ids.add(chat["content_meta_publication_id"])
        ordered = sorted(ids)
        batches = []
        for start in range(0, len(ordered), MAX_PIN_BATCH):
            stop = start + MAX_PIN_BATCH
            batches.append(
                await self.retentions.pin(ordered[start:stop], kind="reader"),
            )
        return batches

    async def _boundaries(self, chat, rows=None, *, latest=False):
        where = "chat_id=%s AND state='committed' AND boundary_key IS NOT NULL"
        params = [chat["id"]]
        if rows is not None:
            ids = sorted(
                {
                    r["archive_publication_id"]
                    for r in rows
                    if r["archive_publication_id"] is not None
                },
            )
            if not ids:
                return []
            where += " AND id IN (" + ",".join(["%s"] * len(ids)) + ")"
            params.extend(ids)
        suffix = (
            " ORDER BY boundary_order DESC LIMIT 1"
            if latest
            else " ORDER BY boundary_order ASC"
        )
        return await self.db.fetch_all(
            "SELECT * FROM swe_conversation_publications WHERE "
            + where
            + suffix,
            tuple(params),
        )

    async def _snapshot(self, chat_id, *, layers=None):
        async with self.db.transaction():
            chat = await self._chat(chat_id)
            session = await self.db.fetch_one(
                "SELECT history_high FROM swe_conversation_sessions "
                "WHERE id=%s",
                (chat["session_id"],),
            )
            where = VISIBLE_ONLINE
            params = [chat["session_id"], chat["id"]]
            where += " AND m.session_order<=%s"
            params.append(session["history_high"])
            if layers:
                where += (
                    " AND m.layer IN (" + ",".join(["%s"] * len(layers)) + ")"
                )
                params.extend(layers)
            rows = await self.db.fetch_all(
                MESSAGE_SELECT
                + where
                + " ORDER BY m.session_order ASC,m.id ASC",
                tuple(params),
            )
            anchors = [
                r["source_msg_key"]
                for r in rows
                if r["layer"] == "anchor" and r["source_msg_key"] is not None
            ]
            if anchors:
                placeholders = ",".join(["%s"] * len(anchors))
                archived = await self.db.fetch_all(
                    "SELECT source_msg_key FROM swe_conversation_messages "
                    "WHERE archive_chat_id=%s AND deleted_at IS NULL "
                    "AND source_msg_key IN (" + placeholders + ")",
                    (chat["id"], *anchors),
                )
                suppressed = {r["source_msg_key"] for r in archived}
                rows = [
                    r
                    for r in rows
                    if r["layer"] != "anchor"
                    or r["source_msg_key"] not in suppressed
                ]
            boundaries = (
                await self._boundaries(chat, latest=True)
                if layers is None
                else []
            )
            batches = await self._pins(chat, rows, boundaries)
            await self._revalidate_rows(chat, rows, current_read=True)
            return HistorySnapshot(chat, rows, boundaries, batches)

    async def _revalidate_rows(self, chat, rows, *, current_read=False):
        if not current_read:
            await self._validate_chat_row(chat)
        if not rows:
            return
        ids = [row["id"] for row in rows]
        markers = ",".join(["%s"] * len(ids))
        actual = await self.db.fetch_all(
            "SELECT * FROM swe_conversation_messages WHERE id IN ("
            + markers
            + ") AND service_unit_key=%s"
            + (" FOR SHARE" if current_read else ""),
            (*ids, self.scope.sql_values[0]),
        )
        by_id = {row["id"]: row for row in actual}
        fields = (
            "service_unit_key",
            "session_id",
            "origin_chat_id",
            "archive_chat_id",
            "layer",
            "session_order",
            "archive_order",
            "publication_id",
            "byte_offset",
            "byte_length",
            "deleted_at",
            "current_visible",
            "context_epoch",
        )
        for row in rows:
            if row["id"] not in by_id or any(
                by_id[row["id"]][field] != row[field] for field in fields
            ):
                raise ConflictError("History locator or visibility changed")

    async def _validate_chat_row(self, chat):
        current = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_chats WHERE id=%s AND service_unit_key=%s",
            (chat["id"], self.scope.sql_values[0]),
        )
        chat_fields = (
            "service_unit_key",
            "session_id",
            "user_key",
            "channel_key",
            "history_revision",
            "epoch",
            "deleted_at",
            "metadata_revision",
            "expert_binding_revision",
            "content_meta_publication_id",
            "content_meta_offset",
            "content_meta_length",
        )
        if current is None or any(
            current[field] != chat[field] for field in chat_fields
        ):
            raise ConflictError("Chat history changed while reading")

    async def _payloads(self, snapshot):
        publications = {
            row["id"]: (row, batch)
            for batch in snapshot.batches
            for row in batch.publications
        }
        payloads = []
        for row in snapshot.rows:
            if row["session_id"] != snapshot.chat["session_id"]:
                raise IntegrityError("history Message Session mismatch")
            if (
                row["layer"] == "archive"
                and row["archive_chat_id"] != snapshot.chat["id"]
            ):
                raise IntegrityError("history archive Chat mismatch")
            publication, batch = publications[row["publication_id"]]
            if publication["session_id"] != snapshot.chat["session_id"]:
                raise IntegrityError("history publication Session mismatch")
            value = await batch.read(
                self.bundles.read_extent,
                _text(publication["relative_path"]),
                row["byte_offset"],
                row["byte_length"],
                expected_length=publication["byte_length"],
            )
            try:
                payload = _load_json(value)
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except (ValueError, TypeError, UnicodeDecodeError) as error:
                raise IntegrityError("history payload is invalid") from error
            raw_message = (
                payload.get("message")
                if set(payload) == {"version", "message", "history_projection"}
                else payload
            )
            if not isinstance(raw_message, dict):
                raise IntegrityError("history source message is invalid")
            if (
                raw_message.get("id") is not None
                and row["source_msg_key"] is not None
                and identity_bytes(raw_message["id"], 1024)
                != row["source_msg_key"]
            ):
                raise IntegrityError(
                    "history source message identity mismatch",
                )
            if (
                raw_message.get("role") is not None
                and identity_bytes(raw_message["role"], 32) != row["role_key"]
            ):
                raise IntegrityError("history source message role mismatch")
            payloads.append(payload)
        return payloads

    @staticmethod
    def _boundary(row):
        def stamp(value):
            if value is None:
                return None
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()

        return ChatCompactionBoundary(
            id=_text(row["boundary_key"]),
            archived_message_count=row["archive_display_count"],
            first_message_id=_text(row["archive_first_source_msg_key"]),
            last_message_id=_text(row["archive_last_source_msg_key"]),
            created_at=stamp(row["committed_at"]),
            first_timestamp=stamp(row["archive_first_timestamp"]),
            last_timestamp=stamp(row["archive_last_timestamp"]),
        )

    async def _finish(self, snapshot):
        await self.chats._finish_batches(snapshot.batches)

    async def _assemble(self, snapshot, *, authorize=None, tracing=False):
        from ..runner import api

        try:
            await self._validate_chat_row(snapshot.chat)
            spec = (
                await self.chats._hydrate([snapshot.chat], snapshot.batches)
            )[0]
            if authorize is not None:
                authorize(spec)
            payloads = await self._payloads(snapshot)
            if tracing:
                messages = await self._convert(
                    snapshot.rows,
                    payloads,
                    spec.session_id,
                )
            else:
                primary = []
                anchors = []
                for row, payload in zip(snapshot.rows, payloads):
                    converted = await self._convert(
                        [row],
                        [payload],
                        spec.session_id,
                    )
                    (anchors if row["layer"] == "anchor" else primary).extend(
                        converted,
                    )
                known = {api._message_original_id(m) for m in primary}
                messages = [
                    *primary,
                    *[
                        m
                        for m in anchors
                        if api._message_original_id(m) not in known
                    ],
                ]
            messages.sort(key=api._message_sort_key)
            from swe.config.context import tenant_context

            with tenant_context(
                tenant_id=self.scope.tenant_id,
                source_id=self.scope.source_id,
                user_id=spec.user_id,
            ):
                messages = await api._annotate_approval_action_statuses(
                    messages,
                )
            if not tracing:
                messages = api._redact_hidden_context_messages(messages)
            await self._revalidate_rows(snapshot.chat, snapshot.rows)
            if authorize is not None:
                authorize(spec)
            archive = ChatArchiveMetadata(
                has_more=bool(snapshot.boundaries),
                boundaries=[self._boundary(r) for r in snapshot.boundaries],
            )
            return ChatHistory(
                chat=None if tracing else spec,
                messages=messages,
                status=spec.status,
                archive=archive,
            )
        finally:
            await self._finish(snapshot)

    async def current(self, chat_id, *, authorize=None):
        return await self._assemble(
            await self._snapshot(chat_id),
            authorize=authorize,
        )

    async def tracing_view(self, chat_id, *, authorize=None):
        return await self._assemble(
            await self._snapshot(chat_id, layers=("memory", "task")),
            authorize=authorize,
            tracing=True,
        )

    async def archive_page(
        self,
        chat_id,
        *,
        before=None,
        limit=50,
        authorize=None,
    ):
        from ..runner import api

        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("archive limit must be between 1 and 50")
        cursor = self._decode_cursor(before, chat_id) if before else None
        async with self.db.transaction():
            chat = await self._chat(chat_id)
            high = chat["history_high"]
            bound = high + 1
            if isinstance(cursor, dict):
                high = cursor.get("high")
                bound = cursor.get("before")
                if cursor.get("epoch") != chat["epoch"]:
                    raise ValueError(
                        "Invalid conversation archive cursor epoch",
                    )
            rows = await self.db.fetch_all(
                MESSAGE_SELECT
                + "m.archive_chat_id=%s AND m.deleted_at IS NULL "
                "AND m.archive_order<%s AND m.archive_order<=%s "
                "ORDER BY m.archive_order DESC LIMIT %s",
                (chat["id"], bound, high, limit + 1),
            )
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
            boundaries = await self._boundaries(chat, rows)
            batches = await self._pins(chat, rows, boundaries)
            await self._revalidate_rows(chat, rows, current_read=True)
            snapshot = HistorySnapshot(chat, rows, boundaries, batches)
        try:
            await self._validate_chat_row(chat)
            spec = (await self.chats._hydrate([chat], batches))[0]
            if authorize is not None:
                authorize(spec)
            messages = await self._convert(
                rows,
                await self._payloads(snapshot),
                chat_id,
            )
            await self._revalidate_rows(chat, rows)
            if authorize is not None:
                authorize(spec)
            visible = [
                self._boundary(row)
                for row in boundaries
                if any(
                    m["archive_order"] == row["archive_last_order"]
                    for m in rows
                )
            ]
            next_cursor = (
                self._encode_cursor(
                    chat_id,
                    epoch=chat["epoch"],
                    high=high,
                    before=rows[0]["archive_order"],
                )
                if has_more and rows
                else None
            )
            return ChatArchivePage(
                messages=api._redact_hidden_context_messages(messages),
                boundaries=visible,
                has_more=has_more,
                next_cursor=next_cursor,
            )
        finally:
            await self._finish(snapshot)

    async def context_usage(self, chat_id, *, authorize=None):
        from pydantic import ValidationError
        from ..runner.context_usage import (
            ContextUsageAvailable,
            ContextUsageSnapshot,
            ContextUsageUnavailable,
        )

        async with self.db.transaction():
            chat = await self._chat(chat_id)
            usage = await self.db.fetch_one(
                "SELECT context_usage_text,context_usage_invalid "
                "FROM swe_conversation_sessions WHERE id=%s",
                (chat["session_id"],),
            )
        if authorize is not None:
            authorization_chat = self.chats._spec(chat, {})
            authorize(authorization_chat)
            if chat["content_meta_publication_id"] is not None and any(
                key not in authorization_chat.meta
                for key in ("source_id", "agent_id")
            ):
                # Historical oversized identity fields may live on NAS.
                authorization_chat = await self.chats.get_chat(chat_id)
                if authorization_chat is None:
                    raise KeyError(chat_id)
                authorize(authorization_chat)
        raw = usage["context_usage_text"]
        await self.chats._revalidate([chat])
        if raw is None:
            return ContextUsageUnavailable()
        try:
            snapshot = ContextUsageSnapshot.model_validate(_load_json(raw))
        except (ValueError, TypeError, ValidationError):
            return ContextUsageUnavailable()
        if authorize is not None:
            authorize(authorization_chat)
        return ContextUsageAvailable(
            **snapshot.model_dump(),
            stale=bool(usage["context_usage_invalid"])
            or chat["projected_status"] in ("running", "stopping"),
        )

    async def _turn_snapshot(self, chat_id, msgid):
        async with self.db.transaction():
            chat = await self._chat(chat_id)
            key = identity_bytes(msgid, 256)
            turn = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_turns WHERE session_id=%s "
                "AND question_msg_key=%s",
                (chat["session_id"], key),
            )
            if turn is not None and turn["chat_id"] != chat["id"]:
                raise KeyError(msgid)
            candidates = await self.db.fetch_all(
                MESSAGE_SELECT
                + VISIBLE_ONLINE
                + " AND m.source_msg_key=%s ORDER BY m.session_order,m.id",
                (chat["session_id"], chat["id"], key),
            )
            if not candidates or _text(candidates[0]["role_key"]) != "user":
                raise KeyError(msgid)
            anchor = candidates[0]
            if turn is not None and anchor["turn_id"] not in (
                None,
                turn["id"],
            ):
                raise IntegrityError(
                    "question occurrence Turn association mismatch",
                )
            typed_turn = turn is not None and anchor["turn_id"] == turn["id"]
            high = await self.db.fetch_one(
                "SELECT history_high FROM swe_conversation_sessions "
                "WHERE id=%s",
                (chat["session_id"],),
            )
            next_user = await self.db.fetch_one(
                "SELECT MIN(m.session_order) AS next_order "
                "FROM swe_conversation_messages m WHERE "
                + VISIBLE_ONLINE
                + " AND m.role_key=%s AND m.session_order>%s",
                (
                    chat["session_id"],
                    chat["id"],
                    b"user",
                    anchor["session_order"],
                ),
            )
            end = (
                next_user["next_order"]
                if next_user["next_order"] is not None
                else high["history_high"] + 1
            )
            # Recovery projection may repeat the admission question. It is
            # not a new Turn boundary when the persisted Turn owns the slice.
            message_filter = (
                " AND m.turn_id=%s"
                if typed_turn
                else " AND m.session_order>=%s AND m.session_order<%s"
            )
            message_params = (
                (turn["id"],) if typed_turn else (anchor["session_order"], end)
            )
            rows = await self.db.fetch_all(
                MESSAGE_SELECT
                + VISIBLE_ONLINE
                + message_filter
                + " ORDER BY m.session_order,m.id",
                (
                    chat["session_id"],
                    chat["id"],
                    *message_params,
                ),
            )
            anchors = [
                r["source_msg_key"]
                for r in rows
                if r["layer"] == "anchor" and r["source_msg_key"] is not None
            ]
            if anchors:
                marks = ",".join(["%s"] * len(anchors))
                archived = await self.db.fetch_all(
                    "SELECT source_msg_key FROM swe_conversation_messages "
                    "WHERE archive_chat_id=%s AND deleted_at IS NULL "
                    "AND source_msg_key IN (" + marks + ")",
                    (chat["id"], *anchors),
                )
                suppressed = {r["source_msg_key"] for r in archived}
                rows = [
                    r
                    for r in rows
                    if r["layer"] != "anchor"
                    or r["source_msg_key"] not in suppressed
                ]
            batches = await self._pins(chat, rows, [])
            await self._revalidate_rows(chat, rows, current_read=True)
            return HistorySnapshot(
                chat,
                rows,
                [],
                batches,
                turn["history_status"] if turn else None,
            )

    async def answer_turn(
        self,
        msgid,
        *,
        chat_id=None,
        session_id=None,
        user_id,
        authorize=None,
    ):
        from ..runner import api

        if chat_id is None:
            if session_id is None:
                raise KeyError(msgid)
            matches = await self.db.fetch_all(
                SELECT_CHAT
                + SCOPE_WHERE
                + " AND c.deleted_at IS NULL AND c.user_key=%s "
                "AND c.channel_key=%s AND s.external_session_key=%s "
                "ORDER BY c.registry_order,c.id",
                (
                    *self.scope.sql_values,
                    identity_bytes(user_id, 256),
                    b"console",
                    identity_bytes(session_id, 1024),
                ),
            )
            candidates = [_text(chat["external_chat_key"]) for chat in matches]
        else:
            candidates = [chat_id]
        for candidate in candidates:
            try:
                snapshot = await self._turn_snapshot(candidate, msgid)
                result = await self._assemble(snapshot, authorize=authorize)
            except KeyError:
                continue
            if result.chat.user_id != user_id:
                continue
            messages = api._slice_answer_turn(result.messages, msgid=msgid)
            if messages is None:
                continue
            result.messages = messages
            result.turn_status = snapshot.turn_status or next(
                (
                    status
                    for status in (
                        api._message_turn_status(m) for m in messages
                    )
                    if status is not None
                ),
                None,
            )
            return result
        raise KeyError(msgid)
