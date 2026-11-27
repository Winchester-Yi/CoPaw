# -*- coding: utf-8 -*-
"""Tests for chat-scoped conversation compaction archives."""

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from agentscope.message import Msg

from swe.agents.memory import conversation_archive
from swe.agents.memory.conversation_archive import ConversationArchiveStore


def _message(index: int, *, timestamp: str | None = None) -> Msg:
    message = Msg(
        name="user",
        role="user",
        content=f"message-{index}",
        timestamp=timestamp or f"2026-08-01 12:00:{index:02d}.000",
    )
    message.id = f"message-{index}"
    return message


def _chat_id() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_commit_writes_immutable_batch_and_reads_a_display_safe_page(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    store = ConversationArchiveStore(tmp_path / "dialog")
    hidden = Msg(
        name="user",
        role="user",
        content="visible\n\n<CONSOLE-HIDDEN-CONTEXT>\ninternal\n</CONSOLE-HIDDEN-CONTEXT>",
        metadata={
            "console_hidden_context_v1": {
                "visible_text": "visible",
                "suffix": "\n\n<CONSOLE-HIDDEN-CONTEXT>\ninternal\n</CONSOLE-HIDDEN-CONTEXT>",
            },
        },
        timestamp="2026-08-01 12:00:01.000",
    )
    hidden.id = "message-hidden"
    messages = [_message(0), hidden, _message(2)]

    boundary = await store.commit(chat_id, messages)

    chat_dir = tmp_path / "dialog" / chat_id
    manifest = json.loads((chat_dir / "manifest.json").read_text())
    batch_path = chat_dir / f"{boundary.id}.jsonl"
    assert batch_path.is_file()
    assert [
        json.loads(line)["id"] for line in batch_path.read_text().splitlines()
    ] == [
        "message-0",
        "message-hidden",
        "message-2",
    ]
    assert manifest["boundaries"] == [boundary.to_dict()]
    assert boundary.chat_id == chat_id
    assert boundary.archived_message_count == 3
    assert boundary.first_message_id == "message-0"
    assert boundary.last_message_id == "message-2"
    assert boundary.first_timestamp == "2026-08-01 12:00:00.000"
    assert boundary.last_timestamp == "2026-08-01 12:00:02.000"

    page = await store.read_page(chat_id)

    assert [message.id for message in page.messages] == [
        "message-0",
        "message-hidden",
        "message-2",
    ]
    assert page.messages[1].content == "visible"
    assert "console_hidden_context_v1" not in page.messages[1].metadata
    assert page.boundaries == [boundary]
    assert page.has_more is False
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_archive_paths_are_isolated_by_canonical_chat_uuid(
    tmp_path: Path,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    first_chat_id = _chat_id()
    second_chat_id = _chat_id()
    await store.commit(first_chat_id, [_message(1)])
    await store.commit(second_chat_id, [_message(2)])

    assert [
        message.id
        for message in (await store.read_page(first_chat_id)).messages
    ] == [
        "message-1",
    ]
    assert [
        message.id
        for message in (await store.read_page(second_chat_id)).messages
    ] == [
        "message-2",
    ]

    with pytest.raises(ValueError):
        await store.read_page(f"{{{first_chat_id}}}")
    with pytest.raises(ValueError):
        await store.commit("../../other-chat", [_message(3)])
    with pytest.raises(ValueError):
        await store.delete_chat(first_chat_id.upper())


@pytest.mark.asyncio
async def test_manifest_cannot_traverse_into_another_chat_archive(
    tmp_path: Path,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    first_chat_id = _chat_id()
    second_chat_id = _chat_id()
    first_boundary = await store.commit(first_chat_id, [_message(1)])
    second_boundary = await store.commit(second_chat_id, [_message(2)])
    manifest_path = tmp_path / "dialog" / first_chat_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["boundaries"][0][
        "id"
    ] = f"../{second_chat_id}/{second_boundary.id}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    page = await store.read_page(first_chat_id)

    assert page.messages == []
    assert first_boundary.id != second_boundary.id


@pytest.mark.asyncio
async def test_concurrent_commits_preserve_each_visible_boundary(
    tmp_path: Path,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    chat_id = _chat_id()

    first_boundary, second_boundary = await asyncio.gather(
        store.commit(chat_id, [_message(1)]),
        store.commit(chat_id, [_message(2)]),
    )

    page = await store.read_page(chat_id)
    assert {boundary.id for boundary in page.boundaries} == {
        first_boundary.id,
        second_boundary.id,
    }
    assert {message.id for message in page.messages} == {
        "message-1",
        "message-2",
    }


@pytest.mark.asyncio
async def test_cursor_is_bound_to_its_chat_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    first_chat_id = _chat_id()
    second_chat_id = _chat_id()
    await store.commit(first_chat_id, [_message(index) for index in range(51)])
    await store.commit(second_chat_id, [_message(99)])

    page = await store.read_page(first_chat_id)
    assert page.next_cursor is not None

    with pytest.raises(
        ValueError,
        match="Invalid conversation archive cursor",
    ):
        await store.read_page(second_chat_id, before=page.next_cursor)
    with pytest.raises(
        ValueError,
        match="Invalid conversation archive cursor",
    ):
        await store.read_page(
            first_chat_id,
            before=page.next_cursor[:-1] + "x",
        )


@pytest.mark.asyncio
async def test_cursor_survives_a_store_instance_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = _chat_id()
    monkeypatch.setattr(
        conversation_archive,
        "SECRET_DIR",
        tmp_path / "secret",
    )
    monkeypatch.delenv("SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET", raising=False)
    conversation_archive._load_or_create_cursor_secret.cache_clear()
    try:
        first_store = ConversationArchiveStore(tmp_path / "dialog")
        await first_store.commit(
            chat_id,
            [_message(index) for index in range(51)],
        )
        first_page = await first_store.read_page(chat_id)
        assert first_page.next_cursor is not None

        restarted_store = ConversationArchiveStore(tmp_path / "dialog")
        next_page = await restarted_store.read_page(
            chat_id,
            before=first_page.next_cursor,
        )

        assert [message.id for message in next_page.messages] == ["message-0"]
    finally:
        conversation_archive._load_or_create_cursor_secret.cache_clear()


@pytest.mark.asyncio
async def test_boundary_allows_missing_message_timestamps(
    tmp_path: Path,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    message = _message(1)
    message.timestamp = None

    boundary = await store.commit(_chat_id(), [message])

    assert boundary.first_timestamp is None
    assert boundary.last_timestamp is None


@pytest.mark.asyncio
async def test_manifest_write_failure_leaves_batch_invisible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = _chat_id()
    store = ConversationArchiveStore(tmp_path / "dialog")

    def fail_manifest(*_args: object, **_kwargs: object) -> None:
        raise OSError("manifest unavailable")

    monkeypatch.setattr(store, "_replace_manifest", fail_manifest)

    with pytest.raises(OSError, match="manifest unavailable"):
        await store.commit(chat_id, [_message(1)])

    chat_dir = tmp_path / "dialog" / chat_id
    assert list(chat_dir.glob("*.jsonl"))
    assert not (chat_dir / "manifest.json").exists()
    assert (await store.read_page(chat_id)).messages == []


@pytest.mark.asyncio
async def test_read_page_skips_malformed_jsonl_rows_and_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = _chat_id()
    store = ConversationArchiveStore(tmp_path / "dialog")
    boundary = await store.commit(chat_id, [_message(1), _message(2)])
    batch_path = tmp_path / "dialog" / chat_id / f"{boundary.id}.jsonl"
    batch_path.write_text(
        json.dumps(_message(1).to_dict())
        + "\nnot-json\n"
        + json.dumps(_message(2).to_dict())
        + "\n",
        encoding="utf-8",
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        conversation_archive.logger,
        "warning",
        lambda message, *_args: warnings.append(message),
    )

    page = await store.read_page(chat_id)

    assert [message.id for message in page.messages] == [
        "message-1",
        "message-2",
    ]
    assert warnings == [
        "Skipping malformed conversation archive record %s:%d: %s",
    ]


@pytest.mark.asyncio
async def test_read_page_limits_to_fifty_and_emits_boundary_only_with_its_last_message(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    store = ConversationArchiveStore(tmp_path / "dialog")
    first_boundary = await store.commit(
        chat_id,
        [_message(index) for index in range(50)],
    )
    second_boundary = await store.commit(
        chat_id,
        [_message(index) for index in range(50, 55)],
    )

    first_page = await store.read_page(chat_id, limit=500)

    assert [message.id for message in first_page.messages] == [
        f"message-{index}" for index in range(5, 55)
    ]
    assert first_page.boundaries == [first_boundary, second_boundary]
    assert first_page.has_more is True
    assert first_page.next_cursor is not None

    second_page = await store.read_page(
        chat_id,
        before=first_page.next_cursor,
        limit=50,
    )

    assert [message.id for message in second_page.messages] == [
        f"message-{index}" for index in range(5)
    ]
    assert second_page.boundaries == []
    assert second_page.has_more is False
    assert second_page.next_cursor is None


@pytest.mark.asyncio
async def test_delete_chat_only_removes_the_validated_chat_archive(
    tmp_path: Path,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    deleted_chat_id = _chat_id()
    retained_chat_id = _chat_id()
    await store.commit(deleted_chat_id, [_message(1)])
    await store.commit(retained_chat_id, [_message(2)])

    await store.delete_chat(deleted_chat_id)

    assert not (tmp_path / "dialog" / deleted_chat_id).exists()
    assert (tmp_path / "dialog" / retained_chat_id / "manifest.json").is_file()
    assert [
        message.id
        for message in (await store.read_page(retained_chat_id)).messages
    ] == [
        "message-2",
    ]


@pytest.mark.asyncio
async def test_deleted_chat_archive_cannot_be_recreated(
    tmp_path: Path,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    chat_id = _chat_id()
    await store.commit(chat_id, [_message(1)])

    await store.delete_chat(chat_id)

    with pytest.raises(ValueError, match="has been deleted"):
        await store.commit(chat_id, [_message(2)])
    assert not (tmp_path / "dialog" / chat_id).exists()
    assert (tmp_path / "dialog" / ".deleted" / chat_id).is_file()


@pytest.mark.asyncio
async def test_evidence_occurrence_requires_unique_token_and_intact_archive(
    tmp_path,
):
    chat_id = _chat_id()
    store = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"test",
    )
    message = _message(1)
    message.metadata = {"agent_observability_evidence_ref": "ref-1"}
    boundary = await store.commit(chat_id, [message])
    found = await store.read_evidence_occurrence(
        chat_id,
        "ref-1",
        message.id,
        max_bytes=10000,
    )
    assert found["status"] == "available"
    assert found["message"].id == message.id
    assert (
        found["snapshot"]
        == (
            await store.read_evidence_occurrence(
                chat_id,
                "ref-1",
                message.id,
                max_bytes=10000,
            )
        )["snapshot"]
    )
    assert (
        await store.read_evidence_occurrence(
            chat_id,
            "ref-1",
            "wrong",
            max_bytes=10000,
        )
    )["status"] == "unresolved"
    assert (
        await store.read_evidence_occurrence(
            chat_id,
            "ref-1",
            message.id,
            max_bytes=1,
        )
    )["status"] == "truncated"
    duplicate = _message(2)
    duplicate.metadata = {"agent_observability_evidence_ref": "ref-1"}
    await store.commit(chat_id, [duplicate])
    assert (
        await store.read_evidence_occurrence(
            chat_id,
            "ref-1",
            message.id,
            max_bytes=10000,
        )
    )["status"] == "unresolved"
    (store.path_for(chat_id) / f"{boundary.id}.jsonl").unlink()
    assert (
        await store.read_evidence_occurrence(
            chat_id,
            "ref-1",
            message.id,
            max_bytes=10000,
        )
    )["status"] == "unavailable"
    await store.delete_chat(chat_id)
    assert (
        await store.read_evidence_occurrence(
            chat_id,
            "ref-1",
            message.id,
            max_bytes=10000,
        )
    )["status"] == "unavailable"


@pytest.mark.asyncio
async def test_evidence_snapshot_changes_when_archive_changes(tmp_path):
    chat_id = _chat_id()
    store = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"test",
    )
    message = _message(1)
    message.metadata = {"agent_observability_evidence_ref": "ref-1"}
    await store.commit(chat_id, [message])
    before = await store.read_evidence_occurrence(
        chat_id,
        "ref-1",
        message.id,
        max_bytes=10000,
    )
    await store.commit(chat_id, [_message(2)])
    after = await store.read_evidence_occurrence(
        chat_id,
        "ref-1",
        message.id,
        max_bytes=10000,
    )
    assert before["status"] == after["status"] == "available"
    assert before["snapshot"] != after["snapshot"]


@pytest.mark.asyncio
async def test_evidence_scan_serializes_with_delete(tmp_path, monkeypatch):
    import threading

    chat_id = _chat_id()
    store = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"test",
    )
    message = _message(1)
    message.metadata = {"agent_observability_evidence_ref": "ref-1"}
    await store.commit(chat_id, [message])
    entered, release = threading.Event(), threading.Event()
    scan = store._scan_evidence_occurrence

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return scan(*args)

    monkeypatch.setattr(store, "_scan_evidence_occurrence", blocked)
    reading = asyncio.create_task(
        store.read_evidence_occurrence(
            chat_id,
            "ref-1",
            message.id,
            max_bytes=10000,
        ),
    )
    assert await asyncio.to_thread(entered.wait, 5)
    deleting = asyncio.create_task(store.delete_chat(chat_id))
    await asyncio.sleep(0.05)
    assert not deleting.done()
    release.set()
    assert (await reading)["status"] == "available"
    await deleting
    assert (
        await store.read_evidence_occurrence(
            chat_id,
            "ref-1",
            message.id,
            max_bytes=10000,
        )
    )["status"] == "unavailable"


@pytest.mark.asyncio
async def test_evidence_occurrence_survives_checkpoint_format_migration(
    tmp_path,
):
    chat_id = _chat_id()
    store = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"test",
    )
    message = _message(1)
    message.metadata = {"agent_observability_evidence_ref": "ref-1"}
    await store.commit(chat_id, [message])
    before = await store.read_evidence_occurrence(
        chat_id,
        "ref-1",
        message.id,
        max_bytes=10000,
    )
    await asyncio.to_thread(store.migrate_checkpoint_v2, chat_id)
    after = await store.read_evidence_occurrence(
        chat_id,
        "ref-1",
        message.id,
        max_bytes=10000,
    )
    assert before["status"] == after["status"] == "available"
    assert before["snapshot"] == after["snapshot"]
