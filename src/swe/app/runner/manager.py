# -*- coding: utf-8 -*-
"""Chat manager for managing chat specifications."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ...agents.memory.conversation_archive import ConversationArchiveStore
from .models import ChatPage, ChatSpec
from .repo import BaseChatRepository
from ..channels.schema import DEFAULT_CHANNEL

logger = logging.getLogger(__name__)

_SESSION_TITLE_GENERATED_META_KEY = "session_title_generated"


def _is_older_datetime(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None and left.tzinfo is not None:
        right = right.replace(tzinfo=timezone.utc)
    return left < right


def _should_keep_generated_title(
    *,
    existing: ChatSpec,
    incoming: ChatSpec,
) -> bool:
    existing_meta = existing.meta or {}
    incoming_meta = incoming.meta or {}
    if not existing_meta.get(_SESSION_TITLE_GENERATED_META_KEY):
        return False
    if incoming_meta.get(_SESSION_TITLE_GENERATED_META_KEY):
        return False
    return _is_older_datetime(incoming.updated_at, existing.updated_at)


def _keep_generated_title(existing: ChatSpec, incoming: ChatSpec) -> None:
    incoming.name = existing.name
    incoming.meta = {
        **(existing.meta or {}),
        **(incoming.meta or {}),
        _SESSION_TITLE_GENERATED_META_KEY: True,
    }


def _can_replace_with_generated_title(
    spec: ChatSpec,
    fallback_name: str,
) -> bool:
    meta = spec.meta or {}
    if meta.get("session_kind") == "task":
        return False
    if meta.get(_SESSION_TITLE_GENERATED_META_KEY):
        return False
    return (spec.name or "").strip() in {
        "New Chat",
        "新会话",
        fallback_name,
    }


class ChatManager:
    """Manages chat specifications in repository.

    Handles ChatSpec CRUD and lifecycle cleanup for chat-scoped archives.
    Does NOT manage Redis session state - that's handled by runner's session.

    Similar to CronManager's role in crons module.
    """

    def __init__(
        self,
        *,
        repo: BaseChatRepository,
        archive_store: ConversationArchiveStore | None = None,
    ):
        """Initialize chat manager.

        Args:
            repo: Chat spec repository for persistence
        """
        self._repo = repo
        self._archive_store = archive_store
        self._lock = asyncio.Lock()
        repo_path = getattr(repo, "path", "<unknown>")
        logger.info(
            f"ChatManager created with repo path: {repo_path}",
        )

    # ----- Read Operations -----

    async def list_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> list[ChatSpec]:
        """List chat specs with optional filters.

        Args:
            user_id: Optional user ID filter
            channel: Optional channel filter

        Returns:
            List of chat specifications
        """
        async with self._lock:
            logger.debug(
                f"list_chats: repo path={self._repo.path}, "
                f"filters: user_id={user_id}, channel={channel}",
            )
            chats = await self._repo.filter_chats(
                user_id=user_id,
                channel=channel,
            )
            return self._repo.sort_chats_by_recency(chats)

    async def list_chats_page(
        self,
        *,
        page: int,
        page_size: int,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> ChatPage:
        """List one filtered chat page in stable newest-first order."""
        async with self._lock:
            logger.debug(
                "list_chats_page: repo path=%s, filters: user_id=%s, "
                "channel=%s, page=%s, page_size=%s",
                self._repo.path,
                user_id,
                channel,
                page,
                page_size,
            )
            return await self._repo.paginate_chats(
                user_id=user_id,
                channel=channel,
                page=page,
                page_size=page_size,
            )

    async def list_chats_cursor(
        self,
        *,
        page_size: int,
        cursor: str | None = None,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> ChatPage:
        """List a live latest-update-ordered page using an opaque cursor."""
        async with self._lock:
            return await self._repo.paginate_chats_cursor(
                user_id=user_id,
                channel=channel,
                page_size=page_size,
                cursor=cursor,
            )

    async def get_chat(self, chat_id: str) -> Optional[ChatSpec]:
        """Get chat spec by chat_id (UUID).

        Args:
            chat_id: Chat UUID

        Returns:
            Chat spec or None if not found
        """
        async with self._lock:
            return await self._repo.get_chat(chat_id)

    async def get_chat_by_session(
        self,
        session_id: str,
        channel: str = DEFAULT_CHANNEL,
        user_id: Optional[str] = None,
    ) -> Optional[ChatSpec]:
        """Get the chat spec for a logical session.

        The normal product invariant is unique by session, user, and channel.
        If legacy data has duplicates, return the most recently updated match.
        """
        async with self._lock:
            chats = await self._repo.filter_chats(
                user_id=user_id,
                channel=channel,
            )
            matching_chats = [
                chat for chat in chats if chat.session_id == session_id
            ]
            if not matching_chats:
                return None
            if len(matching_chats) > 1:
                logger.warning(
                    "Multiple chat records for session_id=%s "
                    "user_id=%s channel=%s; using most recently updated",
                    session_id,
                    user_id,
                    channel,
                )
            return max(matching_chats, key=lambda c: c.updated_at)

    async def get_or_create_chat(
        self,
        session_id: str,
        user_id: str,
        channel: str = DEFAULT_CHANNEL,
        name: str = "New Chat",
        meta: Optional[dict[str, Any]] = None,
    ) -> ChatSpec:
        """Get existing chat or create new one.

        Useful for auto-registration when chats come from channels.

        Args:
            session_id: Session identifier (channel:user_id)
            user_id: User identifier
            channel: Channel name
            name: Chat name
            meta: Optional metadata to merge into the chat spec

        Returns:
            Chat specification (existing or newly created)
        """
        async with self._lock:
            # Try to find existing by session_id
            logger.debug(
                f"get_or_create_chat: Searching for existing chat: "
                f"session_id={session_id}, user_id={user_id}, "
                f"channel={channel}",
            )
            existing = await self._repo.get_chat_by_id(
                session_id,
                user_id,
                channel,
            )
            if existing:
                if meta:
                    merged_meta = {
                        **(existing.meta or {}),
                        **meta,
                    }
                    if merged_meta != (existing.meta or {}):
                        existing.meta = merged_meta
                        existing.updated_at = datetime.now(timezone.utc)
                        await self._repo.upsert_chat(existing)
                logger.debug(
                    f"get_or_create_chat: Found existing chat: {existing.id}",
                )
                return existing

            # Create new
            logger.debug(
                f"get_or_create_chat: Creating new chat for "
                f"session_id={session_id}",
            )
            spec = ChatSpec(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                name=name,
                meta=meta or {},
            )
            logger.debug(f"get_or_create_chat: created spec={spec.id}")
            # Call internal create without lock (already locked)
            await self._repo.upsert_chat(spec)
            logger.debug(
                f"Auto-registered new chat: {spec.id} -> {session_id}",
            )
            return spec

    async def create_chat(self, spec: ChatSpec) -> ChatSpec:
        """Create a new chat.

        Args:
            spec: Chat specification (chat_id will be generated if not set)

        Returns:
            Chat spec
        """
        async with self._lock:
            await self._repo.upsert_chat(spec)
            return spec

    async def update_chat(self, spec: ChatSpec) -> ChatSpec:
        """Update an existing chat spec.

        Args:
            spec: Updated chat specification

        Returns:
            Updated chat spec
        """
        async with self._lock:
            existing = await self._repo.get_chat(spec.id)
            if existing is not None and _should_keep_generated_title(
                existing=existing,
                incoming=spec,
            ):
                _keep_generated_title(existing, spec)
            spec.updated_at = datetime.now(timezone.utc)
            await self._repo.upsert_chat(spec)
            return spec

    async def update_chat_name(
        self,
        chat_id: str,
        name: str,
        *,
        meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """更新会话名称。

        Args:
            chat_id: Chat 标识
            name: 新名称
            meta: 需要同步合并写入的元数据

        Returns:
            更新成功返回 True，chat 不存在返回 False
        """
        async with self._lock:
            spec = await self._repo.get_chat(chat_id)
            if spec is None:
                logger.warning(
                    "update_chat_name: chat not found chat_id=%s",
                    chat_id,
                )
                return False
            spec.name = name
            if meta:
                spec.meta = {
                    **(spec.meta or {}),
                    **meta,
                }
            spec.updated_at = datetime.now(timezone.utc)
            await self._repo.upsert_chat(spec)
            return True

    async def update_generated_chat_title(
        self,
        chat_id: str,
        name: str,
        *,
        fallback_name: str,
    ) -> bool:
        """Atomically apply an automatic title only while it is eligible."""
        async with self._lock:
            spec = await self._repo.get_chat(chat_id)
            if spec is None:
                logger.warning(
                    "update_generated_chat_title: chat not found chat_id=%s",
                    chat_id,
                )
                return False
            if not _can_replace_with_generated_title(spec, fallback_name):
                return False
            spec.name = name
            spec.meta = {
                **(spec.meta or {}),
                _SESSION_TITLE_GENERATED_META_KEY: True,
            }
            spec.updated_at = datetime.now(timezone.utc)
            await self._repo.upsert_chat(spec)
            return True

    async def delete_chats(self, chat_ids: list[str]) -> bool:
        """Delete a chat spec.

        Note: This only deletes the spec. Redis session state is NOT deleted.

        Args:
            chat_ids: List of chat IDs

        Returns:
            True if deleted, False if not found
        """
        async with self._lock:
            deleted = await self._repo.delete_chats(chat_ids)

            if deleted:
                if self._archive_store is not None:
                    for chat_id in chat_ids:
                        await self._archive_store.delete_chat(chat_id)
                logger.debug(f"Deleted chats: {chat_ids}")

            return deleted

    async def count_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> int:
        """Count chats matching filters.

        Args:
            user_id: Optional user ID filter
            channel: Optional channel filter

        Returns:
            Number of matching chats
        """
        async with self._lock:
            chats = await self._repo.filter_chats(
                user_id=user_id,
                channel=channel,
            )
            return len(chats)

    async def get_chat_id_by_session(
        self,
        session_id: str,
        channel: str,
    ) -> str | None:
        """Get chat_id by session_id and channel.

        Args:
            session_id: Normalized session ID (e.g. "console:user1")
            channel: Channel name

        Returns:
            chat_id (UUID) of most recent chat if found, None otherwise

        Note:
            Returns most recently updated chat if multiple matches exist.
            O(N) scan of active chats. Future optimization: add index.
        """
        async with self._lock:
            chats = await self._repo.filter_chats(channel=channel)
            matching_chats = [
                chat for chat in chats if chat.session_id == session_id
            ]

            if not matching_chats:
                logger.debug(
                    f"No chat found for session={session_id[:30]} "
                    f"channel={channel}",
                )
                return None

            most_recent = max(matching_chats, key=lambda c: c.updated_at)
            logger.debug(
                f"Found chat_id={most_recent.id} "
                f"for session={session_id[:30]} "
                f"(from {len(matching_chats)} matches)",
            )
            return most_recent.id
