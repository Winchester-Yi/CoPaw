# -*- coding: utf-8 -*-
"""Tests for JSON chat repository runtime-state worker boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from swe.app.runner.models import ChatSpec, ChatsFile
from swe.app.runner.repo import json_repo
from swe.app.runner.repo.json_repo import JsonChatRepository


def _write_chats(path: Path, chats: list[ChatSpec]) -> None:
    path.write_text(
        json.dumps(ChatsFile(version=1, chats=chats).model_dump(mode="json")),
        encoding="utf-8",
    )


def _saved_chats_payload(chats: list[ChatSpec]) -> str:
    return json.dumps(
        ChatsFile(version=1, chats=chats).model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_chat_repo_creates_one_session_chat_across_repository_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    first = JsonChatRepository(path)
    second = JsonChatRepository(path)
    first_spec = ChatSpec(session_id="s1", user_id="u1", channel="console")
    second_spec = ChatSpec(session_id="s1", user_id="u1", channel="console")

    (first_chat, first_created), (second_chat, second_created) = (
        await asyncio.gather(
            first.create_chat_if_absent_by_session(first_spec),
            second.create_chat_if_absent_by_session(second_spec),
        )
    )

    assert first_created != second_created
    assert first_chat.id == second_chat.id
    assert len(await JsonChatRepository(path).list_chats()) == 1


@pytest.mark.asyncio
async def test_chat_repo_serializes_cross_instance_upserts_without_lost_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    first = JsonChatRepository(path)
    second = JsonChatRepository(path)
    first_spec = ChatSpec(session_id="s1", user_id="u1", channel="console")
    second_spec = ChatSpec(session_id="s2", user_id="u2", channel="console")

    await asyncio.gather(
        first.upsert_chat(first_spec),
        second.upsert_chat(second_spec),
    )

    chats = await JsonChatRepository(path).list_chats()
    assert {chat.session_id for chat in chats} == {"s1", "s2"}


@pytest.mark.asyncio
async def test_chat_repo_load_uses_runtime_state_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    _write_chats(
        path,
        [ChatSpec(session_id="s1", user_id="u1", channel="console")],
    )
    calls: list[str] = []
    operations: list[str] = []
    state = {"in_worker": False}

    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    original_loads = json.loads
    original_model_validate = ChatsFile.model_validate
    original_sha256 = json_repo.hashlib.sha256

    def guarded_read_bytes(self: Path):
        if self == path:
            assert state["in_worker"], "read_bytes ran outside runtime worker"
            operations.append("read")
        return original_read_bytes(self)

    def guarded_read_text(self: Path, *args, **kwargs):
        if self == path:
            raise AssertionError("load should not read text separately")
        return original_read_text(self, *args, **kwargs)

    def guarded_sha256(*args, **kwargs):
        assert state["in_worker"], "signature hash ran outside runtime worker"
        operations.append("hash")
        return original_sha256(*args, **kwargs)

    def guarded_loads(*args, **kwargs):
        assert state["in_worker"], "json.loads ran outside runtime worker"
        operations.append("parse")
        return original_loads(*args, **kwargs)

    def guarded_model_validate(cls, *args, **kwargs):
        assert state[
            "in_worker"
        ], "model validation ran outside runtime worker"
        operations.append("validate")
        return original_model_validate(*args, **kwargs)

    async def fake_worker(func, /, *args, **kwargs):
        calls.append(func.__name__)
        assert not state["in_worker"]
        state["in_worker"] = True
        try:
            return func(*args, **kwargs)
        finally:
            state["in_worker"] = False

    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.run_runtime_state_work",
        fake_worker,
    )
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(json_repo.hashlib, "sha256", guarded_sha256)
    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.json.loads",
        guarded_loads,
    )
    monkeypatch.setattr(
        ChatsFile,
        "model_validate",
        classmethod(guarded_model_validate),
    )

    repo = JsonChatRepository(path)
    loaded = await repo.load()

    assert [chat.session_id for chat in loaded.chats] == ["s1"]
    assert calls
    assert operations == ["read", "hash", "parse", "validate"]


@pytest.mark.asyncio
async def test_chat_repo_session_lookup_uses_indexed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    older = ChatSpec(session_id="session-1", user_id="u1", channel="console")
    newer = ChatSpec(session_id="session-1", user_id="u2", channel="console")
    newer.updated_at = older.updated_at.replace(
        microsecond=older.updated_at.microsecond + 1,
    )
    _write_chats(path, [older, newer])
    repo = JsonChatRepository(path)
    await repo.load()

    async def fail_filter(*args, **kwargs):
        raise AssertionError("indexed lookup must not scan via filter_chats")

    monkeypatch.setattr(repo, "filter_chats", fail_filter)

    assert (
        await repo.get_chat_id_by_session("session-1", "console") == newer.id
    )


@pytest.mark.asyncio
async def test_chat_repo_save_uses_runtime_state_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    calls: list[str] = []
    operations: list[str] = []
    state = {"in_worker": False}

    original_model_dump = ChatsFile.model_dump
    original_dumps = json.dumps
    original_write_text = Path.write_text
    original_move = shutil.move

    def guarded_model_dump(self: ChatsFile, *args, **kwargs):
        assert state["in_worker"], "model dump ran outside runtime worker"
        operations.append("dump")
        return original_model_dump(self, *args, **kwargs)

    def guarded_dumps(*args, **kwargs):
        assert state["in_worker"], "json.dumps ran outside runtime worker"
        operations.append("encode")
        return original_dumps(*args, **kwargs)

    def guarded_write_text(self: Path, *args, **kwargs):
        if self == path.with_suffix(path.suffix + ".tmp"):
            assert state["in_worker"], "write_text ran outside runtime worker"
            operations.append("write")
        return original_write_text(self, *args, **kwargs)

    def guarded_move(*args, **kwargs):
        assert state["in_worker"], "shutil.move ran outside runtime worker"
        operations.append("move")
        return original_move(*args, **kwargs)

    async def fake_worker(func, /, *args, **kwargs):
        calls.append(func.__name__)
        assert not state["in_worker"]
        state["in_worker"] = True
        try:
            return func(*args, **kwargs)
        finally:
            state["in_worker"] = False

    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.run_runtime_state_work",
        fake_worker,
    )
    monkeypatch.setattr(ChatsFile, "model_dump", guarded_model_dump)
    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.json.dumps",
        guarded_dumps,
    )
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.shutil.move",
        guarded_move,
    )

    repo = JsonChatRepository(path)
    await repo.save(
        ChatsFile(
            version=1,
            chats=[ChatSpec(session_id="s1", user_id="u1", channel="console")],
        ),
    )

    assert calls
    assert operations == ["dump", "encode", "write", "move"]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["chats"][0]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_chat_repo_get_chat_reuses_valid_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = JsonChatRepository(tmp_path / "chats.json")
    chat = ChatSpec(session_id="s1", user_id="u1", channel="console")
    await repo.save(ChatsFile(version=1, chats=[chat]))
    calls: list[str] = []
    state = {"in_worker": False}
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(self: Path):
        if self == repo.path:
            assert state[
                "in_worker"
            ], "signature digest ran outside runtime worker"
        return original_read_bytes(self)

    async def fail_worker(func, /, *args, **kwargs):
        calls.append(func.__name__)
        assert not state["in_worker"]
        state["in_worker"] = True
        if func.__name__ == "_load_and_prepare_snapshot_sync":
            raise AssertionError("snapshot should avoid full reload")
        try:
            return func(*args, **kwargs)
        finally:
            state["in_worker"] = False

    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.run_runtime_state_work",
        fail_worker,
    )
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    loaded = await repo.get_chat(chat.id)

    assert loaded is not None
    assert loaded.session_id == "s1"
    assert calls == ["_file_signature", "_copy_chat_sync"]


@pytest.mark.asyncio
async def test_chat_repo_snapshot_hit_does_not_read_chat_file_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    chat = ChatSpec(session_id="s1", user_id="u1", channel="console")
    repo = JsonChatRepository(path)
    await repo.save(ChatsFile(version=1, chats=[chat]))

    original_read_bytes = Path.read_bytes

    def fail_read_bytes(self: Path) -> bytes:
        if self == path:
            raise AssertionError("snapshot hit must not read file contents")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    loaded = await repo.get_chat(chat.id)

    assert loaded is not None
    assert loaded.id == chat.id


@pytest.mark.asyncio
async def test_chat_repo_filter_snapshot_hit_does_not_read_chat_file_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    chat = ChatSpec(session_id="s1", user_id="u1", channel="console")
    repo = JsonChatRepository(path)
    await repo.save(ChatsFile(version=1, chats=[chat]))
    original_read_bytes = Path.read_bytes

    def fail_read_bytes(self: Path) -> bytes:
        if self == path:
            raise AssertionError("snapshot hit must not read file contents")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    loaded = await repo.filter_chats(user_id="u1", channel="console")
    assert [item.id for item in loaded] == [chat.id]


@pytest.mark.asyncio
async def test_chat_repo_session_snapshot_hit_does_not_read_chat_file_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    chat = ChatSpec(session_id="s1", user_id="u1", channel="console")
    repo = JsonChatRepository(path)
    await repo.save(ChatsFile(version=1, chats=[chat]))
    original_read_bytes = Path.read_bytes

    def fail_read_bytes(self: Path) -> bytes:
        if self == path:
            raise AssertionError("snapshot hit must not read file contents")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    assert await repo.get_chat_id_by_session("s1", "console") == chat.id


@pytest.mark.asyncio
async def test_chat_repo_builds_snapshot_index_in_runtime_state_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    _write_chats(
        path,
        [ChatSpec(session_id="s1", user_id="u1", channel="console")],
    )
    state = {"in_worker": False}
    original_getattribute = ChatSpec.__getattribute__

    async def fake_worker(func, /, *args, **kwargs):
        assert not state["in_worker"]
        state["in_worker"] = True
        try:
            return func(*args, **kwargs)
        finally:
            state["in_worker"] = False

    def guarded_getattribute(self: ChatSpec, name: str):
        if name == "id":
            assert state[
                "in_worker"
            ], "chat index built outside runtime worker"
        return original_getattribute(self, name)

    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.run_runtime_state_work",
        fake_worker,
    )
    monkeypatch.setattr(ChatSpec, "__getattribute__", guarded_getattribute)

    repo = JsonChatRepository(path)
    loaded = await repo.load()

    assert [chat.session_id for chat in loaded.chats] == ["s1"]


@pytest.mark.asyncio
async def test_chat_repo_retries_when_file_changes_during_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    old_chat = ChatSpec(session_id="old", user_id="u1", channel="console")
    new_chat = ChatSpec(session_id="new", user_id="u1", channel="console")
    _write_chats(path, [old_chat])

    original_read_bytes = Path.read_bytes
    swapped = False

    def swapping_read_bytes(self: Path) -> bytes:
        nonlocal swapped
        contents = original_read_bytes(self)
        if self == path and not swapped:
            swapped = True
            _write_chats(path, [new_chat])
        return contents

    monkeypatch.setattr(Path, "read_bytes", swapping_read_bytes)

    repo = JsonChatRepository(path)
    await repo.load()

    assert await repo.get_chat(old_chat.id) is None
    loaded_new = await repo.get_chat(new_chat.id)
    assert loaded_new is not None
    assert loaded_new.session_id == "new"


@pytest.mark.asyncio
async def test_chat_repo_load_parses_the_same_bytes_used_for_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    old_chat = ChatSpec(session_id="old", user_id="u1", channel="console")
    new_chat = ChatSpec(session_id="new", user_id="u1", channel="console")
    _write_chats(path, [old_chat])
    new_payload = _saved_chats_payload([new_chat])
    read_text_calls = 0

    original_read_text = Path.read_text

    def mismatched_read_text(self: Path, *args, **kwargs):
        nonlocal read_text_calls
        if self == path:
            read_text_calls += 1
            return new_payload
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mismatched_read_text)

    repo = JsonChatRepository(path)
    loaded = await repo.load()

    assert read_text_calls == 0
    assert [chat.id for chat in loaded.chats] == [old_chat.id]
    assert await repo.get_chat(new_chat.id) is None
    loaded_old = await repo.get_chat(old_chat.id)
    assert loaded_old is not None
    assert loaded_old.session_id == "old"


@pytest.mark.asyncio
async def test_chat_repo_get_chat_invalidates_published_snapshot_after_external_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    old_chat = ChatSpec(session_id="old", user_id="u1", channel="console")
    new_chat = ChatSpec(
        session_id="new-session-after-external-rewrite",
        user_id="u1",
        channel="console",
    )

    repo = JsonChatRepository(path)
    await repo.save(ChatsFile(version=1, chats=[old_chat]))
    _write_chats(path, [new_chat])

    assert await repo.get_chat(old_chat.id) is None
    loaded_new = await repo.get_chat(new_chat.id)

    assert loaded_new is not None
    assert loaded_new.id == new_chat.id
    assert loaded_new.session_id == "new-session-after-external-rewrite"


@pytest.mark.asyncio
async def test_chat_repo_get_chat_invalidates_snapshot_after_same_size_rewrite_with_restored_mtime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    old_chat = ChatSpec(session_id="old", user_id="u1", channel="console")
    new_chat = old_chat.model_copy(
        update={
            "id": "00000000-0000-4000-8000-000000000000",
            "session_id": "new",
        },
    )

    repo = JsonChatRepository(path)
    await repo.save(ChatsFile(version=1, chats=[old_chat]))
    old_stat = path.stat()

    new_payload = _saved_chats_payload([new_chat])
    assert len(new_payload.encode("utf-8")) == old_stat.st_size
    path.write_text(new_payload, encoding="utf-8")
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    assert path.stat().st_size == old_stat.st_size
    assert path.stat().st_mtime_ns == old_stat.st_mtime_ns

    assert await repo.get_chat(old_chat.id) is None
    loaded_new = await repo.get_chat(new_chat.id)

    assert loaded_new is not None
    assert loaded_new.id == new_chat.id
    assert loaded_new.session_id == "new"


@pytest.mark.asyncio
async def test_chat_repo_load_retries_when_read_races_with_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    old_chat = ChatSpec(session_id="old", user_id="u1", channel="console")
    new_chat = old_chat.model_copy(
        update={
            "id": "00000000-0000-4000-8000-000000000000",
            "session_id": "new",
        },
    )

    repo = JsonChatRepository(path)
    await repo.save(ChatsFile(version=1, chats=[old_chat]))
    old_stat = path.stat()
    new_payload = _saved_chats_payload([new_chat])
    assert len(new_payload.encode("utf-8")) == old_stat.st_size

    original_read_bytes = Path.read_bytes
    swapped = False

    def swapping_read_bytes(self: Path) -> bytes:
        nonlocal swapped
        contents = original_read_bytes(self)
        if self == path and not swapped:
            swapped = True
            path.write_text(new_payload, encoding="utf-8")
            os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        return contents

    monkeypatch.setattr(Path, "read_bytes", swapping_read_bytes)

    await repo.load()
    assert await repo.get_chat(old_chat.id) is None
    loaded_new = await repo.get_chat(new_chat.id)

    assert loaded_new is not None
    assert loaded_new.id == new_chat.id
    assert loaded_new.session_id == "new"


@pytest.mark.asyncio
async def test_chat_repo_load_result_mutation_does_not_pollute_cache(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    chat = ChatSpec(session_id="s1", user_id="u1", channel="console")
    _write_chats(path, [chat])

    repo = JsonChatRepository(path)
    loaded = await repo.load()
    loaded.chats[0].session_id = "mutated"

    cached = await repo.get_chat(chat.id)

    assert cached is not None
    assert cached.session_id == "s1"


@pytest.mark.asyncio
async def test_chat_repo_save_input_mutation_does_not_pollute_cache(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    chat = ChatSpec(session_id="s1", user_id="u1", channel="console")
    chats_file = ChatsFile(version=1, chats=[chat])

    repo = JsonChatRepository(path)
    await repo.save(chats_file)
    chats_file.chats[0].session_id = "mutated"

    cached = await repo.get_chat(chat.id)

    assert cached is not None
    assert cached.session_id == "s1"


@pytest.mark.asyncio
async def test_chat_repo_get_chat_returns_copy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    chat = ChatSpec(session_id="s1", user_id="u1", channel="console")
    _write_chats(path, [chat])

    repo = JsonChatRepository(path)
    first = await repo.get_chat(chat.id)
    assert first is not None
    first.session_id = "mutated"
    first.meta["client"] = "changed"

    second = await repo.get_chat(chat.id)

    assert second is not None
    assert second.session_id == "s1"
    assert second.meta == {}


@pytest.mark.asyncio
async def test_chat_repo_load_missing_file_returns_empty_and_publishes_signature(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.json"
    repo = JsonChatRepository(path)

    loaded = await repo.load()

    assert loaded.chats == []
    assert repo._snapshot_signature == json_repo._FileSignature(exists=False)


@pytest.mark.asyncio
async def test_chat_repo_save_clears_snapshot_when_signature_is_unstable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    old_chat = ChatSpec(session_id="old", user_id="u1", channel="console")
    new_chat = ChatSpec(session_id="new", user_id="u1", channel="console")
    repo = JsonChatRepository(path)
    await repo.save(ChatsFile(version=1, chats=[old_chat]))
    assert repo._snapshot is not None

    original_save_sync = repo._save_sync

    def save_without_stable_signature(chats_file: ChatsFile) -> None:
        original_save_sync(chats_file)

    monkeypatch.setattr(repo, "_save_sync", save_without_stable_signature)

    await repo.save(ChatsFile(version=1, chats=[new_chat]))

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert [chat["session_id"] for chat in persisted["chats"]] == ["new"]
    assert repo._snapshot_signature is None
    assert repo._snapshot is None
    assert repo._chat_index == {}


@pytest.mark.asyncio
async def test_chat_repo_load_raises_when_all_stable_read_attempts_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    _write_chats(
        path,
        [ChatSpec(session_id="existing", user_id="u1", channel="console")],
    )
    repo = JsonChatRepository(path)

    monkeypatch.setattr(repo, "_read_file_state", lambda: None)

    with pytest.raises(RuntimeError, match="unstable"):
        await repo.load()


@pytest.mark.asyncio
async def test_chat_repo_get_chat_propagates_unstable_load_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    chat = ChatSpec(session_id="existing", user_id="u1", channel="console")
    _write_chats(path, [chat])
    repo = JsonChatRepository(path)

    monkeypatch.setattr(repo, "_read_file_state", lambda: None)

    with pytest.raises(RuntimeError, match="unstable"):
        await repo.get_chat(chat.id)


@pytest.mark.asyncio
async def test_chat_repo_upsert_does_not_save_after_unstable_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    existing = ChatSpec(
        session_id="existing",
        user_id="u1",
        channel="console",
    )
    incoming = ChatSpec(session_id="incoming", user_id="u1", channel="console")
    _write_chats(path, [existing])
    original_payload = path.read_text(encoding="utf-8")
    repo = JsonChatRepository(path)

    def fail_save(_chats_file: ChatsFile):
        raise AssertionError("unstable load must not save empty state")

    monkeypatch.setattr(repo, "_read_file_state", lambda: None)
    monkeypatch.setattr(repo, "_save_sync", fail_save)

    with pytest.raises(RuntimeError, match="unstable"):
        await repo.upsert_chat(incoming)

    assert path.read_text(encoding="utf-8") == original_payload


@pytest.mark.asyncio
async def test_chat_repo_delete_chats_does_not_save_after_unstable_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "chats.json"
    existing = ChatSpec(
        session_id="existing",
        user_id="u1",
        channel="console",
    )
    _write_chats(path, [existing])
    original_payload = path.read_text(encoding="utf-8")
    repo = JsonChatRepository(path)

    def fail_save(_chats_file: ChatsFile):
        raise AssertionError("unstable load must not save empty state")

    monkeypatch.setattr(repo, "_read_file_state", lambda: None)
    monkeypatch.setattr(repo, "_save_sync", fail_save)

    with pytest.raises(RuntimeError, match="unstable"):
        await repo.delete_chats([existing.id])

    assert path.read_text(encoding="utf-8") == original_payload
