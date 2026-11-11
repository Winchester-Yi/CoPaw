# -*- coding: utf-8 -*-
"""Side-effect-free discovery of existing legacy workspaces and shares."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import os
import stat
from uuid import uuid4

from ..chat_mutations import load_metadata_json
from ..identity import WorkspaceScope, service_unit_bytes
from .legacy import safe_files
from .v3_manifest import V3Manifest

SHARE_FIELDS = (
    "token",
    "chat_id",
    "creator_id",
    "tenant_id",
    "snapshot_key",
    "created_at",
    "access_count",
    "last_access_at",
)


def source_key(*parts):
    """Exact, unambiguous source position, independent of enumeration order."""
    value = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    if len(value.encode("utf-8")) > 2048:
        raise ValueError("source identity exceeds migration key limit")
    return value


def _root(value):
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError(
            "source and target roots must be existing absolute directories",
        )
    if str(root.resolve()) != str(root):
        raise ValueError("root must be canonical and contain no symlink")
    return root


def _overlaps(left, right):
    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _read_json(path):
    return load_metadata_json(path.read_bytes())


def fingerprints(services):
    """Capture complete read-only inputs, rejecting unsafe filesystem objects."""
    result = []
    roots = set()
    for service in services:
        for key in ("source_root", "config_root", "snapshot_root"):
            if service.get(key):
                roots.add(service[key])
    # Nested config/snapshot roots are already covered by their parent.
    roots = [Path(root) for root in sorted(roots)]
    roots = [
        root
        for root in roots
        if not any(
            root != other and root.is_relative_to(other) for other in roots
        )
    ]
    for root in roots:
        for path in [root, *safe_files(root)]:
            if stat.S_IMODE(path.stat().st_mode) & 0o222:
                raise ValueError("source must be read-only before discovery")
            if path.is_file():
                digest = sha256()
                with path.open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                result.append(
                    {
                        "root": str(root),
                        "path": path.relative_to(root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": digest.hexdigest(),
                    },
                )

        def failed(error):
            raise error

        for directory, names, _ in os.walk(root, onerror=failed):
            names.sort()
            if stat.S_IMODE(Path(directory).stat().st_mode) & 0o222:
                raise ValueError("source directory must be read-only")
            result.append(
                {
                    "root": str(root),
                    "path": Path(directory).relative_to(root).as_posix(),
                    "kind": "directory",
                },
            )
    return sorted(result, key=lambda item: (item["root"], item["path"]))


def _block(report, code, position, error):
    report["blockers"].append(
        {"code": code, "position": position, "detail": str(error)},
    )


def _service(item):
    service = dict(item)
    service_unit_bytes(service["service_unit_id"])
    for key in ("source_root", "config_root", "nas_root"):
        service[key] = str(_root(service[key]))
    service.setdefault("original_root", service["nas_root"])
    original = Path(service["original_root"])
    if not original.is_absolute() or ".." in original.parts:
        raise ValueError("original root must be an absolute deployment path")
    if service.get("snapshot_root"):
        service["snapshot_root"] = str(_root(service["snapshot_root"]))
    for key in ("source_root", "config_root", "snapshot_root"):
        if service.get(key) and _overlaps(
            Path(service[key]),
            Path(service["nas_root"]),
        ):
            raise ValueError("target NAS cannot overlap frozen source inputs")
    return service


def _identity(config_path, service):
    from swe.config.context import decode_scope_id, encode_scope_id

    relative = config_path.parent.relative_to(Path(service["config_root"]))
    component = relative.parts[0] if relative.parts else None
    if component and component.startswith("default_"):
        return "default", component[len("default_") :]
    if component:
        try:
            tenant, source = decode_scope_id(component)
            if component not in (
                encode_scope_id(tenant, source),
                "scope.v1." + encode_scope_id(tenant, source),
            ):
                raise ValueError("noncanonical scope encoding")
            return tenant, source
        except (ValueError, UnicodeError):
            registrations = service.get("tenant_sources", {}).get(
                component,
                [],
            )
            if isinstance(registrations, str):
                registrations = [registrations]
            if len(registrations) == 1:
                return component, registrations[0]
            raise ValueError(
                "tenant source ownership is missing or ambiguous",
            ) from None
    if service.get("source_id"):
        return service.get("tenant_id", "default"), service["source_id"]
    raise ValueError("root config requires explicit source ownership evidence")


def _workspace_path(value, config_path, service):
    original = Path(service["original_root"])
    source = Path(service["source_root"])
    path = Path(value)
    if not path.is_absolute():
        # Legacy Workspace uses Path(...).expanduser(): relative paths are CWD-relative.
        if not service.get("original_cwd"):
            raise ValueError(
                "relative workspace path requires original process working directory",
            )
        cwd = Path(service["original_cwd"])
        if not cwd.is_absolute() or ".." in cwd.parts:
            raise ValueError(
                "original process working directory must be absolute",
            )
        path = cwd / path
    if ".." in path.parts or not path.is_relative_to(original):
        raise ValueError("workspace path is outside declared original root")
    relative = path.relative_to(original)
    frozen = source / relative
    if (
        frozen.is_symlink()
        or not frozen.is_dir()
        or frozen.resolve() != frozen
    ):
        raise ValueError("workspace is missing or unsafe in frozen source")
    return frozen, path


def _profiles(service, report):
    workspaces = []
    configs = [Path(service["config_root"]) / "config.json"]
    configs.extend(sorted(Path(service["config_root"]).glob("*/config.json")))
    for config in configs:
        if config.is_symlink():
            _block(
                report,
                "source_invalid",
                str(config),
                "source contains symlink; explicit regular-file snapshot required",
            )
            continue
        if not config.exists():
            continue
        try:
            profiles = _read_json(config).get("agents", {}).get("profiles", {})
            if not isinstance(profiles, dict):
                raise ValueError("agent profiles must be a mapping")
            if not profiles:
                continue
            tenant, source = _identity(config, service)
            for key, profile in sorted(profiles.items()):
                if key != profile["id"]:
                    raise ValueError("profile key and Agent ID disagree")
                frozen, runtime = _workspace_path(
                    profile["workspace_dir"],
                    config,
                    service,
                )
                agent_metadata = frozen / "agent.json"
                if agent_metadata.is_symlink():
                    _block(
                        report,
                        "source_invalid",
                        str(agent_metadata),
                        "source contains symlink; explicit regular-file snapshot required",
                    )
                    continue
                if _read_json(agent_metadata)["id"] != key:
                    raise ValueError(
                        "profile and agent.json identities disagree",
                    )
                WorkspaceScope(
                    tenant,
                    source,
                    key,
                    service_unit_id=service["service_unit_id"],
                )
                workspaces.append(
                    {
                        "root": str(frozen),
                        "runtime_root": str(runtime),
                        "service_unit_id": service["service_unit_id"],
                        "tenant_id": tenant,
                        "source_id": source,
                        "agent_id": key,
                        "position": source_key(
                            "workspace",
                            service["service_unit_id"],
                            frozen.relative_to(
                                service["source_root"],
                            ).as_posix(),
                        ),
                        "evidence_refs": [
                            str(config),
                            str(frozen / "agent.json"),
                        ],
                    },
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            _block(report, "workspace_identity_invalid", str(config), error)
    return workspaces


def _apply_override(path, service, overrides, report):
    position = source_key(
        "workspace",
        service["service_unit_id"],
        path.relative_to(service["source_root"]).as_posix(),
    )
    matches = [
        entry for entry in overrides if entry.get("position") == position
    ]
    if not matches:
        return False
    try:
        if len(matches) != 1:
            raise ValueError("duplicate candidate override")
        entry = matches[0]
        if (
            entry.get("discovery_version") != report["discovery_version"]
            or not entry.get("reason")
            or not entry.get("evidence_refs")
            or not all(
                isinstance(ref, str) and ref for ref in entry["evidence_refs"]
            )
        ):
            raise ValueError(
                "override requires bound evidence version and reason",
            )
        scope = WorkspaceScope(
            entry["tenant_id"],
            entry["source_id"],
            entry["agent_id"],
            service_unit_id=service["service_unit_id"],
        )
        if (path / "agent.json").exists() and _read_json(path / "agent.json")[
            "id"
        ] != scope.agent_id:
            raise ValueError("override disagrees with Agent identity evidence")
        from .inventory import WorkspaceMapping

        session_paths = entry.get("session_paths", [])
        mapping = WorkspaceMapping(scope, path)
        for item in session_paths:
            mapping.path(item["path"])
            from ..identity import SessionIdentity

            SessionIdentity(
                scope,
                item["user_id"],
                item["channel"],
                item["session_id"],
            )
        report["workspaces"].append(
            {
                "root": str(path),
                "runtime_root": str(
                    Path(service["original_root"])
                    / path.relative_to(service["source_root"]),
                ),
                "service_unit_id": scope.service_unit_id,
                "tenant_id": scope.tenant_id,
                "source_id": scope.source_id,
                "agent_id": scope.agent_id,
                "position": position,
                "evidence_refs": list(entry["evidence_refs"]),
                "session_paths": session_paths,
            },
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        _block(report, "override_invalid", position, error)
    return True


def _coverage(service, known, report, overrides):
    for directory, names, files in os.walk(
        service["source_root"],
        followlinks=False,
    ):
        path = Path(directory)
        if any(name in files for name in ("chats.json", "agent.json")) or any(
            name in names for name in ("sessions", "dialog")
        ):
            if path not in known and not _apply_override(
                path,
                service,
                overrides,
                report,
            ):
                _block(
                    report,
                    "unregistered_workspace",
                    source_key(
                        "workspace",
                        service["service_unit_id"],
                        path.relative_to(service["source_root"]).as_posix(),
                    ),
                    "conversation candidate lacks verified profile ownership",
                )


def _json_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _snapshot_matches(services, expected):
    matches = []
    for service in services:
        if not service.get("snapshot_root"):
            continue
        path = Path(service["snapshot_root"]) / expected
        if not path.exists():
            continue
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("unsafe share snapshot path")
        matches.append((service, path))
    return matches


def _shares(services, shares, report):
    tokens = set()
    for source in shares:
        try:
            record = {key: _json_time(source[key]) for key in SHARE_FIELDS}
            if (
                type(record["access_count"]) is not int
                or record["access_count"] < 0
            ):
                raise ValueError(
                    "share access count must preserve a nonnegative original integer",
                )
            token, tenant = record["token"], record["tenant_id"]
            if not all(
                isinstance(value, str)
                and value
                and value not in (".", "..")
                and not any(char in value for char in ("/", "\\", "\x00"))
                for value in (token, tenant)
            ):
                raise ValueError("invalid share path identity")
            if token in tokens:
                raise ValueError("duplicate share token")
            tokens.add(token)
            expected = tenant + "/chat_shares/" + token + ".json"
            if record["snapshot_key"] != expected:
                raise ValueError(
                    "share snapshot key differs from original safe path",
                )
            matches = _snapshot_matches(services, expected)
            if len(matches) != 1:
                raise ValueError(
                    "share service ownership is absent or ambiguous",
                )
            service, path = matches[0]
            from swe.app.chat_sharing.models import (
                ChatShareRecord,
                ChatShareSnapshot,
            )

            ChatShareRecord.model_validate(record)
            ChatShareSnapshot.model_validate(_read_json(path))
            report["shares"].append(
                {
                    **record,
                    "service_unit_id": service["service_unit_id"],
                    "snapshot_path": str(path),
                    "position": source_key(
                        "share",
                        service["service_unit_id"],
                        token,
                    ),
                },
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            _block(
                report,
                "share_invalid",
                str(source.get("token", "share")),
                error,
            )


def _registered_overrides(report):
    registered = list(report["workspaces"])
    for item in registered:
        if (
            not any(
                entry.get("position") == item["position"]
                for entry in report["overrides"]
            )
            or "session_paths" in item
        ):
            continue
        offset = len(report["workspaces"])
        service = next(
            service
            for service in report["services"]
            if service["service_unit_id"] == item["service_unit_id"]
        )
        _apply_override(
            Path(item["root"]),
            service,
            report["overrides"],
            report,
        )
        if len(report["workspaces"]) > offset:
            replacement = report["workspaces"].pop()
            if any(
                replacement[key] != item[key]
                for key in ("tenant_id", "source_id", "agent_id")
            ):
                _block(
                    report,
                    "override_invalid",
                    item["position"],
                    "override disagrees with registered workspace ownership",
                )
            elif not replacement["session_paths"]:
                _block(
                    report,
                    "override_invalid",
                    item["position"],
                    "registered override requires explicit exceptional Session paths",
                )
            else:
                item["session_paths"] = replacement["session_paths"]
                item["evidence_refs"] += replacement["evidence_refs"]


def _unique_workspaces(report):
    roots, scopes = set(), set()
    for item in report["workspaces"]:
        scope = tuple(
            item[key]
            for key in (
                "service_unit_id",
                "tenant_id",
                "source_id",
                "agent_id",
            )
        )
        if item["root"] in roots or scope in scopes:
            _block(
                report,
                "duplicate_workspace",
                item["position"],
                "duplicate root or scope",
            )
        roots.add(item["root"])
        scopes.add(scope)
    positions = {
        item["position"]
        for item in report["workspaces"]
        if "session_paths" in item
    }
    for entry in report["overrides"]:
        if entry.get("position") not in positions:
            _block(
                report,
                "override_invalid",
                entry.get("position", "override"),
                "override does not resolve an unregistered candidate",
            )


def discover_services(services, *, shares=(), overrides=()):
    """Return complete discovery evidence without opening a DB or modifying inputs."""
    report = {
        "version": 3,
        "discovery_version": 1,
        "services": [],
        "workspaces": [],
        "shares": [],
        "fingerprints": [],
        "blockers": [],
        "ready": False,
        "overrides": deepcopy(list(overrides)),
    }
    for item in services:
        try:
            report["services"].append(_service(item))
        except (OSError, ValueError, KeyError, TypeError) as error:
            _block(
                report,
                "service_invalid",
                str(item.get("service_unit_id", "service")),
                error,
            )
    report["services"].sort(
        key=lambda item: item["service_unit_id"].encode("utf-8"),
    )
    names = [item["service_unit_id"] for item in report["services"]]
    if not names or len(names) != len(set(names)):
        _block(
            report,
            "service_invalid",
            "services",
            "nonempty unique services required",
        )
    for service in report["services"]:
        for other in report["services"]:
            for key in ("source_root", "config_root", "snapshot_root"):
                if other.get(key) and _overlaps(
                    Path(service["nas_root"]),
                    Path(other[key]),
                ):
                    _block(
                        report,
                        "service_invalid",
                        service["service_unit_id"],
                        "target NAS overlaps another service's source inputs",
                    )
    for index, service in enumerate(report["services"]):
        for other in report["services"][:index]:
            if _overlaps(Path(service["nas_root"]), Path(other["nas_root"])):
                _block(
                    report,
                    "service_invalid",
                    service["service_unit_id"],
                    "service NAS roots overlap",
                )
        items = _profiles(service, report)
        report["workspaces"].extend(items)
        _coverage(
            service,
            {Path(item["root"]) for item in items},
            report,
            report["overrides"],
        )
    _registered_overrides(report)
    _unique_workspaces(report)
    report["workspaces"].sort(
        key=lambda item: item["position"].encode("utf-8"),
    )
    _shares(report["services"], shares, report)
    if not report["blockers"]:
        from .inventory_v3 import inventory_discovery

        semantic = inventory_discovery(report)
        report["blockers"].extend(semantic["blockers"])
        for item, workspace in zip(
            report["workspaces"],
            semantic["workspaces"],
        ):
            item["counts"] = {
                "sessions": len(workspace["sessions"]),
                "chats": len(workspace["chats"]),
            }
            item["empty"] = not any(item["counts"].values())
    report["ready"] = not report["blockers"]
    return report


def freeze_discovery(report, path, boundary, application_schema):
    """Revalidate discovery and atomically save a v3 manifest outside the source."""
    if not report.get("ready") or report.get("blockers"):
        raise ValueError("discovery contains blockers")
    current = discover_services(
        report["services"],
        shares=report["shares"],
        overrides=report.get("overrides", ()),
    )
    if current["fingerprints"] != report["fingerprints"]:
        raise ValueError("source changed since discovery")
    if current != report:
        raise ValueError(
            "discovery report differs from verified source evidence",
        )
    data = {
        **deepcopy(report),
        "version": 3,
        "workflow": "direct",
        "migration_id": str(uuid4()),
        "source_boundary": {
            **asdict(boundary),
            "evidence_refs": list(boundary.evidence_refs),
        },
        "offline": {"application_schema": application_schema},
        "assignments": {},
        "batches": {},
    }
    manifest = V3Manifest(path, data)
    target = {
        "schema": application_schema,
        "nas_roots": {
            item["service_unit_id"]: item["nas_root"]
            for item in report["services"]
        },
        "migration_id": manifest.migration_id,
    }
    manifest.data["target"] = target
    manifest._validate_target(target)
    output = Path(path).absolute()
    for service in report["services"]:
        for key in ("source_root", "config_root", "snapshot_root"):
            if service.get(key) and output.is_relative_to(service[key]):
                raise ValueError(
                    "manifest output cannot be inside frozen source",
                )
    output.parent.mkdir(parents=True, exist_ok=True)
    with manifest._lock():
        if output.exists():
            raise FileExistsError(output)
        manifest._persist()
    return manifest
