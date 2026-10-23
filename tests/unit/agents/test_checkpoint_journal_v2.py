# -*- coding: utf-8 -*-
import json
from dataclasses import replace

import pytest

from swe.agents.memory.chat_checkpoint import CheckpointEvent, CheckpointRecord
from swe.agents.memory.checkpoint_storage_v2 import CheckpointStorageV2

CHAT = "16374f34-c02e-4bb9-87bf-eb5e64348cef"


def event(seq, epoch=1):
    return CheckpointEvent.new(
        sequence=seq,
        epoch=epoch,
        type="message_added",
        facts={"message_id": str(seq), "role": "user"},
        source_refs=(f"message:{seq}",),
    )


def storage(tmp_path):
    value = CheckpointStorageV2(tmp_path)
    value.initialize(
        CheckpointRecord.new(chat_id=CHAT, epoch=1),
        {"boundaries": []},
        {},
        {},
    )
    return value


def test_append_snapshot_receipt_and_tail_recovery(tmp_path):
    value = storage(tmp_path)
    first = event(1)
    receipt = value.append(first)
    snapshot = value.head()
    assert value.append(first) == receipt
    with value.segment_path(snapshot).open("ab") as handle:
        handle.write(b"partial uncommitted tail")
    value.append(event(2))
    assert [e.sequence for e in value.events(snapshot)] == [1]
    assert [
        e.sequence for e in value.events(value.head(), after_sequence=1)
    ] == [2]
    with pytest.raises(ValueError, match="conflict"):
        value.append(event(1))


def test_segment_rotation_and_watermark_skips_old_segments(
    tmp_path,
    monkeypatch,
):
    value = storage(tmp_path)
    monkeypatch.setattr(value, "MAX_SEGMENT_EVENTS", 2)
    for seq in range(1, 8):
        value.append(event(seq))
    head = value.head()
    assert head["sealed_segment_count"] == 3
    value.segment_path(head, ordinal=0).unlink()
    assert [e.sequence for e in value.events(head, after_sequence=6)] == [7]
    with pytest.raises(ValueError, match="journal"):
        list(value.events(head))
    assert value.quarantine_path.exists()
    with pytest.raises(ValueError, match="quarantined"):
        value.append(event(8))


def test_corrupt_committed_frame_quarantines_other_instance(tmp_path):
    value = storage(tmp_path)
    value.append(event(1))
    path = value.segment_path(value.head())
    payload = path.read_bytes().replace(b"message_added", b"message_alter")
    path.write_bytes(payload)
    with pytest.raises(ValueError, match="journal"):
        value.append(event(2))
    with pytest.raises(ValueError, match="quarantined"):
        CheckpointStorageV2(tmp_path).publish(
            record=CheckpointRecord.new(chat_id=CHAT, epoch=1),
        )


def test_atomic_metadata_and_epoch_chain(tmp_path, monkeypatch):
    value = storage(tmp_path)
    value.append(event(1))
    prior = value.head()
    record = replace(value.record(prior), revision=1, applied_event_sequence=1)
    value.publish(record=record, manifest={"boundaries": [{"id": "batch"}]})
    assert value.record(value.head()).revision == 1
    value.reset(CheckpointRecord.new(chat_id=CHAT, epoch=2), "clear")
    assert value.head()["epoch"] == 2
    assert value.head()["last_event_sequence"] == 0
    assert value.previous_head(value.head())["epoch"] == 1
    assert [
        e.sequence for e in value.events(value.previous_head(value.head()))
    ] == [1]


def test_oversized_event_rejected_without_publication(tmp_path):
    value = storage(tmp_path)
    large = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="fact",
        facts={"x": "x" * 65536},
    )
    with pytest.raises(ValueError, match="64 KiB"):
        value.append(large)
    assert value.head()["last_event_sequence"] == 0


