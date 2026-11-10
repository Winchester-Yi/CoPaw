# -*- coding: utf-8 -*-
"""Strict old-format extraction on disposable working copies only.

Existing checksums are verified by old readers; this module creates no new
source digest. Raw modules and committed artifacts remain available for exact
semantic comparison and rollback export rather than display-only conversion.
"""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile

from agentscope.message import Msg

from swe.agents.memory.chat_checkpoint import (
    CheckpointEvent,
    PrecompactionCandidate,
)
from swe.agents.memory.checkpoint_storage_v2 import CheckpointStorageV2
from swe.agents.memory.conversation_archive import (
    ConversationArchiveBoundary,
    ConversationArchiveStore,
)
from swe.app.runner.session import _read_json_state_sync
from swe.app.runner.turn_ledger import TurnLedger
from ..chat_mutations import load_metadata_json


def safe_files(root):
    root = Path(root).resolve(strict=True)
    paths = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                "source contains symlink; explicit regular-file snapshot required",
            )
        if path.is_file():
            paths.append(path)
        elif not path.is_dir():
            raise ValueError("source contains unsupported filesystem object")
    return paths


@contextmanager
def working_copy(root, *, selected=None):
    root = Path(root).resolve(strict=True)
    paths = safe_files(root)
    if selected is not None:
        paths = [path for path in paths if selected(path.relative_to(root))]
    with tempfile.TemporaryDirectory(
        prefix="conversation-legacy-",
    ) as directory:
        target = Path(directory).resolve() / "source"
        target.mkdir()
        for source in paths:
            destination = target / source.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        yield target


def _json(path):
    return load_metadata_json(path.read_text(encoding="utf-8"))


def read_session(path):
    path = Path(path).resolve(strict=True)
    _, committed = _read_json_state_sync(str(path), allow_not_exist=False)
    if committed.get("schema_version") != 3:
        return {
            "format": "session-v" + str(committed.get("schema_version", 1)),
            "root": deepcopy(committed),
            "state": committed,
            "recovery_actions": [],
            "files": [path.name],
        }
    generation = committed.get("session_generation")

    def selected(relative):
        if relative == Path(path.name):
            return True
        return len(relative.parts) > 2 and relative.parts[:2] == (
            ".turn-ledger",
            generation,
        )

    with working_copy(path.parent, selected=selected) as root:
        copied = root / path.name
        _, state = _read_json_state_sync(str(copied), allow_not_exist=False)
        original = deepcopy(state)
        actions = []
        ledger = TurnLedger(copied)
        if state.get("schema_version") == 3:
            pending = ledger.directory(state) / "pending.json"
            if pending.exists():
                before = _json(pending)
                ledger.recover(state)
                actions.append(
                    {"kind": "ledger_pending_reconciled", "intent": before},
                )
            state = dict(ledger.materialize(state))
        return {
            "format": "session-v" + str(original.get("schema_version", 1)),
            "root": original,
            "state": state,
            "recovery_actions": actions,
            "files": [
                str(item.relative_to(root)) for item in safe_files(root)
            ],
        }


def _strict_events(path):
    if not path.exists():
        return []
    return [
        CheckpointEvent.from_dict(load_metadata_json(line)).to_dict()
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _strict_batches(chat_dir, chat_id, manifest):
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("boundaries"),
        list,
    ):
        raise ValueError("invalid archive manifest")
    batches = []
    seen = set()
    for value in manifest["boundaries"]:
        boundary = ConversationArchiveBoundary.from_dict(value)
        if boundary.chat_id != chat_id or boundary.id in seen:
            raise ValueError("archive boundary identity mismatch or duplicate")
        seen.add(boundary.id)
        path = chat_dir / (boundary.id + ".jsonl")
        if not path.is_file():
            raise ValueError("referenced archive batch is missing")
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = load_metadata_json(line)
            Msg.from_dict(raw)
            messages.append(raw)
        if len(messages) != boundary.archived_message_count:
            raise ValueError("archive batch count disagrees with boundary")
        if (
            not messages
            or messages[0]["id"] != boundary.first_message_id
            or messages[-1]["id"] != boundary.last_message_id
        ):
            raise ValueError("archive batch endpoints disagree with boundary")
        batches.append({"boundary": boundary.to_dict(), "messages": messages})
    return batches


