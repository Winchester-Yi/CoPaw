# -*- coding: utf-8 -*-
"""Semantic inventory with frozen discovery coverage and stable identities."""

from pathlib import Path
from itertools import zip_longest

from ..chat_mutations import load_metadata_json
from ..bundle import MAX_READ_BYTES
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
    ownership = {}
    for workspace in report["workspaces"] if data["shares"] else ():
        for chat in workspace["chats"]:
            key = (*workspace["scope"][:2], chat["spec"]["id"])
            count, _position = ownership.get(key, (0, None))
            ownership[key] = (count + 1, chat["position"])
    for source in data["shares"]:
        snapshot_path = Path(source["snapshot_path"])
        if snapshot_path.stat().st_size > MAX_READ_BYTES:
            report["blockers"].append(
                {
                    "code": "share_snapshot_oversized",
                    "position": source["position"],
                    "detail": "share snapshot exceeds bounded reader size",
                },
            )
            continue
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
        matches, chat_position = ownership.get(
            (
                source["service_unit_id"],
                source["tenant_id"],
                source["chat_id"],
            ),
            (0, None),
        )
        if matches > 1:
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
                "chat_position": chat_position,
                "record": record,
                "snapshot": load_metadata_json(snapshot_path.read_bytes()),
                "snapshot_path": source["snapshot_path"],
            },
        )


def _inventory_native_files(mapping, report, index, position):
    for path in safe_files(mapping.root):
        size = path.stat().st_size
        relative = str(path.relative_to(mapping.root))
        report["files"].append(
            {"workspace": index, "path": relative, "bytes": size},
        )
        if size > MAX_READ_BYTES:
            report["blockers"].append(
                {
                    "code": "source_artifact_oversized",
                    "position": position,
                    "detail": relative,
                },
            )


def inventory_discovery(data, *, workspace_offset=0):
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
        if registry.exists() and registry.stat().st_size > MAX_READ_BYTES:
            workspace = _workspace(mapping, report, index + workspace_offset)
        elif registry.exists():
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
                workspace = _workspace(
                    mapping,
                    report,
                    index + workspace_offset,
                )
        elif any(
            path.parts[0] in ("sessions", "dialog")
            or "turn-ledger" in path.parts
            for path in (
                path.relative_to(mapping.root)
                for path in safe_files(mapping.root)
            )
        ):
            workspace = _workspace(mapping, report, index + workspace_offset)
        else:
            _inventory_native_files(
                mapping,
                report,
                index + workspace_offset,
                mapped["position"],
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
        _begin_tenant_syncs(manifest)
    report = inventory_discovery(manifest.data)
    for blocker in report["blockers"]:
        if blocker["code"] == "source_modified_after_sync_started":
            manifest.record_source_change(blocker)
    report["source_boundary"] = manifest.data["source_boundary"]
    SourceBoundary(**report["source_boundary"])
    return report


def _begin_tenant_syncs(manifest):
    tenants = {
        (item["service_unit_id"], item["tenant_id"])
        for item in manifest.data["workspaces"]
    }
    tenants.update(
        (item["service_unit_id"], item["tenant_id"])
        for item in manifest.data.get("shares", [])
    )
    manifest.begin_tenant_syncs(tenants)


async def inventory_v3_spooled(
    manifest,
    spool,
    *,
    workers=4,
    io_workers=4,
    memory_bytes=512 * 1024 * 1024,
    begin_sync=False,
):
    """Build the import report from bounded, independently parsed records."""
    from .inventory_parallel import spool_workspaces
    from .spool_report import spool_report

    if begin_sync:
        _begin_tenant_syncs(manifest)
    data = manifest.data
    if (
        data.get("fingerprints")
        and fingerprints(data["services"]) != data["fingerprints"]
    ):
        raise ValueError("source changed since discovery freeze")
    _preflight_share_budget(data, memory_bytes)
    summaries = await spool_workspaces(
        data,
        spool,
        workers=workers,
        io_workers=io_workers,
        memory_bytes=memory_bytes,
    )
    if (
        data.get("fingerprints")
        and fingerprints(data["services"]) != data["fingerprints"]
    ):
        raise ValueError("source changed while building inventory")
    report = spool_report(
        spool,
        summaries,
        shares=[],
        source_boundary=data["source_boundary"],
    )
    _inventory_shares(data, report)
    if (
        data.get("fingerprints")
        and fingerprints(data["services"]) != data["fingerprints"]
    ):
        raise ValueError("source changed while building inventory")
    for blocker in report["blockers"]:
        if blocker["code"] == "source_modified_after_sync_started":
            manifest.record_source_change(blocker)
    SourceBoundary(**report["source_boundary"])
    return report


def _preflight_share_budget(data, memory_bytes):
    """Bound decoded share snapshots before materializing the share list."""
    total = 0
    count = 0
    byte_limit = memory_bytes // 64
    count_limit = memory_bytes // 512
    for share in data.get("shares", []):
        size = Path(share["snapshot_path"]).stat().st_size
        if size > MAX_READ_BYTES:
            raise ValueError("share snapshot exceeds the bounded reader limit")
        total += size
        count += 1
        if total > byte_limit or count > count_limit:
            raise ValueError("share snapshots exceed migration memory budget")


def inventories_equal(left, right):
    """Compare spool-backed reports without retaining decoded workspaces."""
    if any(
        left.get(key) != right.get(key)
        for key in (
            "version",
            "shares",
            "blockers",
            "ready",
            "source_boundary",
        )
    ):
        return False
    if len(left["workspaces"]) != len(right["workspaces"]):
        return False
    marker = object()
    for first, second in zip_longest(
        left["workspaces"],
        right["workspaces"],
        fillvalue=marker,
    ):
        if first is marker or second is marker or first != second:
            return False
    return all(
        first == second
        for first, second in zip_longest(
            left["files"],
            right["files"],
            fillvalue=marker,
        )
    )


def inventory_workspace(data, index):
    """Parse one workspace preserving its global artifact positions."""
    return inventory_discovery(
        {
            **data,
            "workspaces": [data["workspaces"][index]],
            "shares": [],
            # Global fingerprint checks belong to the coordinator barriers.
            "fingerprints": [],
        },
        workspace_offset=index,
    )
