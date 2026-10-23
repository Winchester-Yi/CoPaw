# -*- coding: utf-8 -*-
"""Create masked, isolated legacy-history copies for storage rehearsals.

Never persist the returned source_fingerprints in a public report. Derived
.offsets indexes are deliberately omitted; production readers rebuild them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from datetime import datetime
from uuid import UUID

_KEYS = set(
    """
agent memory content schema_version revision turn_states id name role timestamp
metadata type text thinking input output source data url format mime_type
chat_id session_id user_id message_id turn_id task_id checkpoint_id event_id
status turn_status admission_order admitted_at completed_at stopped_at failed_at
created_at updated_at occurred_at started_at finished_at sequence epoch facts
source_refs evidence_refs current_epoch history reason source_revision
applied_event_sequence confidence current_task title goal acceptance_criteria
constraints_and_preferences progress done in_progress blocked key_decisions
next_steps critical_context risks_and_unverified completed_task_index
archived_through messages tool_calls tool_call_id tool_use_id arguments
session_skill_snapshot skills description version enabled expires_at captured_at
_compressed_summary _system_prompt _pending_tool_use_ids _mark
compressed_summary summary system_prompt tool_use tool_result cache_control
artifact_ref artifact_refs boundary_id command error_type exit_code token_count
tool_name
""".split(),
)
_ENUMS = {
    "role": {"user", "assistant", "system", "tool", "developer"},
    "status": {
        "empty",
        "admitted",
        "running",
        "pending",
        "in_progress",
        "completed",
        "stopped",
        "failed",
        "cancelled",
        "done",
        "blocked",
    },
    "confidence": {"verified", "degraded"},
    "type": {
        "text",
        "thinking",
        "image",
        "audio",
        "video",
        "tool_use",
        "tool_result",
        "message_added",
        "task_started",
        "task_completed",
        "checkpoint_updated",
        "task_status_changed",
        "progress_updated",
        "image_url",
        "input_text",
        "output_text",
        "base64",
        "url",
    },
}
_ENUMS["turn_status"] = _ENUMS["status"]
_EXCLUDED = {
    "models",
    "node_modules",
    "logs",
    "skills",
    "active_skills",
    "customized_skills",
    "venv",
    "__pycache__",
    "backups",
}
_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
)
_NUMERIC_FIELDS = {
    "schema_version",
    "revision",
    "epoch",
    "sequence",
    "current_epoch",
    "applied_event_sequence",
    "source_revision",
    "admission_order",
    "token_count",
    "exit_code",
}
_SENSITIVE_CONTAINERS = {"metadata", "input", "arguments", "output", "body"}


class Sanitizer:
    """Per-run HMAC aliases and UTF-8-size-preserving free-text masking."""

    def __init__(self, salt: bytes | None = None):
        self.salt = salt or secrets.token_bytes(32)

    def identifier(self, value: str) -> str:
        digest = hmac.new(
            self.salt,
            value.encode(),
            hashlib.sha256,
        ).hexdigest()
        try:
            UUID(value)
        except ValueError:
            return "k" + digest[:31]
        return str(UUID(digest[:32], version=4))

    @staticmethod
    def mask(value: str) -> str:
        glyphs = {1: "x", 2: "¢", 3: "□", 4: "🔒"}
        return "".join(
            char if char.isspace() else glyphs[len(char.encode("utf-8"))]
            for char in value
        )

    def clean(self, value, field="", sensitive=False):
        sensitive = sensitive or field in _SENSITIVE_CONTAINERS
        if isinstance(value, dict):
            dynamic = field == "turn_states"
            return {
                (
                    self.identifier(key)
                    if dynamic or key not in _KEYS
                    else key
                ): self.clean(
                    item,
                    key,
                    sensitive,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.clean(item, field, sensitive) for item in value]
        if isinstance(value, str):
            return self._string(value, field, sensitive)
        if (
            (sensitive or field not in _NUMERIC_FIELDS)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return 0.0 if isinstance(value, float) else 0
        return value

    def _string(self, value, field, sensitive):
        if field in {
            "source_refs",
            "evidence_refs",
            "artifact_ref",
            "artifact_refs",
        }:
            return self._reference(value)
        if (
            field == "id"
            or field.endswith("_id")
            or field == "archived_through"
        ):
            return self.identifier(value) if value else value
        if field in _ENUMS and value in _ENUMS[field] and not sensitive:
            return value
        if (field.endswith("_at") or field == "timestamp") and _ISO.fullmatch(
            value,
        ):
            try:
                datetime.fromisoformat(value)
            except ValueError:
                return self.mask(value)
            suffix = re.sub(r"\d", "0", value[19:])
            return "2000-01-01" + value[10] + "00:00:00" + suffix
        return self.mask(value)

    def _reference(self, value):
        prefix, separator, identifier = value.partition(":")
        if not separator or not prefix or not identifier:
            return self.mask(value)
        known = {
            "message",
            "event",
            "turn",
            "task",
            "checkpoint",
            "archive",
            "boundary",
        }
        alias = prefix if prefix in known else self.identifier(prefix)
        # Archived message evidence carries batch UUID, row index, message ID.
        components = identifier.split(":")
        if (
            prefix == "archive"
            and len(components) == 3
            and components[1].isdigit()
        ):
            batch, index, message_id = components
            identifier = ":".join(
                (self.identifier(batch), index, self.identifier(message_id)),
            )
        else:
            identifier = self.identifier(identifier)
        return alias + ":" + identifier


def _stable_read(path: Path):
    """Reject symbolic links and files modified during either of two reads."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        payload = handle.read()
        digest = hashlib.sha256(payload).hexdigest()
        handle.seek(0)
        check = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
    current = path.lstat()
    signature = lambda stat: (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    if (
        signature(before) != signature(after)
        or signature(after) != signature(current)
        or digest != check
    ):
        raise RuntimeError("Source changed during stable read")
    return payload, digest


def verify_sources(fingerprints: dict[str, str]) -> bool:
    """Read-only verification that inventoried source bytes are unchanged."""
    try:
        return all(
            _stable_read(Path(path))[1] == digest
            for path, digest in fingerprints.items()
        )
    except (OSError, RuntimeError):
        return False


def _walk(root: Path, inventory):
    for directory, directories, files in os.walk(root, followlinks=False):
        kept = []
        for name in directories:
            path = Path(directory) / name
            if path.is_symlink():
                inventory["symlinks_skipped"] += 1
            elif not name.startswith(".") and name not in _EXCLUDED:
                kept.append(name)
        directories[:] = sorted(kept)
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                inventory["symlinks_skipped"] += 1
            elif not name.startswith("."):
                yield path


def _decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        payload,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ValueError("Nonfinite JSON number"),
        ),
    )


