# -*- coding: utf-8 -*-
"""Semantic inventory with frozen discovery coverage and stable identities."""

from pathlib import Path

from ..chat_mutations import load_metadata_json
from ..identity import WorkspaceScope
from .discovery import fingerprints, source_key, SHARE_FIELDS
from .freshness import changed_source, tenant_key
from .inventory import WorkspaceMapping, _workspace
from .legacy import safe_files
from .manifest import SourceBoundary


def _source_change(position, scope, paths, cutoff):
    changed = changed_source(paths, cutoff)
    if changed is None:
        return None
    path, modified_at_ns = changed
    return {
        "code": "source_modified_after_sync_started",
        "position": position,
        "scope": list(scope),
        "path": str(path),
        "mtime_ns": modified_at_ns,
        "cutoff_ns": cutoff,
    }


def _inventory_shares(data, report):
    for source in data["shares"]:
        snapshot_path = Path(source["snapshot_path"])
        cutoff = data.get("tenant_sync_started_at_ns", {}).get(
            tenant_key(source["service_unit_id"], source["tenant_id"]),
        )
        if cutoff is not None:
            blocker = _source_change(
                source["position"],
                (source["service_unit_id"], source["tenant_id"]),
                [str(snapshot_path), str(snapshot_path.parent)],
                cutoff,
            )
            if blocker:
                report["blockers"].append(blocker)
                continue
        record = {
            key: source[key] for key in (*SHARE_FIELDS, "service_unit_id")
        }
        matches = [
            chat
            for workspace in report["workspaces"]
            if workspace["scope"][0] == source["service_unit_id"]
            and workspace["scope"][1] == source["tenant_id"]
            for chat in workspace["chats"]
            if chat["spec"]["id"] == source["chat_id"]
        ]
        if len(matches) > 1:
            report["blockers"].append(
                {
                    "code": "share_invalid",
                    "position": source["position"],
                    "detail": "share has ambiguous workspace ownership",
                },
            )
            continue
        report["shares"].append(
            {
                "position": source["position"],
                "service_unit_id": source["service_unit_id"],
                "scope_kind": "workspace" if matches else "legacy_share",
                "chat_position": matches[0]["position"] if matches else None,
                "record": record,
                "snapshot": load_metadata_json(snapshot_path.read_bytes()),
                "snapshot_path": source["snapshot_path"],
            },
        )


