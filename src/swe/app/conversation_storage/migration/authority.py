# -*- coding: utf-8 -*-
"""Completion receipts that bind migrated data to runtime and restore roots."""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID, uuid4

from ..chat_mutations import load_metadata_json
from ..errors import IntegrityError
from ..identity import WorkspaceScope, service_unit_bytes


def _json(value, name):
    if isinstance(value, str):
        try:
            value = load_metadata_json(value)
        except (ValueError, TypeError) as error:
            raise IntegrityError("invalid migration " + name) from error
    if not isinstance(value, dict):
        raise IntegrityError("invalid migration " + name)
    return value


def _controls_exist(row):
    count = int(row.get("n") or 0) if row else 0
    if count not in (0, 2):
        raise IntegrityError(
            "migration authority control tables are incomplete",
        )
    return count == 2


def validate_receipt(receipt):
    """Validate the bounded metadata proof without touching business data."""
    receipt = _json(receipt, "authority receipt")
    if (
        set(receipt) != {"version", "migration_id", "services"}
        or type(receipt["version"]) is not int
        or receipt["version"] != 1
        or not isinstance(receipt["services"], dict)
        or not receipt["services"]
    ):
        raise IntegrityError("invalid migration authority receipt")
    try:
        if str(UUID(receipt["migration_id"])) != receipt["migration_id"]:
            raise ValueError("noncanonical UUID")
        scopes, roots = set(), set()
        for service, entry in receipt["services"].items():
            service_unit_bytes(service)
            if (
                set(entry) != {"nas_root", "workspaces"}
                or not Path(entry["nas_root"]).is_absolute()
                or not isinstance(entry["workspaces"], list)
            ):
                raise ValueError("invalid service binding")
            for item in entry["workspaces"]:
                scope = WorkspaceScope.from_values(item["scope"])
                if (
                    set(item) != {"root", "scope"}
                    or scope.service_unit_id != service
                    or not Path(item["root"]).is_absolute()
                    or scope in scopes
                    or (service, item["root"]) in roots
                ):
                    raise ValueError("invalid workspace binding")
                scopes.add(scope)
                roots.add((service, item["root"]))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise IntegrityError(
            "invalid migration authority receipt binding",
        ) from error
    return receipt


async def _job(database, migration_id):
    rows = await database.fetch_all(
        "SELECT migration_id,state,target_binding_text,verification_text "
        "FROM swe_conversation_migration_jobs WHERE migration_id=%s",
        (migration_id.encode(),),
    )
    if len(rows) != 1:
        raise IntegrityError("migration authority job is missing or ambiguous")
    return rows[0]


async def validate_authority(database, document):
    """Check an optional v3 receipt, and reject omitted receipts for unfinished jobs."""
    tables = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
        "('swe_conversation_migration_jobs','swe_conversation_migration_entries')",
    )
    if not _controls_exist(tables):
        if document.get("migration_authority") is not None:
            raise IntegrityError(
                "migration authority control tables are missing",
            )
        return None

    receipt = document.get("migration_authority")
    if receipt is None:
        rows = await database.fetch_all(
            "SELECT state,target_binding_text FROM swe_conversation_migration_jobs",
        )
        for row in rows:
            binding = _json(row["target_binding_text"], "target binding")
            if document.get("service_unit_id") in binding.get("nas_roots", {}):
                raise IntegrityError(
                    "migration authority receipt is required for this service",
                )
        return None
    receipt = validate_receipt(receipt)
    service = document.get("service_unit_id")
    service_unit_bytes(service)
    entry = receipt["services"].get(service)
    if not isinstance(entry, dict) or entry.get("nas_root") != document.get(
        "nas_root",
    ):
        raise IntegrityError("migration authority service binding differs")
    if entry.get("workspaces") != document.get("workspaces"):
        raise IntegrityError("migration authority workspace binding differs")

    row = await _job(database, receipt["migration_id"])
    if row["state"] != "ready":
        raise IntegrityError("migration authority job is not ready")
    binding = _json(row["target_binding_text"], "target binding")
    verification = _json(row["verification_text"], "verification receipt")
    if verification.get("authority") != receipt:
        raise IntegrityError(
            "migration authority receipt differs from verified job",
        )
    identity = await database.fetch_one(
        "SELECT @@server_uuid AS server_uuid,DATABASE() AS schema_name",
    )
    if (
        binding.get("server_uuid") != identity["server_uuid"]
        or binding.get("schema") != identity["schema_name"]
        or binding.get("migration_id") != receipt["migration_id"]
        or binding.get("nas_roots", {}).get(service) != document["nas_root"]
        or {
            service: entry["nas_root"]
            for service, entry in receipt["services"].items()
        }
        != binding.get("nas_roots")
    ):
        raise IntegrityError(
            "migration authority database or NAS binding differs",
        )
    return receipt