def _session(payload):
    value = _decode(payload)
    if not isinstance(value, dict):
        raise ValueError("Session must be an object")
    schema = value.get("schema_version", 1)
    if type(schema) is not int or schema not in (1, 2):
        raise NotImplementedError("Only legacy session schemas 1 and 2")
    turns = value.get("turn_states", {})
    if not isinstance(turns, dict) or any(
        not isinstance(turn, dict) for turn in turns.values()
    ):
        raise ValueError("Invalid turn states")
    return value, schema, len(turns)


def _events(payload):
    events = [_decode(line) for line in payload.splitlines() if line.strip()]
    for event in events:
        if (
            not isinstance(event, dict)
            or not {"id", "sequence", "epoch", "occurred_at", "type", "facts"}
            <= event.keys()
        ):
            raise ValueError("Invalid legacy checkpoint event")
        if "checksum" in event or event.get("format_version", 1) != 1:
            raise NotImplementedError(
                "Checksummed journals require a separate fixture adapter",
            )
        if (
            not isinstance(event["facts"], dict)
            or type(event["sequence"]) is not int
            or type(event["epoch"]) is not int
            or any(
                not isinstance(event[key], str)
                for key in ("id", "occurred_at", "type")
            )
        ):
            raise ValueError("Invalid event structure")
        refs = event.get("source_refs", [])
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) for ref in refs
        ):
            raise ValueError("Invalid event source references")
    return events


def _quantiles(items):
    ordered = sorted(
        items,
        key=lambda item: (item["bytes"], str(item["path"])),
    )
    if not ordered:
        return []
    return [ordered[round((len(ordered) - 1) * q)] for q in (0, 0.5, 0.9, 1)]


def _stats(items, unit):
    sizes = sorted(item["bytes"] for item in items)
    counts = [item[unit] for item in items]
    return {
        "count": len(items),
        "bytes": sum(sizes),
        unit: sum(counts),
        f"max_{unit}": max(counts, default=0),
        "min_bytes": min(sizes, default=0),
        "max_bytes": max(sizes, default=0),
    }


