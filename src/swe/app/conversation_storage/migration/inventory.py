# -*- coding: utf-8 -*-
"""Inventory explicit frozen workspace mappings without deriving identities."""

from dataclasses import asdict, dataclass, field
from pathlib import Path
import stat

from swe.app.runner.models import ChatsFile
from swe.app.runner.session import sanitize_filename
from swe.app.chat_sharing.models import ChatShareRecord, ChatShareSnapshot
from ..chat_mutations import load_metadata_json
from ..identity import (
    SessionIdentity,
    WorkspaceScope,
    chat_key_bytes,
    service_unit_bytes,
)
from ..bundle import MAX_READ_BYTES
from .legacy import read_archive, read_session
from .manifest import SourceBoundary
from .source_inputs import safe_input_entries, workspace_entries


@dataclass(frozen=True)
class WorkspaceMapping:
    scope: WorkspaceScope
    root: Path
    registry_path: str = "chats.json"
    sessions_dir: str = "sessions"
    dialog_dir: str = "dialog"
    session_paths: dict = field(default_factory=dict)

    def path(self, relative):
        value = Path(relative)
        if value.is_absolute() or ".." in value.parts:
            raise ValueError(
                "source path must be relative to trusted workspace",
            )
        root = Path(self.root).resolve(strict=True)
        path = root / value
        if not path.resolve().is_relative_to(root):
            raise ValueError("source path escapes trusted workspace")
        return path

    def session_path(self, identity):
        key = (identity.user_id, identity.channel, identity.session_id)
        if key in self.session_paths:
            return self.path(self.session_paths[key])
        sid = sanitize_filename(identity.session_id)
        user = sanitize_filename(identity.user_id) if identity.user_id else ""
        name = user + "_" + sid + ".json" if user else sid + ".json"
        return self.path(str(Path(self.sessions_dir) / name))


def _source_files(mapping):
    root = Path(mapping.root)
    if root.is_symlink():
        raise ValueError("source root is a symlink")
    entries = workspace_entries(mapping)
    for path in entries:
        if stat.S_IMODE(path.stat().st_mode) & 0o222:
            raise ValueError(
                "source snapshot must be read-only before inventory",
            )
    return [path for path in entries if path.is_file()]


def _block(report, code, position, error):
    report["blockers"].append(
        {"code": code, "position": position, "detail": str(error)},
    )


