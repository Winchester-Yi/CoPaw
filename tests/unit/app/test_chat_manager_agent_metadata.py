# -*- coding: utf-8 -*-
from __future__ import annotations

from agentscope.message import Msg

from src.swe.app.runner.manager import ChatManager
from src.swe.app.runner.models import ChatSpec, ChatsFile
from src.swe.app.runner.repo import BaseChatRepository
from swe.agents.memory.conversation_archive import ConversationArchiveStore


class _InMemoryChatRepo(BaseChatRepository):
    def __init__(self) -> None:
        self._state = ChatsFile(version=1, chats=[])
        self.path = "<memory>"

    async def load(self) -> ChatsFile:
        return self._state.model_copy(deep=True)

    async def save(self, chats_file: ChatsFile) -> None:
        self._state = chats_file.model_copy(deep=True)

    async def get_chat(self, chat_id: str) -> ChatSpec | None:
        return await ChatsFileRepoAdapter(self).get_chat(chat_id)

    async def get_chat_by_id(
        self,
        session_id: str,
        user_id: str,
        channel: str = "console",
    ) -> ChatSpec | None:
        return await ChatsFileRepoAdapter(self).get_chat_by_id(
            session_id,
            user_id,
            channel,
        )

    async def upsert_chat(self, spec: ChatSpec) -> None:
        await ChatsFileRepoAdapter(self).upsert_chat(spec)

    async def filter_chats(self, user_id=None, channel=None) -> list[ChatSpec]:
        return await ChatsFileRepoAdapter(self).filter_chats(
            user_id=user_id,
            channel=channel,
        )


class ChatsFileRepoAdapter:
    def __init__(self, repo: _InMemoryChatRepo) -> None:
        self._repo = repo

    async def get_chat(self, chat_id: str):
        chats_file = await self._repo.load()
        for chat in chats_file.chats:
            if chat.id == chat_id:
                return chat
        return None

    async def get_chat_by_id(
        self,
        session_id: str,
        user_id: str,
        channel: str,
    ):
        chats_file = await self._repo.load()
        for chat in chats_file.chats:
            if (
                chat.session_id == session_id
                and chat.user_id == user_id
                and chat.channel == channel
            ):
                return chat
        return None

    async def upsert_chat(self, spec: ChatSpec) -> None:
        chats_file = await self._repo.load()
        for index, chat in enumerate(chats_file.chats):
            if chat.id == spec.id:
                chats_file.chats[index] = spec
                break
        else:
            chats_file.chats.append(spec)
        await self._repo.save(chats_file)

    async def filter_chats(self, user_id=None, channel=None):
        chats_file = await self._repo.load()
        chats = chats_file.chats
        if user_id is not None:
            chats = [chat for chat in chats if chat.user_id == user_id]
        if channel is not None:
            chats = [chat for chat in chats if chat.channel == channel]
        return chats


async def test_get_or_create_chat_stores_agent_metadata_for_new_chat() -> None:
    manager = ChatManager(repo=_InMemoryChatRepo())

    chat = await manager.get_or_create_chat(
        "session-1",
        "user-1",
        "console",
        name="hello",
        meta={"agent_id": "agent-a"},
    )

    assert chat.meta["agent_id"] == "agent-a"


async def test_get_or_create_chat_merges_agent_metadata_for_existing_chat() -> (
    None
):
    repo = _InMemoryChatRepo()
    manager = ChatManager(repo=repo)
    existing = await manager.get_or_create_chat(
        "session-1",
        "user-1",
        "console",
        name="hello",
        meta={"session_kind": "chat"},
    )

    chat = await manager.get_or_create_chat(
        "session-1",
        "user-1",
        "console",
        name="ignored",
        meta={"agent_id": "agent-b"},
    )

    assert chat.id == existing.id
    assert chat.meta == {
        "session_kind": "chat",
        "agent_id": "agent-b",
    }


async def test_update_chat_name_merges_title_metadata() -> None:
    """更新自动标题时，应保留已有 meta 并写入生成标记。"""
    repo = _InMemoryChatRepo()
    manager = ChatManager(repo=repo)
    chat = await manager.get_or_create_chat(
        "session-1",
        "user-1",
        "console",
        name="hello",
        meta={"agent_id": "agent-a"},
    )

    updated = await manager.update_chat_name(
        chat.id,
        "销售复盘",
        meta={"session_title_generated": True},
    )
    stored = await repo.get_chat(chat.id)

    assert updated is True
    assert stored is not None
    assert stored.name == "销售复盘"
    assert stored.meta == {
        "agent_id": "agent-a",
        "session_title_generated": True,
    }


async def test_update_generated_chat_title_preserves_manual_rename() -> None:
    """Automatic title writes must not overwrite a newer manual rename."""
    repo = _InMemoryChatRepo()
    manager = ChatManager(repo=repo)
    chat = await manager.get_or_create_chat(
        "session-1",
        "user-1",
        "console",
        name="New Chat",
    )
    await manager.update_chat_name(chat.id, "我的自定义标题")

    updated = await manager.update_generated_chat_title(
        chat.id,
        "自动生成标题",
        fallback_name="New Chat",
    )
    stored = await repo.get_chat(chat.id)

    assert updated is False
    assert stored is not None
    assert stored.name == "我的自定义标题"


async def test_delete_chats_removes_the_chat_scoped_archive(tmp_path) -> None:
    repo = _InMemoryChatRepo()
    archive_store = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"test-cursor-secret",
    )
    manager = ChatManager(repo=repo, archive_store=archive_store)
    chat = await manager.get_or_create_chat("session-1", "user-1")
    await archive_store.commit(
        chat.id,
        [
            Msg(
                name="user",
                role="user",
                content="archived",
            ),
        ],
    )

    assert await manager.delete_chats([chat.id]) is True
    assert await repo.get_chat(chat.id) is None
    assert not archive_store.path_for(chat.id).exists()