@pytest.mark.asyncio
async def test_store_v2_routes_atomic_commit_and_reset(tmp_path, monkeypatch):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    assert store.migrate_checkpoint_v2(CHAT, dry_run=True)["events"] == 0
    assert not (store.path_for(CHAT) / "state-v2.json").exists()
    store.migrate_checkpoint_v2(CHAT)
    monkeypatch.setattr(
        store,
        "_read_checkpoint_events",
        lambda p: (_ for _ in ()).throw(AssertionError("full reader")),
    )
    await store.append_checkpoint_event(CHAT, event(1))
    head = await store.read_checkpoint_head(CHAT)
    assert head["last_event_sequence"] == 1
    boundary = await store.commit(CHAT, [Msg("user", "hello", "user")])
    page = await store.read_page(CHAT)
    assert page.boundaries[0].id == boundary.id
    await store.reset_checkpoint_epoch(CHAT, reason="clear")
    assert (await store.read_checkpoint_state(CHAT)).current_epoch == 2
    assert len((await store.read_page(CHAT)).messages) == 1
    destination = tmp_path / "export"
    result = store.export_checkpoint_legacy(CHAT, destination)
    assert result["events"] == 1
    assert (
        json.loads((destination / "epochs.json").read_text())["current_epoch"]
        == 2
    )


@pytest.mark.asyncio
async def test_store_facts_assigns_sequence_without_record_read(
    tmp_path,
    monkeypatch,
):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    monkeypatch.setattr(
        CheckpointStorageV2,
        "record",
        lambda *a: (_ for _ in ()).throw(AssertionError("record read")),
    )
    receipt = await store.append_checkpoint_facts(
        CHAT,
        epoch=1,
        type="message_added",
        facts={"message_id": "x"},
        source_refs=("message:x",),
    )
    assert receipt[1] == 1


@pytest.mark.asyncio
async def test_archive_page_never_hydrates_whole_batch(tmp_path, monkeypatch):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    await store.commit(CHAT, [Msg("user", str(i), "user") for i in range(200)])
    monkeypatch.setattr(
        store,
        "_read_batch",
        lambda *a: (_ for _ in ()).throw(AssertionError("full batch")),
    )
    first = await store.read_page(CHAT, limit=5)
    assert [m.content for m in first.messages] == [
        str(i) for i in range(195, 200)
    ]
    second = await store.read_page(CHAT, before=first.next_cursor, limit=5)
    assert [m.content for m in second.messages] == [
        str(i) for i in range(190, 195)
    ]


def test_failed_rotation_publication_rebuilds_stale_sidecar(
    tmp_path,
    monkeypatch,
):
    value = storage(tmp_path)
    monkeypatch.setattr(value, "MAX_SEGMENT_EVENTS", 1)
    value.append(event(1))
    original = value._publish

    def fail(head):
        raise OSError("root publication failed")

    monkeypatch.setattr(value, "_publish", fail)
    with pytest.raises(OSError):
        value.append(event(2))
    assert value.head()["last_event_sequence"] == 1
    monkeypatch.setattr(value, "_publish", original)
    value.append(event(2))
    value.append(event(3))
    assert [item.sequence for item in value.events(value.head())] == [1, 2, 3]


def test_legacy_export_rejection_leaves_archive_writable(
    tmp_path,
    monkeypatch,
):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    monkeypatch.delenv("SWE_CHECKPOINT_V2_TENANTS", raising=False)
    store = ConversationArchiveStore(tmp_path / "dialog")
    store._append_checkpoint_event(CHAT, event(1))
    source = store.path_for(CHAT)
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    destination = tmp_path / "export"

    with pytest.raises(ValueError):
        store.export_checkpoint_legacy(CHAT, destination)

    assert {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    } == before
    assert not destination.exists()
    assert not list(tmp_path.glob(".export.export-*"))
    store._append_checkpoint_event(CHAT, event(2))
    assert [
        item.sequence for item in store._read_checkpoint_state(CHAT).events
    ] == [
        1,
        2,
    ]


