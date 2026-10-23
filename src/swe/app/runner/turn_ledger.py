# -*- coding: utf-8 -*-
"""Session-owned immutable terminal turns; caller owns the session flock."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator
import uuid
from swe import performance_metrics
from swe.performance_metrics import increment, measure_storage_operation

HOT_TURNS = 64
HOT_BYTES = 256 * 1024
TERMINAL = frozenset({"completed", "stopped", "failed", "cancelled"})
ORDER = "admission_order"
logger = logging.getLogger(__name__)


class TurnHistorySnapshot(dict):
    """Explicit display projection; never used as an execution snapshot."""

    def __init__(self, state, chat_id):
        super().__init__(state)
        self.chat_id = chat_id


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: Any) -> None:
    """Stream one JSON value into a durable same-directory replacement."""
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        sync_directory(directory.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(
                value,
            ):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _digest(value: Any) -> str:
    digest = hashlib.sha256()
    for chunk in json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def checked_record(value):
    return {**value, "checksum": _digest(value)}


def verify_record(value):
    payload = {key: item for key, item in value.items() if key != "checksum"}
    if value.get("checksum") != _digest(payload):
        raise ValueError("Invalid terminal turn record checksum")


def _size(value: Any) -> int:
    return sum(
        len(chunk.encode("utf-8"))
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(value)
    )


def terminal_fingerprints(state: dict) -> dict[str, str]:
    return {
        key: _digest(value)
        for key, value in state.get("turn_states", {}).items()
        if isinstance(value, dict) and value.get("status") in TERMINAL
    }


def observe_hot_turns(state: dict, baseline: dict[str, str]) -> None:
    """Observe one published hot snapshot; never retain or load cold turns."""
    try:
        terminal_count = terminal_bytes = protected_count = protected_bytes = 0
        for key, value in state.get("turn_states", {}).items():
            size = _size({key: value})
            terminal = value.get("status") in TERMINAL
            if terminal:
                terminal_count += 1
                terminal_bytes += size
            if not terminal or baseline.get(key) != _digest(value):
                protected_count += 1
                protected_bytes += size
        performance_metrics.METRICS.record_session_memory(
            terminal_count,
            terminal_bytes,
            protected_count,
            protected_bytes,
        )
    except Exception:
        logger.warning("Failed to observe published session memory quantities")


class TurnLedger:
    """No process cache: reads are bounded to one ID's version directory."""

    def __init__(self, session_path: str | Path):
        self.session_path = Path(session_path)

    def directory(self, state: dict) -> Path:
        generation = state.get("session_generation")
        if (
            not isinstance(generation, str)
            or str(uuid.UUID(generation)) != generation
        ):
            raise ValueError("Invalid session ledger generation")
        return self.session_path.parent / ".turn-ledger" / generation

    def validate(self, state: dict) -> None:
        if state.get("schema_version") != 3:
            return
        directory = self.directory(state)
        metadata = json.loads((directory / "metadata.json").read_text())
        if metadata != {"session_file": self.session_path.name, "version": 1}:
            raise ValueError("Session ledger identity mismatch")

    @staticmethod
    def _key(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _turn_directory(self, state: dict, turn_id: str) -> Path:
        key = self._key(turn_id)
        return self.directory(state) / "turns" / key[:2] / key

    def _reference_directory(self, state: dict, turn_id: str) -> Path:
        key = self._key(turn_id)
        return self.directory(state) / "references" / key[:2] / key

    @staticmethod
    def _version(directory: Path, revision: int) -> Path | None:
        candidate = None
        selected = -1
        if not directory.exists():
            return None
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    number = int(entry.name[:-5])
                except ValueError:
                    raise ValueError(
                        "Invalid ledger version filename",
                    ) from None
                if selected < number <= revision:
                    selected, candidate = number, Path(entry.path)
        return candidate

    def _read_turn(self, state: dict, path: Path, turn_id: str | None = None):
        payload = path.read_bytes()
        increment("ledger_lookup_read_bytes", len(payload))
        value = json.loads(payload)
        verify_record(value)
        if any(
            (
                value["generation"] != state["session_generation"],
                value["revision"] != int(path.stem),
                value["revision"] > state["revision"],
                value["hash"] != _digest(value["turn"]),
                turn_id is not None and value["turn_id"] != turn_id,
                self._turn_directory(state, value["turn_id"]) != path.parent,
            ),
        ):
            raise ValueError("Invalid terminal turn ledger record")
        return value

    def get_turn(self, state: dict, turn_id: str) -> dict | None:
        hot = state.get("turn_states", {})
        if turn_id in hot:
            return dict(hot[turn_id])
        if state.get("schema_version") != 3:
            return None
        self.validate(state)
        reference = self._version(
            self._reference_directory(state, turn_id),
            state["revision"],
        )
        if reference is None:
            if (
                self._version(
                    self._turn_directory(state, turn_id),
                    state["revision"],
                )
                is not None
            ):
                raise ValueError("Missing committed terminal turn reference")
            return None
        ref = self._read_reference(state, reference, turn_id)
        path = self._turn_directory(state, turn_id) / f"{ref['revision']}.json"
        value = self._read_turn(state, path, turn_id)
        return None if value.get("deleted") else value["turn"]

    def _read_reference(self, state, path, turn_id=None):
        ref = json.loads(path.read_text())
        verify_record(ref)
        if any(
            (
                ref["generation"] != state["session_generation"],
                ref["revision"] != int(path.stem),
                ref["revision"] > state["revision"],
                self._reference_directory(state, ref["turn_id"])
                != path.parent,
                turn_id is not None and ref["turn_id"] != turn_id,
            ),
        ):
            raise ValueError("Invalid terminal turn reference")
        return ref

    def iter_cold(self, state: dict) -> Iterator[tuple[str, dict]]:
        if state.get("schema_version") != 3:
            return
        self.validate(state)
        root = self.directory(state) / "references"
        if not root.exists():
            if state.get("cold_turn_count", 0):
                raise ValueError("Missing committed terminal turn references")
            return
        count = 0
        with os.scandir(root) as prefixes:
            for prefix in prefixes:
                with os.scandir(prefix.path) as identities:
                    for identity in identities:
                        path = self._version(
                            Path(identity.path),
                            state["revision"],
                        )
                        if path is not None:
                            count += 1
                            ref = self._read_reference(state, path)
                            turn_id = ref["turn_id"]
                            payload = (
                                self._turn_directory(state, turn_id)
                                / f"{ref['revision']}.json"
                            )
                            value = self._read_turn(state, payload, turn_id)
                            if not value.get("deleted") and value[
                                "turn_id"
                            ] not in state.get("turn_states", {}):
                                yield value["turn_id"], value["turn"]
        if count != state.get("cold_turn_count", 0):
            raise ValueError("Missing committed terminal turn references")
        self.validate_latest(state)

    def materialize(self, state: dict, chat_id: str | None = None) -> dict:
        turns = dict(self.iter_cold(state))
        turns.update(state.get("turn_states", {}))
        if chat_id is not None:
            turns = {
                k: v
                for k, v in turns.items()
                if v.get("chat_id") in (None, chat_id)
            }
        ordered = sorted(turns.items(), key=lambda item: item[1].get(ORDER, 0))
        return TurnHistorySnapshot(
            {**state, "turn_states": dict(ordered)},
            chat_id,
        )

    def latest(self, state: dict, chat_id: str) -> tuple[str, dict] | None:
        best = None
        best_order = -1

        def consider(turn_id, turn):
            nonlocal best, best_order
            if (
                turn
                and turn.get("chat_id") in (None, chat_id)
                and turn.get("status") in (TERMINAL | {"admitted"})
            ):
                if turn.get(ORDER, 0) >= best_order:
                    best = turn_id, turn
                    best_order = turn.get(ORDER, 0)

        for turn_id, turn in state.get("turn_states", {}).items():
            consider(turn_id, turn)
        if state.get("schema_version") != 3:
            return best
        self.validate(state)
        for bucket in (chat_id, ""):
            key = self._key(bucket)
            head = state.get("ledger_latest", {}).get(key)
            for value in self._latest_nodes(state, key, head):
                for candidate in value["candidates"]:
                    if candidate["order"] >= best_order:
                        consider(
                            candidate["turn_id"],
                            self.get_turn(state, candidate["turn_id"]),
                        )
                if best_order > value["max_order"]:
                    break
        return best

    def _latest_nodes(self, state, bucket, head):
        ceiling = state["revision"] + 1
        while head is not None:
            revision = head["revision"]
            if type(revision) is not int or not 0 <= revision < ceiling:
                raise ValueError("Invalid latest turn index revision")
            path = (
                self.directory(state) / "latest" / bucket / f"{revision}.json"
            )
            value = json.loads(path.read_text())
            if (
                _digest(value) != head["hash"]
                or value["generation"] != state["session_generation"]
                or value["bucket"] != bucket
                or value["revision"] != revision
            ):
                raise ValueError("Invalid latest turn index")
            yield value
            ceiling = revision
            head = value["previous"]

    def validate_latest(self, state):
        for bucket, head in state.get("ledger_latest", {}).items():
            if len(bucket) != 64 or any(
                c not in "0123456789abcdef" for c in bucket
            ):
                raise ValueError("Invalid latest turn bucket")
            for _ in self._latest_nodes(state, bucket, head):
                pass

    @measure_storage_operation("session_recovery")
    def recover(self, state: dict) -> None:
        if state.get("schema_version") != 3:
            return
        self.validate(state)
        directory = self.directory(state)
        pending = directory / "pending.json"
        if not pending.exists():
            return
        intent = json.loads(pending.read_text())
        if any(
            (
                intent["generation"] != state["session_generation"],
                type(intent["base_revision"]) is not int,
                type(intent["target_revision"]) is not int,
                intent["target_revision"] != intent["base_revision"] + 1,
                str(uuid.UUID(intent["nonce"])) != intent["nonce"],
                not isinstance(intent["files"], list),
                len(intent["files"]) != len(set(intent["files"])),
            ),
        ):
            raise ValueError("Invalid ledger intent")
        paths = []
        for relative in intent["files"]:
            parts = Path(relative).parts
            valid = (
                len(parts) == 4
                and parts[0] in ("turns", "references")
                and len(parts[1]) == 2
                and len(parts[2]) == 64
                and parts[1] == parts[2][:2]
                and all(c in "0123456789abcdef" for c in parts[2])
            ) or (
                len(parts) == 3
                and parts[0] == "latest"
                and len(parts[1]) == 64
                and all(c in "0123456789abcdef" for c in parts[1])
            )
            path = directory / relative
            if (
                not valid
                or parts[-1] != f"{intent['target_revision']}.json"
                or not path.resolve().is_relative_to(directory.resolve())
            ):
                raise ValueError("Invalid ledger intent path")
            paths.append(path)
        committed = (
            state["revision"] == intent["target_revision"]
            and state.get("ledger_nonce") == intent["nonce"]
        )
        if not committed and state["revision"] != intent["base_revision"]:
            raise ValueError("Cannot reconcile session ledger intent")
        if committed:
            for path in paths:
                kind = path.relative_to(directory).parts[0]
                if kind == "turns":
                    self._read_turn(state, path)
                elif kind == "references":
                    self._read_reference(state, path)
                else:
                    bucket = path.parent.name
                    head = state.get("ledger_latest", {}).get(bucket)
                    if (
                        head is None
                        or head["revision"] != intent["target_revision"]
                    ):
                        raise ValueError("Missing committed latest turn index")
                    next(self._latest_nodes(state, bucket, head))
        else:
            for path in paths:
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    sync_directory(path.parent)
        pending.unlink()
        sync_directory(directory)

    def initialize(self, state: dict) -> dict:
        result = dict(
            state,
            schema_version=3,
            session_generation=str(uuid.uuid4()),
            turn_ledger_version=1,
        )
        result["turn_states"] = {
            key: dict(value, admission_order=index)
            for index, (key, value) in enumerate(
                state.get("turn_states", {}).items(),
            )
        }
        result["next_admission_order"] = len(result["turn_states"])
        result["cold_turn_count"] = 0
        result["ledger_latest"] = {}
        result.setdefault("revision", 0)
        atomic_json(
            self.directory(result) / "metadata.json",
            {"session_file": self.session_path.name, "version": 1},
        )
        return result

    def prepare(
        self,
        state: dict,
        baseline: dict[str, str],
        revision: int,
        *,
        deleted: tuple[str, ...] = (),
    ) -> dict:
        """Stage at most 64 durable terminal records before root publication."""
        self.validate(state)
        result = {
            **state,
            "revision": revision,
            "ledger_nonce": str(uuid.uuid4()),
        }
        hot = {
            key: dict(value)
            for key, value in state.get("turn_states", {}).items()
        }
        next_order = state.get("next_admission_order", 0)
        for value in hot.values():
            if ORDER not in value:
                value[ORDER] = next_order
                next_order += 1
        result["next_admission_order"] = next_order
        result["turn_states"] = hot
        result["ledger_latest"] = dict(state.get("ledger_latest", {}))
        eligible = sorted(
            (
                (key, value)
                for key, value in hot.items()
                if value.get("status") in TERMINAL and key not in deleted
            ),
            key=lambda pair: pair[1][ORDER],
        )
        count = len(eligible)
        size = sum(_size({key: value}) for key, value in eligible)
        moving = []
        for key, value in eligible:
            if count <= HOT_TURNS and size <= HOT_BYTES:
                break
            if baseline.get(key) != _digest(value):
                continue
            moving.append((key, value, False))
            count -= 1
            size -= _size({key: value})
            if len(moving) >= 64 - len(deleted):
                break
        for key in deleted:
            previous = self.get_turn(state, key)
            if previous is not None:
                moving.append((key, previous, True))
                hot.pop(key, None)
        directory = self.directory(state)
        planned = []
        by_chat: dict[str, list[str]] = {}
        for key, value, is_deleted in moving:
            path = self._turn_directory(state, key) / f"{revision}.json"
            if self._version(path.parent, state["revision"]) is None:
                result["cold_turn_count"] = (
                    result.get("cold_turn_count", 0) + 1
                )
            envelope = {
                "turn_id": key,
                "turn": value,
                "hash": _digest(value),
                "deleted": is_deleted,
                "revision": revision,
                "generation": state["session_generation"],
                "nonce": result["ledger_nonce"],
            }
            planned.append((path, checked_record(envelope)))
            planned.append(
                (
                    self._reference_directory(state, key) / f"{revision}.json",
                    checked_record(
                        {
                            "turn_id": key,
                            "revision": revision,
                            "generation": state["session_generation"],
                        },
                    ),
                ),
            )
            by_chat.setdefault(value.get("chat_id") or "", []).append(key)
            hot.pop(key, None)
        self._stage_latest(state, result, moving, by_chat, planned)
        if planned:
            atomic_json(
                directory / "pending.json",
                {
                    "generation": state["session_generation"],
                    "base_revision": state["revision"],
                    "target_revision": revision,
                    "nonce": result["ledger_nonce"],
                    "files": [
                        str(path.relative_to(directory)) for path, _ in planned
                    ],
                },
            )
            for path, value in planned:
                if path.exists():
                    raise ValueError(
                        "Refusing to overwrite immutable ledger version",
                    )
                atomic_json(path, value)
        return result

    def _stage_latest(self, state, result, moving, by_chat, planned):
        directory = self.directory(state)
        revision = result["revision"]
        orders = {key: value[ORDER] for key, value, _ in moving}
        for chat_id, ids in by_chat.items():
            bucket = self._key(chat_id)
            parent = directory / "latest" / bucket
            previous = result["ledger_latest"].get(bucket)
            previous_max = -1
            if previous is not None:
                previous_max = next(
                    self._latest_nodes(state, bucket, previous),
                )["max_order"]
            value = {
                "candidates": [
                    {"turn_id": key, "order": orders[key]} for key in ids
                ],
                "previous": previous,
                "max_order": max(previous_max, *(orders[key] for key in ids)),
                "generation": state["session_generation"],
                "bucket": bucket,
                "revision": revision,
            }
            planned.append((parent / f"{revision}.json", value))
            result["ledger_latest"][bucket] = {
                "revision": revision,
                "hash": _digest(value),
            }

    def finish(self, state: dict) -> None:
        pending = self.directory(state) / "pending.json"
        if pending.exists():
            pending.unlink()
            sync_directory(pending.parent)
