# -*- coding: utf-8 -*-
"""Versioned recovery envelopes with direct immutable body references."""

from copy import deepcopy
from dataclasses import dataclass
import json

from .bundle import BundleBlock
from .errors import IntegrityError

FORMAT = "swe-recovery-projection"
VERSION = 1
LISTS = (
    (("agent", "memory", "content"), "memory", True),
    (("agent", "memory", "messages"), "memory", False),
    (("task_messages",), "task", False),
    (("model_call_failed_messages",), "error", False),
)
TERMINAL = frozenset(
    {"completed", "cancelled", "stopped", "failed", "interrupted"},
)


def _bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _parent(state, path):
    parent = state
    for key in path[:-1]:
        if not isinstance(parent, dict) or key not in parent:
            return None
        parent = parent[key]
    return parent if isinstance(parent, dict) else None


@dataclass(frozen=True)
class Projection:
    envelope: dict
    blocks: tuple
    messages: tuple
    dependency_ids: tuple


def project_state(
    state,
    *,
    operation_key,
    resolve_message=None,
    resolve_turn=None,
):
    if not isinstance(state, dict):
        raise ValueError("recovery state must be a complete object")
    projected = deepcopy(state)
    envelope = {
        "format": FORMAT,
        "version": VERSION,
        "state": projected,
        "lists": [],
        "turns": [],
    }
    blocks, messages, dependencies = [], [], set()
    for path, layer, paired in LISTS:
        parent = _parent(projected, path)
        if parent is None or path[-1] not in parent:
            continue
        values = parent[path[-1]]
        if not isinstance(values, list):
            raise IntegrityError(
                "known recovery history list has invalid shape",
            )
        entries = []
        for item in values:
            body, marks = (item, None)
            if paired and isinstance(item, (list, tuple)) and len(item) == 2:
                body, marks = item
            if not isinstance(body, dict):
                raise IntegrityError(
                    "recovery message must be a complete object",
                )
            ref = resolve_message(layer, body) if resolve_message else None
            if ref is None:
                name = "delta:" + str(len(blocks))
                ref = {"operation_key": operation_key, "block_name": name}
                blocks.append(BundleBlock(name, layer, _bytes(body)))
                messages.append((name, layer, deepcopy(body)))
            else:
                ref = deepcopy(ref)
                if "publication_id" in ref:
                    dependencies.add(ref["publication_id"])
            entries.append(
                {
                    "ref": ref,
                    "shape": "pair" if marks is not None else "dict",
                    "marks": deepcopy(marks),
                },
            )
        parent[path[-1]] = []
        envelope["lists"].append({"path": list(path), "entries": entries})
    turns = projected.get("turn_states")
    if isinstance(turns, dict) and resolve_turn:
        for key, value in list(turns.items()):
            if (
                not isinstance(value, dict)
                or value.get("status") not in TERMINAL
            ):
                continue
            ref = resolve_turn(key, value)
            if ref is not None:
                dependencies.add(ref["publication_id"])
                envelope["turns"].append({"key": key, "ref": deepcopy(ref)})
                del turns[key]
    return Projection(
        envelope,
        tuple(blocks),
        tuple(messages),
        tuple(sorted(dependencies)),
    )


def is_projection(value):
    return isinstance(value, dict) and value.get("format") == FORMAT


def hydrate_state(value, resolve):
    if not is_projection(value):
        if not isinstance(value, dict):
            raise IntegrityError("recovery root must be an object")
        return deepcopy(value)
    fields = {"format", "version", "state", "lists", "turns"}
    if (
        not fields <= set(value)
        or set(value)
        - fields
        - {"identity", "installations", "dependency_ids", "prune_ids"}
        or value["version"] != VERSION
        or not isinstance(value["state"], dict)
    ):
        raise IntegrityError("unsupported recovery projection envelope")
    result = deepcopy(value["state"])
    allowed = {path for path, _, _ in LISTS}
    seen = set()
    for series in value["lists"]:
        path = tuple(series["path"])
        if path not in allowed or path in seen:
            raise IntegrityError(
                "recovery projection path is invalid or repeated",
            )
        seen.add(path)
        parent = _parent(result, path)
        if parent is None or parent.get(path[-1]) != []:
            raise IntegrityError("recovery projection target is not empty")
        entries = []
        for entry in series["entries"]:
            body = deepcopy(resolve(entry["ref"]))
            if not isinstance(body, dict):
                raise IntegrityError(
                    "recovery body reference is not an object",
                )
            if entry["shape"] == "pair":
                entries.append([body, deepcopy(entry["marks"])])
            elif entry["shape"] == "dict" and entry["marks"] is None:
                entries.append(body)
            else:
                raise IntegrityError("recovery list item shape is invalid")
        parent[path[-1]] = entries
    for turn in value["turns"]:
        turns = result.setdefault("turn_states", {})
        if turn["key"] in turns:
            raise IntegrityError(
                "recovery Turn reference duplicates inline state",
            )
        turns[turn["key"]] = deepcopy(resolve(turn["ref"]))
    return result