def test_legacy_migration_strict_and_export_includes_new_epochs(tmp_path):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    store._append_checkpoint_event(CHAT, event(1))
    store._reset_checkpoint_epoch(CHAT, "clear")
    store._append_checkpoint_event(CHAT, event(1, 2))
    original = (store.path_for(CHAT) / "events.jsonl").read_bytes()
    report = store.migrate_checkpoint_v2(CHAT, dry_run=True)
    assert report["events"] == 2
    assert (store.path_for(CHAT) / "events.jsonl").read_bytes() == original
    store.migrate_checkpoint_v2(CHAT)
    store._append_checkpoint_event(CHAT, event(2, 2))
    store._reset_checkpoint_epoch(CHAT, "new")
    store._append_checkpoint_event(CHAT, event(1, 3))
    destination = tmp_path / "legacy"
    assert store.export_checkpoint_legacy(CHAT, destination)["events"] == 4
    events = [
        json.loads(line)
        for line in (destination / "events.jsonl").read_bytes().splitlines()
    ]
    assert [(e["epoch"], e["sequence"]) for e in events] == [
        (1, 1),
        (2, 1),
        (2, 2),
        (3, 1),
    ]
    corrupt = ConversationArchiveStore(tmp_path / "bad")
    path = corrupt.path_for(CHAT)
    path.mkdir(parents=True)
    (path / "events.jsonl").write_text(
        json.dumps(event(1).to_dict()) + "\nnot-json\n",
    )
    with pytest.raises(ValueError):
        corrupt.migrate_checkpoint_v2(CHAT, dry_run=True)
    assert not (path / "state-v2.json").exists()


def test_migration_failed_before_root_can_retry_without_data_loss(
    tmp_path,
    monkeypatch,
):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    store._append_checkpoint_event(CHAT, event(1))
    original = CheckpointStorageV2.append
    monkeypatch.setattr(
        CheckpointStorageV2,
        "append",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk failure")),
    )
    with pytest.raises(OSError):
        store.migrate_checkpoint_v2(CHAT)
    assert not (store.path_for(CHAT) / "state-v2.json").exists()
    assert len(store._read_checkpoint_state(CHAT).events) == 1
    monkeypatch.setattr(CheckpointStorageV2, "append", original)
    store.migrate_checkpoint_v2(CHAT)
    assert len(store._read_checkpoint_state(CHAT).events) == 1