def inventory_discovery(data):
    """Parse source semantics without manufacturing a frozen source boundary."""
    if (
        data.get("fingerprints")
        and fingerprints(
            data["services"],
        )
        != data["fingerprints"]
    ):
        raise ValueError("source changed since discovery freeze")
    mappings = []
    for item in data["workspaces"]:
        paths = {
            (entry["user_id"], entry["channel"], entry["session_id"]): entry[
                "path"
            ]
            for entry in item.get("session_paths", [])
        }
        mappings.append(
            WorkspaceMapping(
                WorkspaceScope(
                    item["tenant_id"],
                    item["source_id"],
                    item["agent_id"],
                    service_unit_id=item["service_unit_id"],
                ),
                Path(item["root"]),
                session_paths=paths,
            ),
        )
    report = {
        "version": 2,
        "workspaces": [],
        "shares": [],
        "files": [],
        "blockers": [],
    }
    for index, mapping in enumerate(mappings):
        mapped = data["workspaces"][index]
        cutoff = data.get("tenant_sync_started_at_ns", {}).get(
            tenant_key(mapping.scope.service_unit_id, mapping.scope.tenant_id),
        )
        source_paths = [str(mapping.root)]
        for evidence in mapped.get("evidence_refs", []):
            source_paths.extend((evidence, str(Path(evidence).parent)))
        if cutoff is not None:
            blocker = _source_change(
                mapped["position"],
                (mapping.scope.service_unit_id, mapping.scope.tenant_id),
                source_paths,
                cutoff,
            )
            if blocker:
                report["blockers"].append(blocker)
                workspace = {
                    "root": str(mapping.root),
                    "scope": [
                        mapping.scope.service_unit_id,
                        mapping.scope.tenant_id,
                        mapping.scope.source_id,
                        mapping.scope.agent_id,
                    ],
                    "sessions": [],
                    "chats": [],
                    "source_paths": source_paths,
                    "tenant_sync_started_at_ns": cutoff,
                }
                report["workspaces"].append(workspace)
                continue
        registry = mapping.path(mapping.registry_path)
        if registry.exists():
            try:
                raw = load_metadata_json(registry.read_bytes())
            except (OSError, ValueError, UnicodeError):
                raw = None
            required = {
                "id",
                "session_id",
                "user_id",
                "channel",
                "created_at",
                "updated_at",
            }
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("chats"), list)
                or any(
                    not isinstance(chat, dict) or not required.issubset(chat)
                    for chat in raw["chats"]
                )
            ):
                report["blockers"].append(
                    {
                        "code": "registry_invalid",
                        "position": data["workspaces"][index]["position"],
                        "detail": "registry lacks persisted identity or timestamp fields",
                    },
                )
                workspace = {
                    "root": str(mapping.root),
                    "scope": [
                        mapping.scope.service_unit_id,
                        mapping.scope.tenant_id,
                        mapping.scope.source_id,
                        mapping.scope.agent_id,
                    ],
                    "sessions": [],
                    "chats": [],
                }
            else:
                workspace = _workspace(mapping, report, index)
        elif any(
            path.parts[0] in ("sessions", "dialog")
            or "turn-ledger" in path.parts
            for path in (
                path.relative_to(mapping.root)
                for path in safe_files(mapping.root)
            )
        ):
            workspace = _workspace(mapping, report, index)
        else:
            report["files"].extend(
                {
                    "workspace": index,
                    "path": str(path.relative_to(mapping.root)),
                    "bytes": path.stat().st_size,
                }
                for path in safe_files(mapping.root)
            )
            workspace = {
                "root": str(mapping.root),
                "scope": [
                    mapping.scope.service_unit_id,
                    mapping.scope.tenant_id,
                    mapping.scope.source_id,
                    mapping.scope.agent_id,
                ],
                "sessions": [],
                "chats": [],
            }
        workspace["source_paths"] = source_paths
        workspace["tenant_sync_started_at_ns"] = cutoff
        report["workspaces"].append(workspace)
    for mapped, workspace in zip(data["workspaces"], report["workspaces"]):
        workspace["position"] = mapped["position"]
        for session in workspace["sessions"]:
            session["position"] = source_key("session", *session["identity"])
        for chat in workspace["chats"]:
            chat["position"] = source_key(
                "chat",
                *workspace["scope"],
                chat["spec"]["id"],
            )
    _inventory_shares(data, report)
    if (
        data.get("fingerprints")
        and fingerprints(
            data["services"],
        )
        != data["fingerprints"]
    ):
        raise ValueError("source changed while building inventory")
    report["ready"] = not report["blockers"]
    return report


def inventory_v3(manifest, *, begin_sync=False):
    """Re-read frozen source semantics and retain the old importer shape."""
    if begin_sync:
        tenants = {
            (item["service_unit_id"], item["tenant_id"])
            for item in manifest.data["workspaces"]
        }
        tenants.update(
            (item["service_unit_id"], item["tenant_id"])
            for item in manifest.data.get("shares", [])
        )
        for service_unit_id, tenant_id in sorted(tenants):
            manifest.begin_tenant_sync(service_unit_id, tenant_id)
    report = inventory_discovery(manifest.data)
    for blocker in report["blockers"]:
        if blocker["code"] == "source_modified_after_sync_started":
            manifest.record_source_change(blocker)
    report["source_boundary"] = manifest.data["source_boundary"]
    SourceBoundary(**report["source_boundary"])
    return report
