# -*- coding: utf-8 -*-
"""Reproducible local synthetic checkpoint/terminal-ledger measurements.

Run with the project virtualenv and PYTHONPATH=src. Fixtures are created in a
TemporaryDirectory, without per-record fsync, then removed after each size.
Only the measured operations use the real durable production write protocol.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

from swe.agents.memory.chat_checkpoint import CheckpointEvent, CheckpointRecord
from swe.agents.memory.checkpoint_storage_v2 import (
    CheckpointStorageV2,
    canonical_bytes,
)
from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.runner.session import _commit_ledger_sync
from swe.app.runner.turn_ledger import (
    TurnLedger,
    _digest,
    terminal_fingerprints,
)
from swe.performance_metrics import PerformanceMetrics

CHAT = "16374f34-c02e-4bb9-87bf-eb5e64348cef"


class _CountedFile:
    def __init__(self, handle, probe):
        self._handle = handle
        self._probe = probe

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *args):
        return self._handle.__exit__(*args)

    def __iter__(self):
        return self

    def __next__(self):
        result = next(self._handle)
        self._probe.read_bytes += _byte_size(result)
        return result

    def read(self, *args, **kwargs):
        result = self._handle.read(*args, **kwargs)
        self._probe.read_bytes += _byte_size(result)
        return result

    def readline(self, *args, **kwargs):
        result = self._handle.readline(*args, **kwargs)
        self._probe.read_bytes += _byte_size(result)
        return result

    def write(self, value):
        count = self._handle.write(value)
        self._probe.write_bytes += _byte_size(value[:count])
        return count


class _CountedScandir:
    def __init__(self, entries, probe):
        self._entries = entries
        self._probe = probe

    def __enter__(self):
        self._entries.__enter__()
        return self

    def __exit__(self, *args):
        return self._entries.__exit__(*args)

    def __iter__(self):
        return self

    def __next__(self):
        result = next(self._entries)
        self._probe.scandir_entries += 1
        return result


class _IOProbe:
    def __init__(self):
        self.read_bytes = self.write_bytes = self.scandir_entries = 0
        self._io_open = io.open
        self._builtin_open = open
        self._scandir = os.scandir

    def io_open(self, *args, **kwargs):
        return _CountedFile(self._io_open(*args, **kwargs), self)

    def builtin_open(self, *args, **kwargs):
        return _CountedFile(self._builtin_open(*args, **kwargs), self)

    def scandir(self, *args, **kwargs):
        return _CountedScandir(self._scandir(*args, **kwargs), self)


def _byte_size(value):
    return len(value.encode("utf-8")) if isinstance(value, str) else len(value)


def _measure(operation):
    """Count application payload I/O, excluding filesystem metadata syscalls."""
    probe = _IOProbe()
    metrics = PerformanceMetrics()
    gc.collect()
    with ExitStack() as stack:
        stack.enter_context(patch("io.open", probe.io_open))
        stack.enter_context(patch("builtins.open", probe.builtin_open))
        stack.enter_context(patch("os.scandir", probe.scandir))
        stack.enter_context(patch("swe.performance_metrics.METRICS", metrics))
        tracemalloc.start()
        started = time.perf_counter()
        try:
            result = operation()
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    return result, {
        "seconds": round(elapsed, 6),
        "peak_traced_python_bytes": peak,
        "application_read_bytes": probe.read_bytes,
        "application_write_bytes": probe.write_bytes,
        "scandir_entries": probe.scandir_entries,
        "counters": metrics.snapshot()["counters"],
    }


def _fixture_json(path, value):
    if "turn_id" in value:
        from swe.app.runner.turn_ledger import checked_record

        value = checked_record(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(value)
    path.write_bytes(payload)
    return len(payload)


def _checkpoint_fixture(directory, count):
    store = ConversationArchiveStore(
        directory,
        cursor_secret=b"benchmark-only",
    )
    store.migrate_checkpoint_v2(CHAT)
    storage = CheckpointStorageV2(store.path_for(CHAT))
    head = storage.head()
    base = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="message_added",
        facts={"message_id": "synthetic", "role": "user"},
        source_refs=("message:synthetic",),
    )
    ordinal = 0
    payload_bytes = 0
    for start in range(1, count + 1, storage.MAX_SEGMENT_EVENTS):
        size = min(storage.MAX_SEGMENT_EVENTS, count - start + 1)
        frames = bytearray()
        for sequence in range(start, start + size):
            event = replace(base, sequence=sequence).to_dict()
            encoded = canonical_bytes(event)
            frames.extend(
                canonical_bytes(
                    {
                        "payload": event,
                        "length": len(encoded),
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    },
                )
                + b"\n",
            )
        path = storage.segment_path(head, ordinal)
        path.write_bytes(frames)
        payload_bytes += len(frames)
        if start + size <= count:
            _fixture_json(
                path.with_suffix(".meta.json"),
                {
                    "ordinal": ordinal,
                    "bytes": len(frames),
                    "count": size,
                    "first_sequence": start,
                    "last_sequence": start + size - 1,
                    "sha256": hashlib.sha256(frames).hexdigest(),
                },
            )
            ordinal += 1
    head.update(
        last_event_sequence=count,
        active_segment_ordinal=ordinal,
        sealed_segment_count=ordinal,
        committed_bytes=len(frames),
        active_count=size,
        active_first_sequence=count - size + 1,
    )
    storage._publish(head)
    return store, replace(base, sequence=count + 1), payload_bytes


def _benchmark_checkpoint(directory, count):
    store, event, fixture_bytes = _checkpoint_fixture(directory, count)
    _, measurement = _measure(
        lambda: store._append_checkpoint_event(CHAT, event),
    )
    assert measurement["counters"]["journal_frames_decoded"] <= 512
    assert measurement["counters"]["journal_read_bytes"] <= 1024 * 1024
    return {
        "history_count": count,
        "fixture_journal_bytes": fixture_bytes,
        "append": measurement,
    }


def _turn(index, chat_id):
    return {
        "status": "completed",
        "chat_id": chat_id,
        "admission_order": index,
        "message": {
            "id": f"turn-{index}",
            "name": "user",
            "role": "user",
            "content": f"Synthetic terminal turn {index}: " + "x" * 256,
        },
    }


def _ledger_fixture(directory, count):
    session_path = directory / "session.json"
    ledger = TurnLedger(session_path)
    state = ledger.initialize({"turn_states": {}, "revision": 0})
    cold_count = count - 64
    generation = state["session_generation"]
    bucket = ledger._key("cold")
    latest = None
    fixture_bytes = 0
    revision = 0
    for start in range(0, cold_count, 64):
        revision += 1
        candidates = []
        for index in range(start, min(cold_count, start + 64)):
            turn_id = f"turn-{index}"
            turn = _turn(index, "cold")
            fixture_bytes += _fixture_json(
                ledger._turn_directory(state, turn_id) / f"{revision}.json",
                {
                    "turn_id": turn_id,
                    "turn": turn,
                    "hash": _digest(turn),
                    "deleted": False,
                    "revision": revision,
                    "generation": generation,
                    "nonce": generation,
                },
            )
            fixture_bytes += _fixture_json(
                ledger._reference_directory(state, turn_id)
                / f"{revision}.json",
                {
                    "turn_id": turn_id,
                    "revision": revision,
                    "generation": generation,
                },
            )
            candidates.append({"turn_id": turn_id, "order": index})
        node = {
            "candidates": candidates,
            "previous": latest,
            "max_order": candidates[-1]["order"],
            "generation": generation,
            "bucket": bucket,
            "revision": revision,
        }
        fixture_bytes += _fixture_json(
            ledger.directory(state) / "latest" / bucket / f"{revision}.json",
            node,
        )
        latest = {"revision": revision, "hash": _digest(node)}
    state.update(
        revision=revision,
        cold_turn_count=cold_count,
        next_admission_order=count,
        ledger_latest={bucket: latest},
        turn_states={
            f"turn-{index}": _turn(index, "hot")
            for index in range(cold_count, count)
        },
    )
    fixture_bytes += _fixture_json(session_path, state)
    return ledger, state, fixture_bytes


@contextmanager
def _session_flock(path):
    with path.with_suffix(".benchmark.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _benchmark_ledger(directory, count):
    ledger, state, fixture_bytes = _ledger_fixture(directory, count)

    def snapshot():
        return json.loads(ledger.session_path.read_bytes())

    result, point = _measure(
        lambda: ledger.get_turn(snapshot(), f"turn-{(count - 64) // 2}"),
    )
    assert result["status"] == "completed"
    result, latest = _measure(lambda: ledger.latest(snapshot(), "cold"))
    assert result[0] == f"turn-{count - 65}"
    baseline = terminal_fingerprints(state)
    state["turn_states"][f"turn-{count}"] = _turn(count, "hot")

    def commit():
        with _session_flock(ledger.session_path):
            return _commit_ledger_sync(
                str(ledger.session_path),
                state,
                baseline,
                state["revision"] + 1,
            )

    result, commit_measurement = _measure(commit)
    assert len(result["turn_states"]) == 64
    assert result["cold_turn_count"] == count - 63
    return {
        "terminal_turn_count": count,
        "cold_turn_count": count - 64,
        "fixture_payload_bytes": fixture_bytes,
        "point_lookup_including_root_read": point,
        "latest_cold_chat_including_root_read": latest,
        "commit_65_hot_turns_moving_one_to_cold": commit_measurement,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counts",
        type=int,
        nargs="+",
        default=[10000, 100000],
    )
    parser.add_argument(
        "--parts",
        choices=["checkpoint", "ledger"],
        nargs="+",
        default=["checkpoint", "ledger"],
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if any(count < 65 for count in args.counts):
        parser.error("counts must be at least 65")
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "fixture_kind": "synthetic direct filesystem construction; fixture writes omit per-record fsync",
        "measurement": "one operation; local warm filesystem; tracemalloc excludes fixture construction and imports; application byte I/O includes payloads only, not metadata syscalls; no RSS or NAS claim",
        "checkpoint": [],
        "ledger": [],
    }
    for part in args.parts:
        for count in args.counts:
            print(
                f"Building {part} fixture: {count}",
                file=sys.stderr,
                flush=True,
            )
            started = time.perf_counter()
            with tempfile.TemporaryDirectory(
                prefix="swe-memory-benchmark-",
            ) as temporary:
                directory = Path(temporary)
                if part == "checkpoint":
                    measurement = _benchmark_checkpoint(directory, count)
                else:
                    measurement = _benchmark_ledger(directory, count)
                measurement["fixture_and_measurement_seconds"] = round(
                    time.perf_counter() - started,
                    3,
                )
                report[part].append(measurement)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            )
            print(
                f"Finished {part}: {count}; temporary fixture removed",
                file=sys.stderr,
                flush=True,
            )


if __name__ == "__main__":
    main()