def _write(path, value, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(
        path,
        "x",
        encoding="utf-8",
        opener=lambda name, flags: os.open(name, flags, 0o600),
    ) as handle:
        if jsonl:
            for item in value:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        else:
            json.dump(value, handle, ensure_ascii=False)
    return path.stat().st_size


def _inventory(source):
    inventory: dict = {
        "invalid": 0,
        "unsupported": 0,
        "changed": 0,
        "symlinks_skipped": 0,
        "omitted_derived_indexes": True,
    }
    sessions, journals, fingerprints = [], [], {}
    for path in _walk(source, inventory):
        kind = (
            "session"
            if path.parent.name == "sessions" and path.suffix == ".json"
            else (
                "journal"
                if path.name == "events.jsonl"
                and path.parent.parent.name == "dialog"
                else None
            )
        )
        if kind is None:
            continue
        try:
            if (
                kind == "journal"
                and str(UUID(path.parent.name)) != path.parent.name
            ):
                raise ValueError("Chat directory must be a canonical UUID")
            payload, digest = _stable_read(path)
            fingerprints[str(path)] = digest
            item = {"path": path, "bytes": len(payload)}
            if kind == "session":
                _, item["schema"], item["turns"] = _session(payload)
                sessions.append(item)
            else:
                extras = [
                    p
                    for p in path.parent.iterdir()
                    if p.name != "events.jsonl"
                    and not p.name.endswith(".offsets")
                ]
                if extras:
                    raise NotImplementedError(
                        "Journal includes other artifacts",
                    )
                item["events"] = len(_events(payload))
                journals.append(item)
        except NotImplementedError:
            inventory["unsupported"] += 1
        except RuntimeError:
            inventory["changed"] += 1
        except (ValueError, OSError, UnicodeError):
            inventory["invalid"] += 1
    inventory["sessions"] = _stats(sessions, "turns")
    inventory["sessions"]["schemas"] = {
        str(schema): sum(item["schema"] == schema for item in sessions)
        for schema in (1, 2)
    }
    inventory["journals"] = _stats(journals, "events")
    return inventory, sessions, journals, fingerprints


def prepare_cases(source_root: Path, output_root: Path) -> dict:
    """Inventory legacy data, select representative copies, and mask them."""
    source_root, output_root = Path(source_root), Path(output_root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError("Source must be a real directory")
    source = source_root.resolve()
    output = output_root.resolve()
    if (
        output_root.exists()
        or output_root.is_symlink()
        or output == source
        or source in output.parents
    ):
        raise ValueError("Destination must be new and outside source")
    inventory, sessions, journals, fingerprints = _inventory(source)
    selected = [item for item in sessions if item["turns"]]
    for schema in (1, 2):
        selected.extend(
            _quantiles(
                [item for item in sessions if item["schema"] == schema],
            ),
        )
    selected = list({str(item["path"]): item for item in selected}.values())
    if len(selected) > 20:
        raise ValueError(
            "More than 20 required session cases; explicit sampling needed",
        )
    output_root.mkdir(parents=True, mode=0o700)
    sanitizer, cases = Sanitizer(), []
    for kind, items in (
        ("session", selected),
        ("journal", _quantiles(journals)),
    ):
        items = list({str(item["path"]): item for item in items}.values())
        for number, item in enumerate(items, 1):
            payload, digest = _stable_read(item["path"])
            if digest != fingerprints[str(item["path"])]:
                raise RuntimeError("Source changed after inventory")
            case_id = f"{kind}-{number:02d}"
            root = output_root / case_id
            root.mkdir(mode=0o700)
            case = {
                "id": case_id,
                "kind": kind,
                "root": str(root.resolve()),
                "source_bytes": item["bytes"],
            }
            if kind == "session":
                case.update(session_id="s", turns=item["turns"])
                case["fixture_bytes"] = _write(
                    root / "sessions/s.json",
                    sanitizer.clean(_session(payload)[0]),
                )
            else:
                chat_id = sanitizer.identifier(item["path"].parent.name)
                case.update(chat_id=chat_id, events=item["events"])
                (root / "dialog").mkdir(mode=0o700)
                case["fixture_bytes"] = _write(
                    root / "dialog" / chat_id / "events.jsonl",
                    sanitizer.clean(_events(payload)),
                    True,
                )
            cases.append(case)
    if not verify_sources(fingerprints):
        raise RuntimeError("Original sources changed during preparation")
    return {
        "inventory": inventory,
        "cases": cases,
        "source_fingerprints": fingerprints,
    }