def _v2_epochs(chat_dir, chat_id):
    storage = CheckpointStorageV2(chat_dir)
    storage.check_writable()
    head = storage.head()
    epochs = []
    while head is not None:
        if head["chat_id"] != chat_id:
            raise ValueError("checkpoint journal Chat identity mismatch")
        epoch = {
            "epoch": head["epoch"],
            "head": head,
            "record": storage.record(head).to_dict(),
            "events": [event.to_dict() for event in storage.events(head)],
        }
        active = storage.segment_path(head)
        size = active.stat().st_size
        if size < head["committed_bytes"]:
            raise ValueError("checkpoint journal committed extent is missing")
        epoch["excluded_tail_bytes"] = size - head["committed_bytes"]
        for field in ("manifest", "evidence", "epochs"):
            epoch[field] = storage.read_object(head[field])
        # Validate archived batches for every retained epoch, even if current
        # roots no longer expose them. No malformed row is silently omitted.
        epoch["batches"] = _strict_batches(
            chat_dir,
            chat_id,
            epoch["manifest"],
        )
        epochs.append(epoch)
        head = storage.previous_head(head)
    return epochs


def _v1_epoch(store, chat_dir, chat_id):
    current = store._read_current_epoch(chat_dir)
    record = store._read_checkpoint_locked(chat_id, current)
    events = _strict_events(chat_dir / "events.jsonl")
    manifest = (
        _json(chat_dir / "manifest.json")
        if (chat_dir / "manifest.json").exists()
        else {"boundaries": []}
    )
    evidence = store._read_evidence_epochs(chat_dir)
    epochs = (
        _json(chat_dir / "epochs.json")
        if (chat_dir / "epochs.json").exists()
        else {"current_epoch": 1, "history": []}
    )
    _validate_epoch_history(epochs, current)
    _validate_v1_events(record, events)
    return {
        "epoch": current,
        "record": record.to_dict(),
        "events": events,
        "manifest": manifest,
        "evidence": evidence,
        "epochs": epochs,
        "batches": _strict_batches(chat_dir, chat_id, manifest),
    }


def _validate_v1_events(record, events):
    groups = {}
    for event in events:
        if event["epoch"] > record.epoch:
            raise ValueError("event epoch exceeds current checkpoint epoch")
        groups.setdefault(event["epoch"], []).append(event)
    if record.applied_event_sequence > len(groups.get(record.epoch, [])):
        raise ValueError("checkpoint watermark exceeds committed events")
    for epoch, batch in groups.items():
        sequences = [event["sequence"] for event in batch]
        if sequences != list(range(1, len(batch) + 1)):
            raise ValueError("checkpoint event sequence is not continuous")


def _validate_epoch_history(epochs, current):
    if not isinstance(epochs, dict) or epochs.get("current_epoch") != current:
        raise ValueError(
            "checkpoint epoch metadata disagrees with current root",
        )
    history = epochs.get("history")
    if not isinstance(history, list):
        raise ValueError("checkpoint epoch history is invalid")
    sequence = [
        entry.get("epoch") if isinstance(entry, dict) else None
        for entry in history
    ]
    if any(
        type(value) is not int or not 1 <= value <= current
        for value in sequence
    ):
        raise ValueError("checkpoint epoch history contains invalid epoch")
    if sequence != sorted(set(sequence)) or (
        sequence and sequence[-1] != current
    ):
        raise ValueError(
            "checkpoint epoch history is not ordered to current epoch",
        )


def read_archive(dialog_root, chat_id):
    ConversationArchiveStore._validate_chat_id(chat_id)

    def selected(relative):
        return relative.parts[0] == chat_id or relative.parts == (
            ".deleted",
            chat_id,
        )

    with working_copy(dialog_root, selected=selected) as copied:
        store = ConversationArchiveStore(
            copied,
            cursor_secret=b"offline-migration",
        )
        deleted = (copied / ".deleted" / chat_id).exists()
        chat_dir = copied / chat_id
        if not chat_dir.exists():
            return {
                "format": "absent",
                "deleted": deleted,
                "epochs": [],
                "batches": [],
                "candidates": [],
                "recovery_actions": [],
            }
        epochs = (
            _v2_epochs(chat_dir, chat_id)
            if (chat_dir / "state-v2.json").exists()
            else [_v1_epoch(store, chat_dir, chat_id)]
        )
        candidates = []
        for path in sorted((chat_dir / "candidates").glob("*.json")):
            value = _json(path)
            candidate = PrecompactionCandidate.from_dict(value)
            if candidate.chat_id != chat_id:
                raise ValueError("candidate Chat identity mismatch")
            candidates.append(value)
        actions = [
            {
                "kind": "journal_uncommitted_tail_excluded",
                "epoch": epoch["epoch"],
                "bytes": epoch["excluded_tail_bytes"],
            }
            for epoch in epochs
            if epoch.get("excluded_tail_bytes")
        ]
        return {
            "format": (
                "checkpoint-v2" if "head" in epochs[0] else "checkpoint-v1"
            ),
            "deleted": deleted,
            "epochs": epochs,
            "batches": epochs[0]["batches"],
            "candidates": candidates,
            "recovery_actions": actions,
        }
