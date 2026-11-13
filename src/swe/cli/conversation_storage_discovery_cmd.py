# -*- coding: utf-8 -*-
"""Pure discovery and freeze commands for DBA-prepared migration."""

import asyncio
import json
import os
import tempfile
import shutil
from pathlib import Path

import click

from swe.app.conversation_storage.chat_mutations import load_metadata_json
from swe.app.conversation_storage.migration.discovery import (
    SHARE_FIELDS,
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary


def _read(path):
    return load_metadata_json(Path(path).read_bytes())


def _list(value, field):
    result = value.get(field) if isinstance(value, dict) else value
    if not isinstance(result, list):
        raise ValueError(field + " must be a JSON list")
    return result


def _output(path, services):
    output = Path(path).absolute().resolve()
    for service in services:
        for key in ("source_root", "config_root", "snapshot_root"):
            if service.get(key) and output.is_relative_to(
                Path(service[key]).resolve(),
            ):
                raise ValueError(
                    "output cannot be inside frozen source inputs",
                )
    return output


async def read_original_shares():
    """Read all legacy share fields in a consistent, read-only snapshot."""
    from swe.database import DatabaseConnection, get_database_config

    database = DatabaseConnection(get_database_config())
    try:
        await database.connect()
        async with database.transaction() as transaction:
            # Read one connection/transaction; schema initialization is never called.
            result = []
            offset = 0
            fields = ", ".join("t." + name for name in SHARE_FIELDS)
            while True:
                rows = await transaction.fetch_all(
                    "SELECT "
                    + fields
                    + " FROM swe_chat_shares t ORDER BY token LIMIT %s OFFSET %s",
                    (1000, offset),
                )
                result.extend(dict(row) for row in rows)
                if len(rows) < 1000:
                    return result
                offset += len(rows)
    finally:
        await database.close()


def _resource_options(command):
    for name, limit, default in (
        ("workers", click.IntRange(1, 32), 4),
        ("io-workers", click.IntRange(1, 32), 4),
        ("memory-budget-mib", click.IntRange(128, 65536), 512),
    ):
        command = click.option(
            "--" + name,
            type=limit,
            default=default,
            show_default=True,
        )(command)
    return click.option(
        "--work-dir",
        type=click.Path(exists=True, file_okay=False, path_type=Path),
    )(command)


def _semantic_validator(*, workers, io_workers, memory_budget_mib, work_dir):
    def validate(data):
        return asyncio.run(
            _semantic_spooled(
                data,
                workers,
                io_workers,
                memory_budget_mib,
                work_dir,
            ),
        )

    return validate


async def _semantic_spooled(
    data,
    workers,
    io_workers,
    memory_budget_mib,
    work_dir,
):
    from swe.cli.conversation_storage_v3_cmd import validate_output
    from swe.app.conversation_storage.migration.inventory_parallel import (
        spool_workspaces,
    )
    from swe.app.conversation_storage.migration.inventory_v3 import (
        _preflight_share_budget,
        _inventory_shares,
    )
    from swe.app.conversation_storage.migration.discovery import fingerprints
    from swe.app.conversation_storage.migration.spool import InventorySpool
    from swe.app.conversation_storage.migration.spool_report import (
        spool_report,
    )

    candidates = (
        (Path(work_dir),)
        if work_dir is not None
        else (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    )
    base = None
    for candidate in candidates:
        try:
            candidate = validate_output(candidate, data)
        except ValueError:
            continue
        if candidate.is_dir():
            base = candidate
            break
    if base is None:
        raise ValueError("no work directory is separate from source roots")
    memory_bytes = memory_budget_mib * 1024 * 1024
    baseline = data.get("fingerprints")
    if baseline and fingerprints(data["services"]) != baseline:
        raise ValueError("source changed during discovery")
    _preflight_share_budget(data, memory_bytes)
    protected = [
        item[key]
        for item in data["services"]
        for key in ("source_root", "config_root", "snapshot_root", "nas_root")
        if item.get(key)
    ]
    with InventorySpool(
        base,
        max_bytes=shutil.disk_usage(base).free * 3 // 4,
        record_limit=memory_bytes // workers,
        protected=protected,
    ) as spool:
        summaries = await spool_workspaces(
            data,
            spool,
            workers=workers,
            io_workers=io_workers,
            memory_bytes=memory_bytes,
        )
        semantic = spool_report(
            spool,
            summaries,
            shares=[],
            source_boundary=None,
        )
        _inventory_shares(data, semantic)
        if baseline and fingerprints(data["services"]) != baseline:
            raise ValueError("source changed during discovery")
        return {
            "blockers": semantic["blockers"],
            "workspaces": [
                {"sessions": range(item.sessions), "chats": range(item.chats)}
                for item in summaries
            ],
        }


def _write_failed_tenants(destination, report):
    """Write account-only diagnostics; raw paths and parser errors stay private."""
    path = destination.with_suffix(".failed-tenants.json")
    reasons = {
        "workspace_identity_invalid": "账号工作区配置不完整",
        "session_invalid": "会话数据不完整",
        "registry_invalid": "会话目录数据不完整",
        "share_invalid": "账号分享数据或归属异常",
        "source_modified_after_sync_started": "冻结会话输入发生变化",
        "tenant_input_invalid": "账号会话输入不符合迁移限制",
    }
    failures = []
    for tenant in report["tenants"]:
        if tenant["state"] != "failed":
            continue
        for code in sorted({item["code"] for item in tenant["blockers"]}):
            failures.append(
                {
                    "service_unit_id": tenant["service_unit_id"],
                    "tenant_id": tenant["tenant_id"],
                    "stage": "discover",
                    "code": code,
                    "reason": reasons.get(code, "账号迁移校验未通过"),
                },
            )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(failures, handle, ensure_ascii=False, sort_keys=True)
    return str(path)


def register_discovery_commands(group):
    """Attach discover/freeze without initializing business services."""

    @group.command("discover")
    @_resource_options
    @click.option(
        "--services",
        required=True,
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option("--output", required=True, type=click.Path(dir_okay=False))
    @click.option(
        "--shares-file",
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option("--overrides", type=click.Path(exists=True, dir_okay=False))
    @click.option(
        "--tenant-isolation",
        is_flag=True,
        help="Isolate tenant data failures; requires independent frozen inputs.",
    )
    def discover_command(
        services,
        output,
        shares_file,
        overrides,
        tenant_isolation,
        **resources,
    ):
        try:
            declared = _list(_read(services), "services")
            destination = _output(output, declared)
            shares = (
                _list(_read(shares_file), "shares")
                if shares_file
                else asyncio.run(read_original_shares())
            )
            exceptions = (
                _list(_read(overrides), "overrides") if overrides else []
            )
            report = discover_services(
                declared,
                shares=shares,
                overrides=exceptions,
                semantic_validator=_semantic_validator(**resources),
                tenant_isolation=tenant_isolation,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Existing evidence is never silently overwritten.
            with destination.open("x", encoding="utf-8") as handle:
                json.dump(
                    report,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            failures_path = (
                _write_failed_tenants(destination, report)
                if tenant_isolation
                else None
            )
            click.echo(
                json.dumps(
                    {
                        "report": str(destination),
                        "ready": report["ready"],
                        "blockers": len(report["blockers"]),
                        **(
                            {
                                "status": report["status"],
                                "failed_tenants_report": failures_path,
                                "successful_tenants": sum(
                                    t["state"] == "discovered"
                                    for t in report["tenants"]
                                ),
                                "failed_tenants": sum(
                                    t["state"] == "failed"
                                    for t in report["tenants"]
                                ),
                            }
                            if tenant_isolation
                            else {}
                        ),
                    },
                ),
            )
            if not report["ready"] and tenant_isolation:
                raise click.exceptions.Exit(2)
            if not report["ready"]:
                raise click.ClickException(
                    "discovery contains blockers; inspect the report",
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise click.ClickException(str(error)) from error

    @group.command("freeze")
    @_resource_options
    @click.option(
        "--discovery",
        required=True,
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option(
        "--boundary",
        required=True,
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option("--application-schema", required=True)
    @click.option("--manifest", required=True, type=click.Path(dir_okay=False))
    def freeze_command(
        discovery,
        boundary,
        application_schema,
        manifest,
        **resources,
    ):
        try:
            report = _read(discovery)
            _output(manifest, report["services"])
            frozen = freeze_discovery(
                report,
                manifest,
                SourceBoundary(**_read(boundary)),
                application_schema,
                semantic_validator=_semantic_validator(**resources),
            )
            click.echo(
                json.dumps(
                    {
                        "manifest": str(frozen.path),
                        "migration_id": frozen.migration_id,
                    },
                ),
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise click.ClickException(str(error)) from error