async def backup_authority(database, scope):
    """Return the receipt's service-local workspace proof for one scope backup."""
    from ..tenant_admission import partial_admission

    partial = await partial_admission(database, scope)
    if partial is not None:
        workspaces = await partial.require(scope.tenant_id)
        selected = [
            item for item in workspaces if item["scope"] == list(scope.values)
        ]
        if not selected:
            raise IntegrityError(
                "backup scope is outside tenant publication proof",
            )
        return {
            "version": 1,
            "migration_id": partial.document["migration_id"],
            "services": {
                scope.service_unit_id: {
                    "nas_root": partial.document["nas_root"],
                    "workspaces": selected,
                },
            },
        }
    tables = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
        "('swe_conversation_migration_jobs','swe_conversation_migration_entries')",
    )
    if not _controls_exist(tables):
        return None
    rows = await database.fetch_all(
        "SELECT migration_id,state,verification_text FROM "
        "swe_conversation_migration_jobs WHERE state='ready' ORDER BY updated_at DESC",
    )
    wanted = [
        scope.service_unit_id,
        scope.tenant_id,
        scope.source_id,
        scope.agent_id,
    ]
    for row in rows:
        verification = _json(row["verification_text"], "verification receipt")
        receipt = verification.get("authority")
        if not isinstance(receipt, dict):
            continue
        validate_receipt(receipt)
        service = receipt.get("services", {}).get(scope.service_unit_id)
        if service and any(
            item.get("scope") == wanted for item in service["workspaces"]
        ):
            await validate_authority(
                database,
                {
                    "service_unit_id": scope.service_unit_id,
                    "nas_root": service["nas_root"],
                    "workspaces": service["workspaces"],
                    "migration_authority": receipt,
                },
            )
            return {
                "version": 1,
                "migration_id": receipt["migration_id"],
                "services": {
                    scope.service_unit_id: {
                        "nas_root": service["nas_root"],
                        "workspaces": [
                            item
                            for item in service["workspaces"]
                            if item["scope"] == wanted
                        ],
                    },
                },
            }
    return None


async def restore_authorities(
    database,
    receipts,
    scopes,
    roots,
    *,
    workspace_roots=None,
):
    """Install a new non-resumable ready proof for the exact restored scopes."""
    from ..tenant_admission import partial_jobs

    if await partial_jobs(database):
        raise IntegrityError("restore target has an active partial migration")
    grouped = {}
    source_ids = set()
    for receipt in receipts:
        receipt = validate_receipt(receipt)
        source_ids.add(receipt.get("migration_id"))
        for service, entry in receipt.get("services", {}).items():
            grouped.setdefault(service, []).append(entry)
    actual = {}
    for scope in scopes:
        actual.setdefault(scope.service_unit_id, set()).add(
            tuple(scope.values),
        )
    if (
        not actual
        or set(actual) != set(roots)
        or not source_ids
        or None in source_ids
    ):
        raise IntegrityError("restore authority service set differs")
    services = {}
    for service, scope_set in actual.items():
        entries = grouped.get(service, [])
        if not entries or service not in roots:
            raise IntegrityError("restore authority service is missing")
        workspaces = {
            tuple(item["scope"]): item
            for entry in entries
            for item in entry.get("workspaces", [])
        }
        if not scope_set.issubset(workspaces):
            raise IntegrityError(
                "restore authority scope is outside backup receipt",
            )
        if workspace_roots is None or any(
            scope not in workspace_roots for scope in scope_set
        ):
            raise IntegrityError(
                "restore authority requires explicit workspace roots",
            )
        for scope in scope_set:
            root = Path(workspace_roots[scope])
            if (
                not root.is_absolute()
                or not root.is_dir()
                or root.resolve() != root
            ):
                raise IntegrityError(
                    "restore authority workspace root is missing or unsafe",
                )
        services[service] = {
            "nas_root": roots[service],
            "workspaces": [
                {"root": str(workspace_roots[scope]), "scope": list(scope)}
                for scope in sorted(scope_set)
            ],
        }

    identity = await database.fetch_one(
        "SELECT @@server_uuid AS server_uuid,DATABASE() AS schema_name",
    )
    migration_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    target = {
        "server_uuid": identity["server_uuid"],
        "schema": identity["schema_name"],
        "migration_id": migration_id,
        "nas_roots": {service: roots[service] for service in sorted(roots)},
    }
    proof = {"version": 1, "migration_id": migration_id, "services": services}
    validate_receipt(proof)
    verification = {
        "authority": proof,
        "restored": True,
        "source_migration_ids": sorted(source_ids),
        "actual_scopes": {
            service: [list(scope) for scope in sorted(values)]
            for service, values in actual.items()
        },
    }
    await database.insert(
        "INSERT INTO swe_conversation_migration_jobs "
        "(migration_id,manifest_version,state,target_binding_text,"
        "source_binding_text,verification_text,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            migration_id.encode(),
            3,
            "ready",
            json.dumps(target, sort_keys=True),
            json.dumps(
                {"restored": True, "source_migration_ids": sorted(source_ids)},
                sort_keys=True,
            ),
            json.dumps(verification, sort_keys=True),
            now,
            now,
        ),
    )
    return proof
