# -*- coding: utf-8 -*-
"""Offline migration for MySQL Chat metadata."""

import asyncio
import json
from pathlib import Path

import click


async def _execute_metadata(
    operation,
    path,
    *,
    apply=False,
    destination=None,
    full_audit=False,
    resume_from=None,
    workers=4,
):
    from swe.app.conversation_storage.migration import metadata
    from swe.database.config import get_database_config
    from swe.database.connection import DatabaseConnection

    config = get_database_config()
    manifest, digest = (
        metadata.read_manifest(path, config.database)
        if path is not None
        else (None, None)
    )
    if operation == "migrate" and not apply:
        return await metadata.migrate(
            None,
            manifest,
            digest,
            apply=False,
            full_audit=full_audit,
            workers=workers,
        )
    original = (
        metadata.read_manifest(resume_from, config.database)
        if resume_from is not None
        else None
    )
    config = config.model_copy(update={"time_zone": "+00:00"})
    control = DatabaseConnection(config)
    database = DatabaseConnection(config)
    try:
        await control.connect()
        await database.connect()
        # A failed source transaction can discard its own connection without
        # releasing the migration lock or preventing the next source from running.
        async with control.named_lock(
            "conversation-metadata-migration:" + config.database,
            timeout=0,
        ):
            if operation == "export":
                workspaces = await metadata.export(
                    database,
                    manifest,
                    digest,
                    destination,
                )
                return {"status": "exported", "workspaces": workspaces}
            previous = (
                metadata.WorkspaceProgress(resume_from, original[1], config)
                if original is not None
                else None
            )
            progress = metadata.WorkspaceProgress(
                path,
                digest,
                config,
                previous=previous,
            )
            return await metadata.migrate(
                database,
                manifest,
                digest,
                apply=True,
                full_audit=full_audit,
                resume_from=original,
                progress=progress,
                workers=min(workers, config.max_connections),
            )
    finally:
        try:
            await database.close()
        finally:
            await control.close()


def _echo(result):
    click.echo(
        json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
    )


def register_workflow_commands(group):
    @group.command("prepare")
    @click.option(
        "--mode",
        type=click.Choice(["mysql-metadata"]),
        default="mysql-metadata",
        show_default=True,
    )
    @click.option(
        "--workspaces",
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option(
        "--workspace-root",
        type=click.Path(exists=True, file_okay=False),
        help="NAS working directory containing encoded tenant scope directories.",
    )
    @click.option("--manifest", required=True, type=click.Path(dir_okay=False))
    @click.option(
        "--workers",
        type=click.IntRange(min=1),
        default=4,
        show_default=True,
        help="Maximum number of NAS scan workers with --workspace-root.",
    )
    def prepare_command(mode, workspaces, workspace_root, manifest, workers):
        from swe.app.conversation_storage.chat_mutations import (
            load_metadata_json,
        )
        from swe.app.conversation_storage.migration import metadata
        from swe.database.config import get_database_config

        if (workspaces is None) == (workspace_root is None):
            raise click.UsageError(
                "provide exactly one of --workspace-root and --workspaces",
            )
        try:
            schema = get_database_config().database
            if workspace_root is not None:
                result = metadata.prepare_scan(
                    workspace_root,
                    schema,
                    manifest,
                    workers=workers,
                )
                failures = len(result["scan_errors"]) + sum(
                    "scan_error" in item for item in result["workspaces"]
                )
                _echo(
                    {
                        "status": (
                            "partial_success" if failures else "prepared"
                        ),
                        "migration_id": result["migration_id"],
                        "manifest": manifest,
                        "workspaces": len(result["workspaces"]),
                        "failed": failures,
                    },
                )
                if failures:
                    raise click.exceptions.Exit(2)
                return
            items = load_metadata_json(Path(workspaces).read_bytes())
            if isinstance(items, dict):
                items = items.get("workspaces")
            if not isinstance(items, list):
                raise ValueError("workspaces must be a JSON list")
            result = metadata.prepare(
                items,
                schema,
                manifest,
            )
            _echo(
                {"status": "prepared", "migration_id": result["migration_id"]},
            )
        except click.exceptions.Exit:
            raise
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            raise click.ClickException(str(error)) from error

    @group.command("migrate")
    @click.option(
        "--manifest",
        required=True,
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option("--apply", is_flag=True)
    @click.option("--full-audit", is_flag=True)
    @click.option(
        "--workers",
        type=click.IntRange(min=1),
        default=4,
        show_default=True,
        help="Maximum number of source groups processed concurrently.",
    )
    @click.option(
        "--resume-from",
        type=click.Path(exists=True, dir_okay=False),
        help="Previous manifest for the unpublished sources being repaired.",
    )
    def migrate_command(manifest, apply, full_audit, workers, resume_from):
        if resume_from is not None and not apply:
            raise click.UsageError("--resume-from requires --apply")
        try:
            result = asyncio.run(
                _execute_metadata(
                    "migrate",
                    manifest,
                    apply=apply,
                    full_audit=full_audit,
                    resume_from=resume_from,
                    workers=workers,
                ),
            )
            _echo(result)
            if result["status"] == "partial_success":
                raise click.exceptions.Exit(2)
        except click.exceptions.Exit:
            raise
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            raise click.ClickException(str(error)) from error

    @group.command("export-metadata")
    @click.option(
        "--manifest",
        type=click.Path(exists=True, dir_okay=False),
        help="Limit export to a published migration. Omit after full cutover.",
    )
    @click.option(
        "--destination",
        required=True,
        type=click.Path(file_okay=False),
    )
    def export_metadata_command(manifest, destination):
        """Export the SQL Chat directory in the legacy file format."""
        try:
            result = asyncio.run(
                _execute_metadata("export", manifest, destination=destination),
            )
            _echo(result)
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            raise click.ClickException(str(error)) from error