def _sessions(mapping, chats, report, position):
    grouped, paths, result = {}, {}, []
    candidates = [
        (chat.user_id, chat.channel, chat.session_id, index)
        for index, chat in enumerate(chats)
    ]
    known = {(user, channel, sid) for user, channel, sid, _ in candidates}
    candidates.extend(
        (*key, None) for key in mapping.session_paths if key not in known
    )
    for user_id, channel, session_id, index in candidates:
        identity = SessionIdentity(
            mapping.scope,
            user_id,
            channel,
            session_id,
        )
        key = identity.sql_values
        path = mapping.session_path(identity)
        previous = paths.get(path)
        if previous is not None and previous != key:
            _block(
                report,
                "identity_path_collision",
                position,
                "distinct Session identities map to one source filename",
            )
            continue
        paths[path] = key
        if key in grouped:
            if index is not None:
                grouped[key]["chat_positions"].append(index)
            continue
        record = {
            "position": position + "/session:" + str(len(result)),
            "identity": [
                mapping.scope.service_unit_id,
                mapping.scope.tenant_id,
                mapping.scope.source_id,
                mapping.scope.agent_id,
                user_id,
                channel,
                session_id,
            ],
            "path": str(path.relative_to(Path(mapping.root).resolve())),
            "chat_positions": [index] if index is not None else [],
        }
        grouped[key] = record
        try:
            record["legacy"] = read_session(path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            _block(report, "session_invalid", record["position"], error)
        result.append(record)
    session_root = mapping.path(mapping.sessions_dir)
    for path in sorted(session_root.glob("*.json")):
        if path not in paths:
            _block(
                report,
                "unmapped_session",
                position,
                str(path.relative_to(Path(mapping.root).resolve())),
            )
    return result


def _archive_records(mapping, chats, report, position):
    root = mapping.path(mapping.dialog_dir)
    result, known = [], {chat.id for chat in chats}
    for index, chat in enumerate(chats):
        try:
            chat_key_bytes(chat.id)
            record = (
                read_archive(root, chat.id)
                if root.exists()
                else {
                    "format": "absent",
                    "deleted": False,
                    "epochs": [],
                    "batches": [],
                    "candidates": [],
                }
            )
            result.append(
                {
                    "position": position + "/chat:" + str(index),
                    "spec": chat.model_dump(mode="json"),
                    "registry_order": index + 1,
                    "archive": record,
                },
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            _block(
                report,
                "archive_invalid",
                position + "/chat:" + str(index),
                error,
            )
    if root.exists():
        candidates = [
            path.name
            for path in root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ]
        candidates += [path.name for path in (root / ".deleted").glob("*")]
        for chat_id in sorted(set(candidates) - known):
            _block(report, "unmapped_archive", position, chat_id)
    return result


def _workspace(mapping, report, index):
    position = (
        "service:" + mapping.scope.service_unit_id + "/workspace:" + str(index)
    )
    root = Path(mapping.root).resolve(strict=True)
    empty_workspace = {
        "position": position,
        "root": str(root),
        "scope": [
            mapping.scope.service_unit_id,
            mapping.scope.tenant_id,
            mapping.scope.source_id,
            mapping.scope.agent_id,
        ],
        "sessions": [],
        "chats": [],
    }
    files = _source_files(mapping)
    report["files"].extend(
        {
            "workspace": index,
            "path": str(path.relative_to(root)),
            "bytes": path.stat().st_size,
        }
        for path in files
    )
    oversized = False
    for path in files:
        if path.stat().st_size > MAX_READ_BYTES:
            oversized = True
            _block(
                report,
                "source_artifact_oversized",
                position,
                str(path.relative_to(root))
                + ": exceeds supported migration reader limit; split-file migration required",
            )
        if "quarantine" in path.name:
            _block(
                report,
                "source_quarantined",
                position,
                str(path.relative_to(root)),
            )
    if oversized:
        return empty_workspace
    try:
        value = load_metadata_json(
            mapping.path(mapping.registry_path).read_text(encoding="utf-8"),
        )
        if value.get("version", 1) != 1:
            raise ValueError("unsupported Chat registry schema")
        chats = ChatsFile.model_validate(value).chats
        if len({chat.id for chat in chats}) != len(chats):
            raise ValueError("duplicate Chat external identity")
    except (OSError, ValueError, KeyError, TypeError) as error:
        _block(report, "registry_invalid", position, error)
        return empty_workspace
    try:
        sessions = _sessions(mapping, chats, report, position)
    except (ValueError, TypeError) as error:
        _block(report, "identity_invalid", position, error)
        sessions = []
    _ledger_generations(mapping, sessions, report, position)
    return {
        "position": position,
        "root": str(root),
        "scope": [
            mapping.scope.service_unit_id,
            mapping.scope.tenant_id,
            mapping.scope.source_id,
            mapping.scope.agent_id,
        ],
        "sessions": sessions,
        "chats": _archive_records(mapping, chats, report, position),
    }


def _ledger_generations(mapping, sessions, report, position):
    mapped = {Path(item["path"]).name for item in sessions}
    active = {
        item.get("legacy", {}).get("root", {}).get("session_generation")
        for item in sessions
    }
    root = mapping.path(str(Path(mapping.sessions_dir) / ".turn-ledger"))
    entries = sorted(root.iterdir()) if root.exists() else []
    for entry in entries:
        path = entry / "metadata.json"
        try:
            if not entry.is_dir() or not path.is_file():
                raise ValueError("ledger entry requires generation metadata")
            value = load_metadata_json(path.read_text(encoding="utf-8"))
            if (
                value.get("version") != 1
                or value.get("session_file") not in mapped
            ):
                raise ValueError(
                    "ledger generation has no trusted Session mapping",
                )
            if path.parent.name not in active:
                _block(
                    report,
                    "retired_generation_root_missing",
                    position,
                    str(path.relative_to(Path(mapping.root).resolve())),
                )
        except (OSError, ValueError, TypeError, KeyError) as error:
            _block(report, "unmapped_ledger_generation", position, error)


def _shares(mappings, shares, report):
    tokens = set()
    for index, source in enumerate(shares):
        try:
            workspace_index = source["snapshot_workspace_index"]
            if type(
                workspace_index,
            ) is not int or not 0 <= workspace_index < len(
                mappings,
            ):
                raise ValueError(
                    "share snapshot requires explicit workspace mapping",
                )
            service_unit_bytes(source["service_unit_id"])
            mapping = mappings[workspace_index]
            if mapping.scope.service_unit_id != source["service_unit_id"]:
                raise ValueError(
                    "share service differs from snapshot workspace",
                )
            snapshot_path = mapping.path(source["snapshot_path"])
            entries = safe_input_entries(
                mapping.root,
                [source["snapshot_path"]],
                recursive=False,
            )
            if snapshot_path not in entries or not snapshot_path.is_file():
                raise ValueError("share snapshot is not a regular file")
            if snapshot_path.stat().st_size > MAX_READ_BYTES:
                raise ValueError("share snapshot exceeds bounded reader size")
            if any(
                stat.S_IMODE(path.stat().st_mode) & 0o222 for path in entries
            ):
                raise ValueError(
                    "source snapshot must be read-only before inventory",
                )
            snapshot = load_metadata_json(
                snapshot_path.read_text(
                    encoding="utf-8",
                ),
            )
            matches = [
                item
                for workspace in report["workspaces"]
                for item in workspace["chats"]
                if item["spec"]["id"] == source["chat_id"]
                and workspace["scope"][0] == source["service_unit_id"]
                and mappings[
                    int(workspace["position"].rsplit(":", 1)[1])
                ].scope.tenant_id
                == source["tenant_id"]
            ]
            record = {
                key: value
                for key, value in source.items()
                if key not in ("snapshot_path", "snapshot_workspace_index")
            }
            required = {
                "service_unit_id",
                "token",
                "chat_id",
                "creator_id",
                "tenant_id",
                "snapshot_key",
                "created_at",
                "access_count",
                "last_access_at",
            }
            if (
                set(record) != required
                or type(record["access_count"]) is not int
                or record["access_count"] < 0
            ):
                raise ValueError("invalid legacy share row")
            ChatShareRecord.model_validate(record)
            ChatShareSnapshot.model_validate(snapshot)
            if record["token"] in tokens:
                raise ValueError("duplicate permanent share token")
            tokens.add(record["token"])
            if len(matches) > 1:
                raise ValueError("share has ambiguous workspace ownership")
            report["shares"].append(
                {
                    "position": "service:"
                    + source["service_unit_id"]
                    + "/share:"
                    + str(index),
                    "service_unit_id": source["service_unit_id"],
                    "scope_kind": "workspace" if matches else "legacy_share",
                    "chat_position": (
                        matches[0]["position"] if matches else None
                    ),
                    "record": record,
                    "snapshot": snapshot,
                },
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            _block(report, "share_invalid", "share:" + str(index), error)


def inventory(mappings, boundary, *, shares=()):
    if not isinstance(boundary, SourceBoundary):
        raise ValueError("fixed source boundary is required")
    mappings = list(mappings)
    roots = [Path(mapping.root).resolve(strict=True) for mapping in mappings]
    if len(set(roots)) != len(roots):
        raise ValueError("duplicate workspace source roots")
    report = {
        "version": 2,
        "source_boundary": {
            **asdict(boundary),
            "evidence_refs": list(boundary.evidence_refs),
        },
        "files": [],
        "workspaces": [],
        "shares": [],
        "blockers": [],
    }
    for index, mapping in enumerate(mappings):
        report["workspaces"].append(_workspace(mapping, report, index))
    _shares(mappings, shares, report)
    report["ready"] = not report["blockers"]
    return report
