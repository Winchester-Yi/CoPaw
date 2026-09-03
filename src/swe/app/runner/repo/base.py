# -*- coding: utf-8 -*-
"""Chat repository for storing chat/session specs."""

from __future__ import annotations

from abc import ABC, abstractmethod
import base64
import binascii
from datetime import datetime, timezone
import json
from typing import Optional

from ..models import ChatPage, ChatSpec, ChatsFile
from ...channels.schema import DEFAULT_CHANNEL


class BaseChatRepository(ABC):
    """Abstract repository for chat specs persistence."""

    @abstractmethod
    async def load(self) -> ChatsFile:
        """Load all chat specs from storage."""
        raise NotImplementedError

    @abstractmethod
    async def save(self, chats_file: ChatsFile) -> None:
        """Persist all chat specs to storage (should be atomic if possible)."""
        raise NotImplementedError

    # ---- Convenience operations ----

    async def list_chats(self) -> list[ChatSpec]:
        """List all chat specifications."""
        cf = await self.load()
        return cf.chats

    async def get_chat(self, chat_id: str) -> Optional[ChatSpec]:
        """Get chat spec by chat_id (UUID).

        Args:
            chat_id: Chat UUID

        Returns:
            ChatSpec or None if not found
        """
        cf = await self.load()
        for chat in cf.chats:
            if chat.id == chat_id:
                return chat
        return None

    async def get_chat_by_id(
        self,
        session_id: str,
        user_id: str,
        channel: str = DEFAULT_CHANNEL,
    ) -> Optional[ChatSpec]:
        """Get chat spec by session_id and user_id.

        Args:
            session_id: Session identifier (e.g., "discord:alice")
            user_id: User identifier
            channel: Channel identifier

        Returns:
            ChatSpec or None if not found
        """
        import logging

        logger = logging.getLogger(__name__)

        cf = await self.load()

        logger.debug(
            f"get_chat_by_id: Searching in {len(cf.chats)} chats for "
            f"session_id={session_id}, user_id={user_id}, "
            f"channel={channel}",
        )

        for chat in cf.chats:
            if (
                chat.session_id == session_id
                and chat.user_id == user_id
                and chat.channel == channel
            ):
                logger.debug(f"get_chat_by_id: Found match: {chat.id}")
                return chat

        logger.debug("get_chat_by_id: No match found")
        return None

    async def create_chat_if_absent_by_session(
        self,
        spec: ChatSpec,
    ) -> tuple[ChatSpec, bool]:
        """Create a Chat only when its logical session has no record yet.

        Storage implementations with cross-process coordination override this
        compare-and-set operation. The default supports in-memory repositories.
        """
        existing = await self.get_chat_by_id(
            spec.session_id,
            spec.user_id,
            spec.channel,
        )
        if existing is not None:
            return existing, False
        await self.upsert_chat(spec)
        return spec, True

    async def upsert_chat(self, spec: ChatSpec) -> None:
        """Insert or update a chat spec.

        Args:
            spec: Chat specification to upsert
        """
        cf = await self.load()
        for i, c in enumerate(cf.chats):
            if c.id == spec.id:
                cf.chats[i] = spec
                break
        else:
            cf.chats.append(spec)
        await self.save(cf)

    async def delete_chats(self, chat_ids: list[str]) -> bool:
        """Delete a chat spec by chat_id (UUID).

        Args:
            chat_ids: List of chat IDs

        Returns:
            True if deleted, False if not found
        """
        if not chat_ids:
            return False

        cf = await self.load()
        before = len(cf.chats)
        cf.chats = [c for c in cf.chats if c.id not in chat_ids]
        if len(cf.chats) == before:
            return False
        await self.save(cf)
        return True

    async def filter_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> list[ChatSpec]:
        """Filter chats by user_id and/or channel.

        Args:
            user_id: Optional user ID filter
            channel: Optional channel filter

        Returns:
            Filtered list of chat specs
        """
        cf = await self.load()
        results = cf.chats

        if user_id is not None:
            results = [c for c in results if c.user_id == user_id]

        if channel is not None:
            results = [c for c in results if c.channel == channel]

        return results

    async def get_chat_id_by_session(
        self,
        session_id: str,
        channel: str,
    ) -> str | None:
        """Return the most recently updated chat for a session and channel."""
        chats = await self.filter_chats(channel=channel)
        matching = [chat for chat in chats if chat.session_id == session_id]
        if not matching:
            return None
        return max(matching, key=lambda chat: chat.updated_at).id

    async def paginate_chats(
        self,
        *,
        page: int,
        page_size: int,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> ChatPage:
        """Return one filtered chat page in stable newest-first order."""
        chats = await self.filter_chats(user_id=user_id, channel=channel)

        ordered = self.sort_chats_by_recency(chats)
        total = len(ordered)
        offset = (page - 1) * page_size
        items = ordered[offset : offset + page_size]
        return ChatPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_more=offset + len(items) < total,
        )

    @staticmethod
    def _recency_sort_key(chat: ChatSpec) -> tuple[datetime, str]:
        updated_at = chat.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return updated_at, chat.id

    @staticmethod
    def _creation_sort_key(chat: ChatSpec) -> tuple[datetime, str]:
        created_at = chat.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at, chat.id

    @classmethod
    def sort_chats_by_recency(cls, chats: list[ChatSpec]) -> list[ChatSpec]:
        """Return chats ordered by latest update, then identifier."""
        return sorted(chats, key=cls._recency_sort_key, reverse=True)

    @staticmethod
    def _encode_cursor(
        key: tuple[datetime, str],
        *,
        version: str = "v2",
    ) -> str:
        timestamp, chat_id = key
        cursor_payload = (
            [timestamp.astimezone(timezone.utc).isoformat(), chat_id]
            if version == "v1"
            else [
                "v2",
                timestamp.astimezone(timezone.utc).isoformat(),
                chat_id,
            ]
        )
        payload = json.dumps(
            cursor_payload,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, tuple[datetime, str]]:
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(cursor.encode("ascii")),
            )
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError("Invalid chat pagination cursor") from exc

        if not isinstance(payload, list):
            raise ValueError("Invalid chat pagination cursor")
        if len(payload) == 2:
            version = "v1"
            timestamp_raw, chat_id = payload
        elif len(payload) == 3 and payload[0] == "v2":
            version, timestamp_raw, chat_id = payload
        else:
            raise ValueError("Invalid chat pagination cursor")

        try:
            timestamp = datetime.fromisoformat(timestamp_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid chat pagination cursor") from exc
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("Invalid chat pagination cursor")
        return version, (timestamp, chat_id)

    async def paginate_chats_cursor(
        self,
        *,
        page_size: int,
        cursor: str | None = None,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> ChatPage:
        """Return a latest-update page or continue a legacy creation-time page."""
        chats = await self.filter_chats(user_id=user_id, channel=channel)
        cursor_version = "v2"
        sort_key = self._recency_sort_key
        boundary: tuple[datetime, str] | None = None
        if cursor:
            cursor_version, boundary = self._decode_cursor(cursor)
            if cursor_version == "v1":
                sort_key = self._creation_sort_key

        ordered = sorted(chats, key=sort_key, reverse=True)
        if boundary:
            ordered = [chat for chat in ordered if sort_key(chat) < boundary]

        total = len(chats)
        items = ordered[:page_size]
        has_more = len(ordered) > len(items)
        next_cursor = (
            self._encode_cursor(sort_key(items[-1]), version=cursor_version)
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
