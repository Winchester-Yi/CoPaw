# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

import pytest
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


async def test_delete_chats_removes_scenario_private_resources(
    tmp_path,
) -> None:
    repo = _InMemoryChatRepo()
    manager = ChatManager(repo=repo, resource_root=tmp_path / "scenario")
    chat = await manager.get_or_create_chat("session-1", "user-1")
    private_root = tmp_path / "scenario" / chat.id
    private_root.mkdir(parents=True)
    (private_root / "SKILL.md").write_text("private", encoding="utf-8")

    assert await manager.delete_chats([chat.id]) is True
    assert not private_root.exists()


async def test_delete_chats_releases_expert_dependency_view(tmp_path) -> None:
    repo = _InMemoryChatRepo()
    manager = ChatManager(
        repo=repo,
        resource_root=tmp_path / "scenario",
        expert_dependency_root=tmp_path,
    )
    chat = await manager.get_or_create_chat("session-1", "user-1")
    expert_view = (
        tmp_path
        / ".expert_sessions"
        / chat.id
        / "00000000-0000-0000-0000-000000000040"
    )
    expert_view.mkdir(parents=True)
    (expert_view / "SKILL.md").write_text("private", encoding="utf-8")

    assert await manager.delete_chats([chat.id]) is True
    assert not expert_view.exists()


async def test_delete_chats_releases_expert_view_without_scenario_root(
    tmp_path,
) -> None:
    repo = _InMemoryChatRepo()
    manager = ChatManager(repo=repo, expert_dependency_root=tmp_path)
    chat = await manager.get_or_create_chat("session-1", "user-1")
    expert_view = (
        tmp_path
        / ".expert_sessions"
        / chat.id
        / "00000000-0000-0000-0000-000000000041"
    )
    expert_view.mkdir(parents=True)

    assert await manager.delete_chats([chat.id]) is True
    assert not expert_view.exists()


async def test_delete_chats_never_removes_resource_root_for_invalid_id(
    tmp_path,
) -> None:
    repo = _InMemoryChatRepo()
    manager = ChatManager(repo=repo, resource_root=tmp_path / "scenario")
    chat = await manager.get_or_create_chat("session-1", "user-1")
    private_root = tmp_path / "scenario" / chat.id
    private_root.mkdir(parents=True)

    assert await manager.delete_chats([chat.id, "."]) is True
    assert (tmp_path / "scenario").exists()


async def test_get_or_create_scenario_chat_keeps_first_snapshot_under_race() -> (
    None
):
    repo = _InMemoryChatRepo()
    manager = ChatManager(repo=repo)
    first_factory_started = asyncio.Event()
    release_first_factory = asyncio.Event()
    factories_called: list[str] = []

    async def first_factory(_chat: ChatSpec) -> dict[str, str]:
        factories_called.append("first")
        first_factory_started.set()
        await release_first_factory.wait()
        return {"scenario_id": "scenario-first"}

    async def second_factory(_chat: ChatSpec) -> dict[str, str]:
        factories_called.append("second")
        return {"scenario_id": "scenario-second"}

    first = asyncio.create_task(
        manager.get_or_create_scenario_chat(
            "session-1",
            "user-1",
            "console",
            "scenario",
            {},
            first_factory,
        ),
    )
    await first_factory_started.wait()
    second = asyncio.create_task(
        manager.get_or_create_scenario_chat(
            "session-1",
            "user-1",
            "console",
            "scenario",
            {},
            second_factory,
        ),
    )
    release_first_factory.set()
    (first_chat, first_created), (second_chat, second_created) = (
        await asyncio.gather(
            first,
            second,
        )
    )

    assert first_created is True
    assert second_created is False
    assert first_chat.id == second_chat.id
    assert first_chat.meta["scenario_preset_snapshot"] == {
        "scenario_id": "scenario-first",
    }
    assert factories_called == ["first"]


async def test_get_or_create_scenario_chat_cleans_private_resources_on_failure(
    tmp_path,
) -> None:
    repo = _InMemoryChatRepo()
    resource_root = tmp_path / "scenario"
    manager = ChatManager(repo=repo, resource_root=resource_root)

    async def failing_factory(chat: ChatSpec) -> dict[str, str]:
        leaked_path = resource_root / chat.id / "mcp-1" / "mcp.json"
        leaked_path.parent.mkdir(parents=True)
        leaked_path.write_text("{}", encoding="utf-8")
        raise ValueError("preset unavailable")

    with pytest.raises(ValueError, match="preset unavailable"):
        await manager.get_or_create_scenario_chat(
            "session-1",
            "user-1",
            "console",
            "scenario",
            {},
            failing_factory,
        )

    assert await repo.get_chat_by_id("session-1", "user-1", "console") is None
    assert not resource_root.exists() or list(resource_root.iterdir()) == []


@pytest.mark.parametrize("entry", ["manager", "runner"])
async def test_automatic_title_rejects_concurrent_metadata_change(entry):
    from src.swe.app.conversation_storage.errors import ConflictError

    class ConcurrentRepo(_InMemoryChatRepo):
        async def update_chat_if_unchanged(self, spec, expected_chat):
            current = await self.get_chat(spec.id)
            current.meta["concurrent"] = "preserve"
            await self.upsert_chat(current)
            assert expected_chat.name == "hello"
            assert "session_title_generated" not in expected_chat.meta
            raise ConflictError("Chat changed since patch was read")

    repo = ConcurrentRepo()
    manager = ChatManager(repo=repo)
    chat = await manager.get_or_create_chat("session", "user", name="hello")
    if entry == "manager":
        with pytest.raises(ConflictError):
            await manager.update_chat_name(
                chat.id,
                "generated",
                meta={"session_title_generated": True},
            )
    else:
        from types import SimpleNamespace
        from src.swe.app.runner.runner import AgentRunner

        request = SimpleNamespace(channel_meta={"session_title": "generated"})
        await AgentRunner._persist_session_title(
            SimpleNamespace(_chat_manager=manager),
            request=request,
            title="generated",
            trace_id="trace",
            chat_id=chat.id,
        )
        assert "session_title" not in request.channel_meta
    stored = await repo.get_chat(chat.id)
    assert stored.name == "hello"
    assert stored.meta["concurrent"] == "preserve"
