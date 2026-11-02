# -*- coding: utf-8 -*-
"""Offline v3 orchestration over a pinned DML-only application connection."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile

import click

from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.migration.discovery import SHARE_FIELDS
from swe.app.conversation_storage.migration.direct_import import (
    DirectImporter,
    DirectTarget,
)
from swe.app.conversation_storage.migration.inventory_v3 import inventory_v3
from swe.app.conversation_storage.migration.journal import (
    MigrationJournal,
    JOBS,
    BUSINESS_TABLES,
)
from swe.app.conversation_storage.migration.preflight import (
    DMLDatabase,
    check_schema,
)
from swe.app.conversation_storage.migration.v3_manifest import V3Manifest
from swe.app.conversation_storage.migration.verify import verify_import


def authority_receipt(manifest):
    services = {
        item["service_unit_id"]: {
            "nas_root": item["nas_root"],
            "workspaces": [],
        }
        for item in manifest.data["services"]
    }
    for item in manifest.data["workspaces"]:
        services[item["service_unit_id"]]["workspaces"].append(
            {
                "root": item["runtime_root"],
                "scope": [
                    item[key]
                    for key in (
                        "service_unit_id",
                        "tenant_id",
                        "source_id",
                        "agent_id",
                    )
                ],
            },
        )
    return {
        "version": 1,
        "migration_id": manifest.migration_id,
        "services": services,
    }


def validate_runtime_roots(data):
    from swe.app.conversation_storage.factory import existing_root

    values = [item["nas_root"] for item in data["services"]]
    values.extend(item["runtime_root"] for item in data["workspaces"])
    for value in values:
        root = Path(value)
        if not root.is_absolute() or str(root.resolve()) != value:
            raise ValueError("runtime root must be absolute and canonical")
        if any(parent.is_symlink() for parent in (root, *root.parents)):
            raise ValueError("runtime root cannot traverse symlinks")
        existing_root(root)


def validate_output(output, data):
    root = Path(output).absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("output cannot traverse symbolic links")
    root = root.resolve()
    protected = {
        Path(item[key]).resolve()
        for item in data["services"]
        for key in ("source_root", "config_root", "snapshot_root", "nas_root")
        if item.get(key)
    }
    if any(
        root == path or root.is_relative_to(path) or path.is_relative_to(root)
        for path in protected
    ):
        raise ValueError("output must not overlap source or NAS roots")
    return root


def _stamp(value):
    from datetime import datetime, timezone

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            return _stamp(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return value


async def compare_original_shares(database, data):
    rows = await database.fetch_all(
        "SELECT "
        + ",".join(SHARE_FIELDS)
        + " FROM swe_chat_shares ORDER BY token",
    )

    def normalized(row):
        return {
            key: (
                _stamp(row.get(key))
                if key in ("created_at", "last_access_at")
                else row.get(key)
            )
            for key in SHARE_FIELDS
        }

    actual = {row["token"]: normalized(row) for row in rows}
    expected = {row["token"]: normalized(row) for row in data["shares"]}
    if (
        len(actual) != len(rows)
        or len(expected) != len(data["shares"])
        or actual != expected
    ):
        raise ValueError(
            "original share set, identity, timestamps or counters changed",
        )


def _write_json(path, value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(
                "existing completion output differs: " + str(path),
            )
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_outputs(output, receipt):
    output.mkdir(parents=True, exist_ok=True)
    for service, entry in receipt["services"].items():
        # Service identifiers are domain values; do not interpolate into paths.
        name = sha256(service.encode()).hexdigest()
        _write_json(
            output / (name + ".runtime.json"),
            {
                "version": 2,
                "service_unit_id": service,
                **entry,
                "migration_authority": receipt,
            },
        )
    _write_json(output / "completion.json", receipt)


async def _inspect_job(db, journal):
    target, source = journal._bindings()
    jobs = await db.fetch_all("SELECT * FROM " + JOBS)
    own = None
    for row in jobs:
        if row["migration_id"] != journal.migration_id:
            raise ValueError("another job owns this application schema")
        if (
            row["target_binding_text"] != target
            or row["source_binding_text"] != source
            or row["manifest_version"] != 3
        ):
            raise ValueError("migration source or target binding changed")
        own = row
    if own is None:
        for table in BUSINESS_TABLES:
            if await db.fetch_one("SELECT id FROM " + table + " LIMIT 1"):
                raise ValueError(
                    "first migration requires six empty business tables",
                )
    else:
        await journal.validate_ownership()
    return own


# pylint: disable-next=too-many-statements
async def _execute(operation, manifest, inventory, output):
    from swe.database.config import get_database_config
    from swe.database.connection import DatabaseConnection

    config = get_database_config()
    schema = manifest.data["offline"]["application_schema"]
    if config.database != schema:
        raise ValueError("configured application schema differs from manifest")
    target = DirectTarget(schema, schema, manifest.migration_id)
    bundles = {
        item["service_unit_id"]: BundleStore(item["nas_root"])
        for item in manifest.data["services"]
    }
    binding = {
        "schema": schema,
        "migration_id": manifest.migration_id,
        "nas_roots": {
            key: str(store.root.resolve()) for key, store in bundles.items()
        },
    }
    manifest._validate_target(binding)
    if manifest.data.get("target") != binding:
        raise ValueError(
            "frozen target binding differs from application target",
        )
    control = DatabaseConnection(
        config.model_copy(update={"time_zone": "+00:00"}),
    )
    try:
        await control.connect()
        # DatabaseConnection.named_lock pins every nested operation to its lease.
        async with control.named_lock(
            "conversation-migration:" + schema,
            timeout=0,
        ):
            db = DMLDatabase(control)
            identity = await db.fetch_one(
                "SELECT @@server_uuid AS server_uuid,DATABASE() AS schema_name",
            )
            if (
                identity["schema_name"] != schema
                or not identity["server_uuid"]
            ):
                raise ValueError(
                    "database identity differs from application target",
                )
            journal = MigrationJournal(
                db,
                manifest,
                server_uuid=identity["server_uuid"],
            )
            baseline = await check_schema(
                db,
                schema,
                finalized=operation == "finalize",
            )
            await compare_original_shares(db, manifest.data)
            job = await _inspect_job(db, journal)
            if operation == "preflight":
                return {
                    "version": 3,
                    "migration_id": manifest.migration_id,
                    "passed": True,
                    "state": job["state"] if job else "planned",
                }
            if operation in ("import", "resume"):
                result = await DirectImporter(
                    db,
                    bundles,
                    manifest,
                    target,
                    journal=journal,
                ).run(inventory)
            else:
                if job is None or job["state"] not in (
                    "imported",
                    "verified",
                    "ready",
                ):
                    raise ValueError(
                        "complete import is required before verification",
                    )
                await journal.hydrate(persist=False)
                readonly_manifest = SimpleNamespace(
                    data=manifest.data,
                    migration_id=manifest.migration_id,
                    set_batch_state=lambda *_: None,
                )
                result = await verify_import(
                    db,
                    bundles,
                    readonly_manifest,
                    inventory,
                    target,
                )
                if not result["passed"]:
                    raise ValueError(
                        "semantic verification failed: "
                        + json.dumps(result["blockers"]),
                    )
            # Re-read the complete frozen source and old SQL shares before completion.
            if inventory_v3(manifest) != inventory:
                raise ValueError("source inventory changed during operation")
            await compare_original_shares(db, manifest.data)
            if (
                await check_schema(
                    db,
                    schema,
                    finalized=operation == "finalize",
                )
                != baseline
            ):
                raise ValueError(
                    "schema or original share definition changed during operation",
                )
            if operation == "finalize":
                from swe.app.conversation_storage.migration.authority import (
                    validate_receipt,
                )

                validate_runtime_roots(manifest.data)
                receipt = validate_receipt(authority_receipt(manifest))
                if job["state"] != "ready":
                    if job["state"] == "imported":
                        await journal.mark_state(
                            "verified",
                            {"report": result},
                        )
                    await journal.mark_state(
                        "ready",
                        {"report": result, "authority": receipt},
                    )
                elif (
                    json.loads(job["verification_text"]).get("authority")
                    != receipt
                ):
                    raise ValueError("ready completion receipt differs")
                _write_outputs(output, receipt)
                result = {
                    **result,
                    "authority": receipt,
                    "output_dir": str(output),
                }
            return result
    finally:
        await control.close()


def run_v3_command(operation, manifest_path, apply=False, output_dir=None):
    if operation not in (
        "inventory",
        "plan",
        "inspect",
        "preflight",
        "import",
        "resume",
        "verify",
        "finalize",
    ):
        raise ValueError("unsupported v3 command")
    manifest = V3Manifest.load(manifest_path)
    output = None
    if operation == "finalize":
        if output_dir is None:
            raise ValueError("finalize requires --output-dir")
        output = validate_output(output_dir, manifest.data)
    for service in manifest.data["services"]:
        nas = Path(service["nas_root"]).resolve()
        for key in ("source_root", "config_root", "snapshot_root"):
            if service.get(key):
                source = Path(service[key]).resolve()
                if (
                    nas == source
                    or nas.is_relative_to(source)
                    or source.is_relative_to(nas)
                ):
                    raise ValueError("source and target NAS roots overlap")
    inventory = inventory_v3(manifest)
    if operation in ("inventory", "plan", "inspect"):
        return {
            "version": 3,
            "operation": operation,
            "migration_id": manifest.migration_id,
            "preview": True,
            "application_schema": manifest.data["offline"][
                "application_schema"
            ],
            "source_boundary": inventory["source_boundary"],
            "source_scopes": [
                {
                    "scope": item["scope"],
                    "root": item["root"],
                    "sessions": len(item["sessions"]),
                    "chats": len(item["chats"]),
                }
                for item in inventory["workspaces"]
            ],
            "files": len(inventory["files"]),
            "workspaces": len(inventory["workspaces"]),
            "shares": len(inventory["shares"]),
            "blockers": inventory["blockers"],
            "ready": inventory["ready"],
        }
    if not inventory.get("ready") or inventory.get("blockers"):
        raise ValueError("frozen inventory has blockers")
    if operation in ("import", "resume", "finalize") and not apply:
        return {
            "version": 3,
            "operation": operation,
            "migration_id": manifest.migration_id,
            "apply": False,
            "ready": True,
        }
    return asyncio.run(_execute(operation, manifest, inventory, output))


def register_v3_commands(group):
    for operation in ("preflight", "resume", "finalize"):

        def command(manifest, _operation=operation, **options):
            try:
                report = run_v3_command(_operation, manifest, **options)
            except (ValueError, OSError, RuntimeError) as error:
                raise click.ClickException(str(error)) from error
            click.echo(json.dumps(report, ensure_ascii=False, default=str))

        command = click.option(
            "--manifest",
            type=click.Path(exists=True, dir_okay=False),
            required=True,
        )(command)
        if operation in ("resume", "finalize"):
            command = click.option("--apply", is_flag=True)(command)
        if operation == "finalize":
            command = click.option(
                "--output-dir",
                type=click.Path(file_okay=False),
                required=True,
            )(command)
        group.command(operation)(command)
