# -*- coding: utf-8 -*-
"""Pure discovery and freeze commands for DBA-prepared migration."""

import asyncio
import json
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


def register_discovery_commands(group):
    """Attach discover/freeze without initializing business services."""

    @group.command("discover")
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
    def discover_command(services, output, shares_file, overrides):
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
            click.echo(
                json.dumps(
                    {
                        "report": str(destination),
                        "ready": report["ready"],
                        "blockers": len(report["blockers"]),
                    },
                ),
            )
            if not report["ready"]:
                raise click.ClickException(
                    "discovery contains blockers; inspect the report",
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise click.ClickException(str(error)) from error

    @group.command("freeze")
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
    def freeze_command(discovery, boundary, application_schema, manifest):
        try:
            report = _read(discovery)
            _output(manifest, report["services"])
            frozen = freeze_discovery(
                report,
                manifest,
                SourceBoundary(**_read(boundary)),
                application_schema,
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