@pytest.mark.asyncio
async def test_worker_cancellation_waits_for_durable_transaction(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import threading
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    entered, release = threading.Event(), threading.Event()
    original = CheckpointStorageV2.append

    def blocked(value, item):
        entered.set()
        assert release.wait(5)
        return original(value, item)

    monkeypatch.setattr(CheckpointStorageV2, "append", blocked)
    task = asyncio.create_task(store.append_checkpoint_event(CHAT, event(1)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.read_checkpoint_head(CHAT))["last_event_sequence"] == 1


def test_segment_gap_between_valid_frames_quarantines(tmp_path, monkeypatch):
    value = storage(tmp_path)
    monkeypatch.setattr(value, "MAX_SEGMENT_EVENTS", 1)
    for sequence in range(1, 4):
        value.append(event(sequence))
    head = value.head()
    # Simulate a sidecar whose range no longer joins its predecessor.
    path = value.segment_path(head, 1).with_suffix(".meta.json")
    meta = json.loads(path.read_bytes())
    meta["first_sequence"] = 99
    path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="journal"):
        list(value.events(head))


def test_two_processes_allocate_sequences_under_same_flock(tmp_path):
    from contextlib import ExitStack
    import subprocess
    import sys
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    source = """
import asyncio, sys
from swe.agents.memory.conversation_archive import ConversationArchiveStore
store = ConversationArchiveStore(sys.argv[1])
async def write():
    for index in range(8):
        await store.append_checkpoint_facts(sys.argv[2], epoch=1, type='message_added', facts={'message_id': str(index)}, source_refs=(f'message:{index}',))
asyncio.run(write())
"""
    with ExitStack() as stack:
        processes = [
            stack.enter_context(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        source,
                        str(tmp_path / "dialog"),
                        CHAT,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                _, error = process.communicate(timeout=15)
                assert process.returncode == 0, error.decode()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    assert [
        item.sequence for item in store._read_checkpoint_state(CHAT).events
    ] == list(range(1, 17))


def test_migrates_legacy_oversized_event_without_truncating(tmp_path):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    large = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="fact",
        facts={"message_id": "x" * 70000},
        source_refs=("message:x",),
    )
    store._append_checkpoint_event(CHAT, large)
    assert store.migrate_checkpoint_v2(CHAT)["oversized_events"] == 1
    assert store._read_checkpoint_state(CHAT).events == (large,)
    with pytest.raises(ValueError, match="64 KiB"):
        store._append_checkpoint_event(CHAT, replace(large, sequence=2))


def test_failed_atomic_metadata_publish_preserves_old_view(
    tmp_path,
    monkeypatch,
):
    value = storage(tmp_path)
    prior = value.head()
    original = value._publish
    monkeypatch.setattr(
        value,
        "_publish",
        lambda *a: (_ for _ in ()).throw(OSError("root failure")),
    )
    with pytest.raises(OSError):
        value.publish(manifest={"boundaries": [{"id": "uncommitted"}]})
    assert value.head() == prior
    assert value.read_object(value.head()["manifest"]) == {"boundaries": []}
    monkeypatch.setattr(value, "_publish", original)
    value.publish(manifest={"boundaries": [{"id": "committed"}]})
    assert (
        value.read_object(value.head()["manifest"])["boundaries"][0]["id"]
        == "committed"
    )


@pytest.mark.parametrize("total", [10000, 100000])
def test_append_cost_bounded_by_active_segment_for_large_history(
    tmp_path,
    monkeypatch,
    total,
):
    import hashlib
    from swe.agents.memory.checkpoint_storage_v2 import (
        atomic_bytes,
        canonical_bytes,
    )
    from swe.performance_metrics import PerformanceMetrics

    value = storage(tmp_path)
    head = value.head()
    base = event(1)
    ordinal = 0
    first_sequence = 1
    while first_sequence <= total:
        count = min(512, total - first_sequence + 1)
        frames = bytearray()
        for sequence in range(first_sequence, first_sequence + count):
            payload = replace(base, sequence=sequence).to_dict()
            encoded = canonical_bytes(payload)
            frames.extend(
                canonical_bytes(
                    {
                        "payload": payload,
                        "length": len(encoded),
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    },
                )
                + b"\n",
            )
        atomic_bytes(value.segment_path(head, ordinal), bytes(frames))
        if first_sequence + count <= total:
            meta = {
                "ordinal": ordinal,
                "bytes": len(frames),
                "count": count,
                "first_sequence": first_sequence,
                "last_sequence": first_sequence + count - 1,
                "sha256": hashlib.sha256(frames).hexdigest(),
            }
            atomic_bytes(
                value.segment_path(head, ordinal).with_suffix(".meta.json"),
                canonical_bytes(meta),
            )
            ordinal += 1
        first_sequence += count
    head.update(
        last_event_sequence=total,
        active_segment_ordinal=ordinal,
        sealed_segment_count=ordinal,
        committed_bytes=len(frames),
        active_count=count,
        active_first_sequence=total - count + 1,
    )
    value._publish(head)
    metrics = PerformanceMetrics()
    monkeypatch.setattr("swe.performance_metrics.METRICS", metrics)
    value.append(replace(base, sequence=total + 1))
    counters = metrics.snapshot()["counters"]
    assert counters["journal_frames_decoded"] <= 512
    assert counters["journal_read_bytes"] <= value.MAX_SEGMENT_BYTES
    assert value.head()["last_event_sequence"] == total + 1


def test_conditional_retry_requires_same_canonical_payload_hash(tmp_path):
    value = storage(tmp_path)
    original = replace(event(1), facts={"token_count": 1})
    receipt = value.append(original)
    with pytest.raises(ValueError, match="conflict"):
        value.append(replace(original, facts={"token_count": 1.0}))
    assert value.append(original) == receipt


@pytest.mark.asyncio
async def test_deleted_chat_cannot_be_revived_by_any_metadata_writer(tmp_path):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.agents.memory.chat_checkpoint import PrecompactionCandidate

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    record = CheckpointRecord.new(chat_id=CHAT, epoch=1)
    candidate = PrecompactionCandidate.new(
        record=record,
        base_revision=0,
        applied_event_sequence=0,
    )
    await store.delete_chat(CHAT)
    for action in (
        store.write_active_checkpoint(CHAT, record),
        store.reset_checkpoint_epoch(CHAT, reason="new"),
        store.write_pending_candidate(CHAT, candidate),
        store.install_ready_candidate(CHAT),
    ):
        with pytest.raises(ValueError, match="deleted"):
            await action
    assert not store.path_for(CHAT).exists()


def test_failed_export_never_publishes_partial_destination(
    tmp_path,
    monkeypatch,
):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    store._append_checkpoint_event(CHAT, event(1))
    destination = tmp_path / "rollback"
    monkeypatch.setattr(
        CheckpointStorageV2,
        "read_object",
        lambda *a: (_ for _ in ()).throw(OSError("export read failure")),
    )
    with pytest.raises(OSError):
        store.export_checkpoint_legacy(CHAT, destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".rollback.export-*")) == []


@pytest.mark.parametrize("formal", [False, True])
def test_migration_rejects_malformed_manifest_before_publishing(
    tmp_path,
    formal,
):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    directory = store.path_for(CHAT)
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({"boundaries": [{"id": "bad"}]}),
    )
    with pytest.raises(ValueError):
        store.migrate_checkpoint_v2(CHAT, dry_run=not formal)
    assert not (directory / "state-v2.json").exists()


