# -*- coding: utf-8 -*-
"""Conservative failure attribution for independently published tenants."""

from copy import deepcopy
import json
from pathlib import Path

from ..identity import WorkspaceScope


def _configs(service):
    from .discovery import _identity

    root = Path(service["config_root"])
    from .failure_scope import config_candidates

    candidates = config_candidates(root)
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            tenant, source = _identity(path, service)
            WorkspaceScope(
                tenant,
                source,
                "",
                service_unit_id=service["service_unit_id"],
            )
        except (ValueError, TypeError, KeyError):
            continue
        yield path, tenant


def _path(position, services):
    try:
        parts = json.loads(position)
    except (ValueError, TypeError):
        return Path(position) if isinstance(position, str) else None
    if isinstance(parts, list) and len(parts) == 3 and parts[0] == "workspace":
        service = next(
            (s for s in services if s["service_unit_id"] == parts[1]),
            None,
        )
        if service:
            return Path(service["source_root"]) / parts[2]
    return None


def isolate_discovery(report, shares, validator):
    """Preserve all blockers and quarantine every affected tenant before parsing."""
    from .discovery import _overlaps
    from .inventory_v3 import inventory_discovery

    services = report["services"]
    tenants, paths, positions = {}, [], {}
    for service in services:
        name = service["service_unit_id"]
        for frozen in services:
            for key in ("source_root", "config_root", "snapshot_root"):
                if frozen.get(key) and _overlaps(
                    Path(service["original_root"]),
                    Path(frozen[key]),
                ):
                    raise ValueError(
                        "tenant isolation requires frozen inputs separate from runtime roots",
                    )
        for config, tenant in _configs(service):
            key = (name, tenant)
            tenants.setdefault(key, [])
            paths.append((config, key))
            relative = config.parent.relative_to(service["config_root"])
            if relative.parts:
                paths.append((Path(service["source_root"]) / relative, key))
    for item in report["workspaces"]:
        key = (item["service_unit_id"], item["tenant_id"])
        tenants.setdefault(key, [])
        paths.append((Path(item["root"]), key))
        positions[item["position"]] = {key}
    for item in report["shares"]:
        key = (item["service_unit_id"], item["tenant_id"])
        tenants.setdefault(key, [])
        positions[item["position"]] = {key}
    # A missing/ambiguous snapshot cannot establish service ownership. Block the
    # same trustworthy account in every declared service, never guess one owner.
    for item in shares:
        tenant = item.get("tenant_id")
        if not isinstance(tenant, str):
            continue
        matched = [
            s for s in report["shares"] if s["token"] == item.get("token")
        ]
        candidates = {
            (s["service_unit_id"], tenant) for s in (matched or services)
        }
        for key in candidates:
            tenants.setdefault(key, [])
        positions.setdefault(str(item.get("token", "share")), set()).update(
            candidates,
        )
    _attribute_blockers(report, services, tenants, paths, positions)
    for key in sorted(tenants):
        if tenants[key]:
            continue
        selected = {
            **report,
            "blockers": [],
            "workspaces": [
                w
                for w in report["workspaces"]
                if (w["service_unit_id"], w["tenant_id"]) == key
            ],
            "shares": [
                s
                for s in report["shares"]
                if (s["service_unit_id"], s["tenant_id"]) == key
            ],
        }
        from .spool import MigrationCapacityError, MigrationDiskCapacityError
        from .source_inputs import SourceInputInvalid

        try:
            semantic = (validator or inventory_discovery)(selected)
        except MigrationDiskCapacityError:
            raise
        except (
            MigrationCapacityError,
            SourceInputInvalid,
            FileNotFoundError,
        ) as error:
            blocker = {
                "code": "tenant_input_invalid",
                "position": json.dumps(key),
                "detail": str(error),
            }
            tenants[key].append(blocker)
            report["blockers"].append(blocker)
            continue
        tenants[key].extend(semantic["blockers"])
        report["blockers"].extend(semantic["blockers"])
        for item, workspace in zip(
            selected["workspaces"],
            semantic["workspaces"],
        ):
            item["counts"] = {
                "sessions": len(workspace["sessions"]),
                "chats": len(workspace["chats"]),
            }
            item["empty"] = not any(item["counts"].values())
    report["tenants"] = [
        {
            "service_unit_id": key[0],
            "tenant_id": key[1],
            "state": "failed" if failures else "discovered",
            "blockers": deepcopy(failures),
        }
        for key, failures in sorted(tenants.items())
    ]
    report["status"] = "partial_success" if report["blockers"] else "success"


def _attribute_blockers(report, services, tenants, paths, positions):
    for blocker in report["blockers"]:
        position = blocker["position"]
        owners = positions.get(position, set())
        path = _path(position, services)
        if not owners and path is not None:
            matches = [
                (p, k) for p, k in paths if path == p or path.is_relative_to(p)
            ]
            # Longest verified directory wins; a root config must not swallow
            # a nested tenant's candidate.
            if matches:
                depth = max(len(p.parts) for p, _ in matches)
                owners = {k for p, k in matches if len(p.parts) == depth}
        if not owners:
            affected = {
                s["service_unit_id"]
                for s in services
                if position == s["service_unit_id"]
                or (
                    path is not None
                    and any(
                        path.is_relative_to(Path(s[k]))
                        for k in ("source_root", "config_root")
                    )
                )
            }
            owners = {k for k in tenants if not affected or k[0] in affected}
        for key in owners:
            tenants[key].append(blocker)
