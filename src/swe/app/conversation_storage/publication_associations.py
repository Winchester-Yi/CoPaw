# -*- coding: utf-8 -*-
"""Complete ascending Session, Chat and historical Turn publication locks."""

from .errors import IntegrityError


class PublicationAssociations:
    async def _lock_associations(
        self,
        request,
        *,
        session_rows=None,
        archive_turns=None,
        archive_chat_ids=(),
    ):
        session = chat = turn = None
        archive_turns = archive_turns or {}
        if request.session_id is not None:
            if session_rows is None:
                session = await self.db.fetch_one(
                    "SELECT * "
                    "FROM swe_conversation_sessions WHERE id=%s AND service_unit_key=%s FOR UPDATE",
                    (request.session_id, request.scope.sql_values[0]),
                )
            else:
                session = session_rows.get(request.session_id)
        historical_chat_ids = {
            item["chat_id"] for item in archive_turns.values()
        } | set(archive_chat_ids)
        chat_ids = set(historical_chat_ids)
        if request.chat_id is not None:
            chat_ids.add(request.chat_id)
        for identity in sorted(chat_ids):
            current = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_chats WHERE id=%s AND service_unit_key=%s FOR UPDATE",
                (identity, request.scope.sql_values[0]),
            )
            if identity == request.chat_id:
                chat = current
            if identity in historical_chat_ids:
                self._validate_archive_chat(request, current)
        turn_ids = set(archive_turns)
        if request.turn_id is not None:
            turn_ids.add(request.turn_id)
        for identity in sorted(turn_ids):
            current = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_turns WHERE id=%s AND service_unit_key=%s FOR UPDATE",
                (identity, request.scope.sql_values[0]),
            )
            if (
                current is not None
                and current["service_unit_key"] != request.scope.sql_values[0]
            ):
                raise IntegrityError("publication Turn service unit mismatch")
            if identity == request.turn_id:
                turn = current
            if identity in archive_turns and (
                current is None
                or any(
                    current[key] != archive_turns[identity][key]
                    for key in (
                        "id",
                        "service_unit_key",
                        "session_id",
                        "chat_id",
                    )
                )
            ):
                raise IntegrityError("archived Turn association changed")
        return session, chat, turn

    @staticmethod
    def _validate_archive_chat(request, chat):
        if (
            chat is None
            or tuple(
                chat[key]
                for key in (
                    "service_unit_key",
                    "tenant_key",
                    "source_key",
                    "agent_key",
                )
            )
            != request.scope.sql_values
        ):
            raise IntegrityError("archived Turn Chat scope mismatch")

    async def _archive_turns(self, request, plan):
        result = {}
        for identity in sorted(
            {
                item.turn_id
                for item in plan.messages
                if item.layer == "archive" and item.turn_id is not None
            },
        ):
            turn = await self.db.fetch_one(
                "SELECT id,service_unit_key,session_id,chat_id FROM swe_conversation_turns WHERE id=%s AND service_unit_key=%s",
                (identity, request.scope.sql_values[0]),
            )
            if (
                turn is None
                or turn["service_unit_key"] != request.scope.sql_values[0]
                or turn["session_id"] != request.session_id
            ):
                raise IntegrityError(
                    "archived Turn does not belong to Session",
                )
            if any(
                item.origin_chat_id is not None
                and item.origin_chat_id != turn["chat_id"]
                for item in plan.messages
                if item.layer == "archive" and item.turn_id == identity
            ):
                raise IntegrityError(
                    "archived occurrence origin differs from Turn",
                )
            result[identity] = turn
        return result