def test_migration_rejects_missing_batch_and_invalid_epoch_history(tmp_path):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    boundary = store._commit(CHAT, [Msg("user", "hello", "user")])
    path = store.path_for(CHAT) / f"{boundary.id}.jsonl"
    payload = path.read_bytes()
    path.unlink()
    with pytest.raises((ValueError, OSError)):
        store.migrate_checkpoint_v2(CHAT, dry_run=True)
    path.write_bytes(payload)
    (store.path_for(CHAT) / "epochs.json").write_text(
        json.dumps({"current_epoch": 1, "history": "bad"}),
    )
    with pytest.raises(ValueError):
        store.migrate_checkpoint_v2(CHAT, dry_run=True)


def test_full_validation_quarantines_invalid_committed_manifest(tmp_path):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    value = CheckpointStorageV2(store.path_for(CHAT))
    value.publish(manifest={"boundaries": "bad"})
    with pytest.raises(ValueError, match="manifest"):
        store.validate_checkpoint_v2(CHAT)
    assert value.quarantine_path.exists()
    with pytest.raises(ValueError, match="quarantined"):
        store._append_checkpoint_event(CHAT, event(1))


def test_full_validation_checks_historical_root_structure(tmp_path):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.agents.memory.checkpoint_storage_v2 import (
        atomic_bytes,
        canonical_bytes,
    )

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    store._append_checkpoint_event(CHAT, event(1))
    store._reset_checkpoint_epoch(CHAT, "new")
    value = CheckpointStorageV2(store.path_for(CHAT))
    head = value.head()
    previous = value.previous_head(head)
    previous["active_count"] = "not-an-integer"
    head["previous_epoch"] = value._object(previous)
    atomic_bytes(value.root_path, canonical_bytes(head))
    with pytest.raises(ValueError, match="root"):
        store.validate_checkpoint_v2(CHAT)
    assert value.quarantine_path.exists()


def test_export_validates_checkpoint_before_publishing(tmp_path):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.agents.memory.checkpoint_storage_v2 import (
        atomic_bytes,
        canonical_bytes,
    )

    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    value = CheckpointStorageV2(store.path_for(CHAT))
    head = value.head()
    head["checkpoint_revision"] += 1
    atomic_bytes(value.root_path, canonical_bytes(head))
    destination = tmp_path / "export"
    with pytest.raises(ValueError, match="record"):
        store.export_checkpoint_legacy(CHAT, destination)
    assert not destination.exists()


@pytest.mark.asyncio
async def test_malformed_derived_offsets_fall_back_to_streaming_rows(tmp_path):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path / "dialog")
    messages = [Msg("user", str(index), "user") for index in range(20)]
    boundary = await store.commit(CHAT, messages)
    path = store.path_for(CHAT) / (boundary.id + ".offsets")
    offsets = bytearray(path.read_bytes())
    # Keep the total size intact while one requested window spans two lines.
    offsets[16 * 8 : 17 * 8] = offsets[17 * 8 : 18 * 8]
    path.write_bytes(offsets)
    page = await store.read_page(CHAT, limit=5)
    assert [message.content for message in page.messages] == [
        str(index) for index in range(15, 20)
    ]
