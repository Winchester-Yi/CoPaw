# -*- coding: utf-8 -*-
"""Synthetic offline migration and rollback rehearsals through the real CLI."""

import asyncio
from copy import deepcopy
import hashlib
import json

from agentscope.message import Msg
from click.testing import CliRunner

from swe.agents.memory.chat_checkpoint import CheckpointEvent, CheckpointRecord
from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.runner.session import SafeJSONSession
from swe.cli.memory_storage_cmd import memory_storage_group

CHAT = "16374f34-c02e-4bb9-87bf-eb5e64348cef"


def digest(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tree(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(
            path.read_bytes(),
        ).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def cli(kind, options, operation, *extra):
    result = CliRunner().invoke(
        memory_storage_group,
        [kind, *options, operation, *extra],
    )
    assert result.exit_code == 0, (result.output, repr(result.exception))
    return json.loads(result.output)


def evidence(tmp_path, **values):
    (tmp_path / "rehearsal-evidence.json").write_text(
        json.dumps(values, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def normalized_session(value):
    value = deepcopy(value)
    value.pop("revision", None)
    return value


async def reopen_legacy_session(destination, expected, new_turn):
    legacy = SafeJSONSession(str(destination.parent))
    state = await legacy.get_session_state_dict("s")
    assert normalized_session(state) == expected
    async with legacy.execution("s") as tx:
        tx.state["turn_states"]["rollback-new"] = deepcopy(new_turn)
        await tx.commit_state(tx.state)
    return await legacy.get_session_state_dict("s")


async def reopen_legacy_journal(destination, current, expected_messages):
    legacy = ConversationArchiveStore(destination.parent)
    state = await legacy.read_checkpoint_state(CHAT)
    assert state == current
    page = await legacy.read_page(CHAT, limit=50)
    messages = list(page.messages)
    while page.has_more:
        page = await legacy.read_page(CHAT, limit=50, before=page.next_cursor)
        messages = [*page.messages, *messages]
    assert [message.to_dict() for message in messages] == expected_messages
    event = CheckpointEvent.new(
        sequence=25,
        epoch=3,
        type="message_added",
        facts={"message_id": "rollback-new"},
        source_refs=("message:rollback-new",),
    )
    await legacy.append_checkpoint_event(CHAT, event)
    assert (await legacy.read_checkpoint_state(CHAT)).events[-1] == event
    assert not (destination / "state-v2.json").exists()
    return event.to_dict()


def compare_journal_export(
    destination,
    current,
    expected_events,
    expected_messages,
    boundaries,
    expected_epochs,
    expected_evidence,
):
    exported_events = [
        json.loads(line)
        for line in (destination / "events.jsonl").read_text().splitlines()
    ]
    assert exported_events == expected_events
    assert digest(exported_events) == digest(expected_events)
    assert (
        json.loads((destination / "checkpoint.json").read_text())
        == current.record.to_dict()
    )
    assert (
        json.loads((destination / "epochs.json").read_text())
        == expected_epochs
    )
    assert expected_epochs["current_epoch"] == current.current_epoch == 3
    assert (
        json.loads((destination / "evidence-epochs.json").read_text())
        == expected_evidence
    )
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["boundaries"] == boundaries
    exported_messages = [
        json.loads(line)
        for boundary in boundaries
        for line in (destination / f"{boundary['id']}.jsonl")
        .read_text()
        .splitlines()
    ]
    assert exported_messages == expected_messages
    assert digest(exported_messages) == digest(expected_messages)
    legacy_files = {
        "checkpoint.json",
        "events.jsonl",
        "epochs.json",
        "evidence-epochs.json",
        "manifest.json",
    }
    legacy_files.update(
        f"{b['id']}{suffix}"
        for b in boundaries
        for suffix in (".jsonl", ".offsets")
    )
    assert {path.name for path in destination.iterdir()} == legacy_files
    return exported_events, exported_messages, manifest


def test_session_cli_migration_and_legacy_rollback(tmp_path, monkeypatch):
    monkeypatch.delenv("SWE_SESSION_V3_TENANTS", raising=False)
    sessions = tmp_path / "sessions"
    session = SafeJSONSession(str(sessions))
    original = {
        "agent": {"memory": {"summary": "合成记忆 🧪", "messages": []}},
        "chat_metadata": {"name": "回滚演练", "channel": "console"},
        "turn_states": {
            f"t{i}": {
                "status": (
                    ("admitted", "running", "unknown")[i % 3]
                    if i >= 80
                    else ("completed", "failed", "stopped")[i % 3]
                ),
                "message": {
                    "id": f"t{i}",
                    "role": "user",
                    "name": "合成用户",
                    "content": [
                        {"type": "text", "text": f"问题 {i} 🧪\n" * 8},
                    ],
                    "metadata": {"ordinal": i, "tags": ["演练", "UTF-8"]},
                },
                "created_at": f"2026-10-15T00:{i:02}:00Z",
                "chat_id": f"chat-{i % 2}",
                "response": {"text": f"答复 {i}", "tool_calls": [f"tool-{i}"]},
            }
            for i in range(86)
        },
    }
    asyncio.run(session.save_merged_state("s", state=original))
    options = ["--sessions-dir", str(sessions), "--session-id", "s"]
    before = tree(sessions)
    assert cli("session", options, "preview")["total"] == 86
    assert tree(sessions) == before
    assert cli("session", options, "migrate")["total"] == 86
    assert tree(sessions) == before  # Migration without --apply is a preview.
    cli("session", options, "migrate", "--apply")

    async def mutate():
        async with session.execution("s") as tx:
            assert tx.state["schema_version"] == 3
            assert "t0" not in tx.state["turn_states"]
            await tx.hydrate_turn("t0")
            tx.state["turn_states"]["t0"]["status"] = "stopped"
            tx.state["turn_states"]["t0"]["response"]["text"] = "冷记录更新 ✅"
            tx.state["turn_states"]["t86"] = deepcopy(
                original["turn_states"]["t85"],
            )
            tx.state["turn_states"]["t86"]["message"]["id"] = "t86"
            await tx.commit_state(tx.state)
        await session.delete_turn_state("s", "t2")

    asyncio.run(mutate())
    expected = deepcopy(original)
    expected["schema_version"] = 2
    expected["turn_states"]["t0"]["status"] = "stopped"
    expected["turn_states"]["t0"]["response"]["text"] = "冷记录更新 ✅"
    expected["turn_states"]["t86"] = deepcopy(original["turn_states"]["t85"])
    expected["turn_states"]["t86"]["message"]["id"] = "t86"
    del expected["turn_states"]["t2"]
    cli("session", options, "validate")
    destination = tmp_path / "rollback" / "s.json"
    source = tree(sessions)
    assert (
        cli("session", options, "export", "--destination", str(destination))[
            "total"
        ]
        == 86
    )
    assert tree(sessions) == source
    exported = json.loads(destination.read_text())
    assert normalized_session(exported) == expected
    assert digest(normalized_session(exported)) == digest(expected)
    assert not {
        "session_generation",
        "turn_ledger_version",
        "ledger_nonce",
        "next_admission_order",
        "cold_turn_count",
        "ledger_latest",
    }.intersection(exported)
    assert all(
        "admission_order" not in t for t in exported["turn_states"].values()
    )
    reopened = asyncio.run(
        reopen_legacy_session(
            destination,
            expected,
            original["turn_states"]["t0"],
        ),
    )
    assert reopened["schema_version"] == 2
    expected["turn_states"]["rollback-new"] = deepcopy(
        original["turn_states"]["t0"],
    )
    assert normalized_session(reopened) == expected
    evidence(
        tmp_path,
        format="session3-to-legacy2",
        initial_turns=86,
        exported_turns=86,
        reopened_turns=87,
        export_sha256=digest(normalized_session(exported)),
        reopened_sha256=digest(normalized_session(reopened)),
    )


def test_journal_cli_migration_and_legacy_rollback(tmp_path, monkeypatch):
    monkeypatch.delenv("SWE_CHECKPOINT_V2_TENANTS", raising=False)
    root = tmp_path / "dialog"
    store = ConversationArchiveStore(root)
    expected_events, expected_messages, boundaries = [], [], []
    expected_evidence = {}

    async def write_events(epoch, first, count):
        for sequence in range(first, first + count):
            event = CheckpointEvent.new(
                sequence=sequence,
                epoch=epoch,
                type="message_added",
                facts={
                    "message_id": f"e{epoch}-m{sequence}",
                    "role": "user",
                    "reason": f"合成事实 {sequence} 🧪",
                },
                source_refs=(
                    f"message:e{epoch}-m{sequence}",
                    f"archive:epoch-{epoch}",
                ),
                occurred_at="2026-10-15T00:00:00Z",
            )
            await store.append_checkpoint_event(CHAT, event)
            expected_events.append(event.to_dict())

    async def archive(count, label, epoch):
        messages = [
            Msg(
                "合成用户",
                f"{label} 消息 {i} 🧪",
                "user",
                metadata={"index": i, "batch": label},
            )
            for i in range(count)
        ]
        boundaries.append((await store.commit(CHAT, messages)).to_dict())
        expected_messages.extend(message.to_dict() for message in messages)
        expected_evidence.update(
            {
                f"{boundaries[-1]['id']}:{index}:{message.id}": epoch
                for index, message in enumerate(messages)
            },
        )

    async def legacy_setup():
        await write_events(1, 1, 30)
        await archive(36, "迁移前", 1)
        await store.reset_checkpoint_epoch(CHAT, reason="clear")
        await write_events(2, 1, 30)
        record = CheckpointRecord.new(chat_id=CHAT, epoch=2).with_current_task(
            "合成迁移演练",
            ["完整保留"],
            evidence_refs=("message:e2-m1",),
        )
        await store.write_active_checkpoint(CHAT, record)

    asyncio.run(legacy_setup())
    legacy_checkpoint = asyncio.run(store.read_checkpoint_state(CHAT))
    expected_epochs = json.loads(
        (store.path_for(CHAT) / "epochs.json").read_text(),
    )
    assert (
        json.loads((store.path_for(CHAT) / "evidence-epochs.json").read_text())
        == expected_evidence
    )
    assert len(expected_evidence) == 36
    options = ["--dialog-root", str(root), "--chat-id", CHAT]
    before = tree(root)
    assert cli("journal", options, "preview")["events"] == 60
    assert tree(root) == before
    assert cli("journal", options, "migrate")["events"] == 60
    assert tree(root) == before
    cli("journal", options, "migrate", "--apply")
    assert asyncio.run(store.read_checkpoint_state(CHAT)) == legacy_checkpoint

    async def new_writes():
        await write_events(2, 31, 4)
        await archive(44, "迁移后", 2)
        await store.reset_checkpoint_epoch(CHAT, reason="new")
        await write_events(3, 1, 24)
        return await store.read_checkpoint_state(CHAT)

    current = asyncio.run(new_writes())
    expected_epochs["current_epoch"] = 3
    expected_epochs["history"].append({"epoch": 3, "reason": "new"})
    cli("journal", options, "validate")
    destination = tmp_path / "legacy" / CHAT
    source = tree(root)
    assert (
        cli("journal", options, "export", "--destination", str(destination))[
            "events"
        ]
        == 88
    )
    assert tree(root) == source
    exported_events, exported_messages, manifest = compare_journal_export(
        destination,
        current,
        expected_events,
        expected_messages,
        boundaries,
        expected_epochs,
        expected_evidence,
    )
    appended = asyncio.run(
        reopen_legacy_journal(destination, current, expected_messages),
    )
    reopened_events = [
        json.loads(line)
        for line in (destination / "events.jsonl").read_text().splitlines()
    ]
    assert reopened_events == [*expected_events, appended]
    evidence(
        tmp_path,
        format="checkpoint2-to-legacy",
        epochs=3,
        exported_events=len(exported_events),
        reopened_events=len(reopened_events),
        archive_batches=len(boundaries),
        archive_messages=len(exported_messages),
        events_sha256=digest(exported_events),
        reopened_events_sha256=digest(reopened_events),
        messages_sha256=digest(exported_messages),
        checkpoint_sha256=digest(current.record.to_dict()),
        manifest_sha256=digest(manifest),
        epochs_sha256=digest(expected_epochs),
        evidence_epochs_sha256=digest(expected_evidence),
    )
