# -*- coding: utf-8 -*-
"""Offline conversation migration; no workspace or Agent is initialized."""

import asyncio
import json
from pathlib import Path
import re
from uuid import UUID

import click


class OfflineJob:
    """Validate the frozen mapping before connecting or changing anything."""

    def __init__(self, path):
        from swe.app.conversation_storage.migration.manifest import (
            MigrationManifest,
        )
        from swe.app.conversation_storage.migration.install_records import (
            OfflineTarget,
        )

        self.manifest = MigrationManifest.load(path)
        options = self.manifest.data.get("offline", {})
        self.application_schema = options.get("application_schema", "")
        if not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", self.application_schema):
            raise ValueError("offline.application_schema is required")
        self.staging_schema = (
            "conversation_staging_" + UUID(self.manifest.migration_id).hex
        )
        if (
            options.get("staging_schema", self.staging_schema)
            != self.staging_schema
        ):
            raise ValueError(
                "target must use the job's generated staging schema",
            )
        self.target = OfflineTarget(
            self.staging_schema,
            self.application_schema,
            self.manifest.migration_id,
        )
        root = Path(options.get("nas_root", ""))
        if (
            not root.is_absolute()
            or root.is_symlink()
            or not root.is_dir()
            or str(root.resolve()) != str(root)
        ):
            raise ValueError(
                "offline.nas_root must be an existing original NAS root",
            )
        self.nas_root = root
        binding = self.manifest.data.get("target")
        if binding is not None and binding != {
            "schema": self.staging_schema,
            "nas_root": str(root),
            "migration_id": self.manifest.migration_id,
        }:
            raise ValueError(
                "manifest target differs from generated staging/NAS binding",
            )
        self.options = options

    def inventory(self):
        from swe.app.conversation_storage.identity import WorkspaceScope
        from swe.app.conversation_storage.migration.inventory import (
            WorkspaceMapping,
            inventory,
        )
        from swe.app.conversation_storage.migration.manifest import (
            SourceBoundary,
        )

        mappings = []
        for item in self.manifest.data["workspaces"]:
            source_root = Path(item["root"]).resolve(strict=True)
            if self.nas_root == source_root or self.nas_root.is_relative_to(
                source_root,
            ):
                raise ValueError(
                    "target NAS cannot overlap the frozen source snapshot",
                )
            paths = {
                (
                    entry["user_id"],
                    entry["channel"],
                    entry["session_id"],
                ): entry["path"]
                for entry in item.get("session_paths", [])
            }
            mappings.append(
                WorkspaceMapping(
                    WorkspaceScope(
                        item["tenant_id"],
                        item["source_id"],
                        item["agent_id"],
                    ),
                    Path(item["root"]),
                    registry_path=item.get("registry_path", "chats.json"),
                    sessions_dir=item.get("sessions_dir", "sessions"),
                    dialog_dir=item.get("dialog_dir", "dialog"),
                    session_paths=paths,
                ),
            )
        report = inventory(
            mappings,
            SourceBoundary(**self.manifest.data["source_boundary"]),
            shares=self.options.get("shares", []),
        )
        return report

    def preview(self, operation, inventory):
        from swe.app.conversation_storage.migration.install import (
            ConversationInstaller,
            TABLES,
        )

        installer = ConversationInstaller(None, self.manifest, self.target)
        report = {
            "version": 1,
            "migration_id": self.manifest.migration_id,
            "operation": operation,
            "preview": True,
            "staging_schema": self.staging_schema,
            "application_schema": self.application_schema,
            "nas_root": str(self.nas_root),
            "tables": list(TABLES),
            "rename_sql": installer.rename_sql(),
            "source_boundary": self.manifest.data["source_boundary"],
            "source_scopes": [
                {
                    "scope": item["scope"],
                    "root": item["root"],
                    "sessions": len(item["sessions"]),
                    "chats": len(item["chats"]),
                }
                for item in inventory["workspaces"]
            ],
            "report_refs": {"install": str(installer.receipt_path)},
            "files": len(inventory["files"]),
            "workspaces": len(inventory["workspaces"]),
            "blockers": inventory["blockers"],
            "next": "explicit --apply required for mutations; verify always reruns full semantic checks",
        }
        if operation == "inspect" and installer.receipt_path.exists():
            from swe.app.conversation_storage.chat_mutations import (
                load_metadata_json,
            )

            report["install_receipt"] = load_metadata_json(
                installer.receipt_path.read_bytes(),
            )
        return report

    def export(self):
        from swe.app.conversation_storage.migration.exporter import (
            ConversationExporter,
            OldFormatTarget,
        )

        options = self.options.get("export", {})
        destination = Path(options.get("destination", ""))
        if (
            not destination.is_absolute()
            or destination.is_symlink()
            or (
                destination.exists()
                and (not destination.is_dir() or any(destination.iterdir()))
            )
        ):
            raise ValueError(
                "export destination must be an explicit empty directory",
            )
        target = OldFormatTarget(
            self.manifest.data["source_boundary"]["old_image_digest"],
            options.get("session_version"),
            options.get("checkpoint_version"),
        )
        return ConversationExporter(options["backup_path"], target).export(
            destination,
        )

    async def execute(self, operation, inventory):
        from swe.app.conversation_storage.bundle import BundleStore
        from swe.app.conversation_storage.migration.importer import (
            ConversationImporter,
        )
        from swe.app.conversation_storage.migration.install import (
            ConversationInstaller,
        )
        from swe.app.conversation_storage.migration.verify import verify_import
        from swe.database.config import get_database_config
        from swe.database.connection import DatabaseConnection

        config = get_database_config()
        if config.database != self.application_schema:
            raise ValueError(
                "configured application schema differs from manifest; do not repoint SWE_DB_NAME to a snapshot",
            )
        control = DatabaseConnection(config)
        staging = None
        try:
            await control.connect()
            installer = ConversationInstaller(
                control,
                self.manifest,
                self.target,
            )
            if operation == "import":
                await installer.prepare_staging()
            staging = DatabaseConnection(
                config.model_copy(
                    update={
                        "database": self.staging_schema,
                        "time_zone": "+00:00",
                    },
                ),
            )
            await staging.connect()
            bundles = BundleStore(self.nas_root)
            if operation == "import":
                return await ConversationImporter(
                    staging,
                    bundles,
                    self.manifest,
                    self.target,
                ).run(inventory)
            if operation == "verify":
                # verify_import marks batches verified by default; the CLI's
                # verification is deliberately read-only, including its journal.
                readonly = _readonly_manifest(self.manifest)
                return await verify_import(
                    staging,
                    bundles,
                    readonly,
                    inventory,
                    self.target,
                )
            return await installer.install(staging, bundles, inventory)
        finally:
            if staging is not None:
                await staging.close()
            await control.close()


def _readonly_manifest(manifest):
    from swe.app.conversation_storage.migration.manifest import (
        MigrationManifest,
    )

    class ReadOnlyManifest(MigrationManifest):
        def set_batch_state(self, _source_position, _state):
            pass

    return ReadOnlyManifest(manifest.path, manifest.data)


def _command(operation, path, apply=False):
    try:
        job = OfflineJob(path)
        report = job.inventory()
        if apply or operation == "verify":
            from swe.app.conversation_storage.migration.importer import (
                validate_inventory,
            )

            validate_inventory(report)
        if operation == "export" and apply:
            result = job.export()
        elif operation in {"verify"} or apply:
            result = asyncio.run(job.execute(operation, report))
        else:
            result = job.preview(operation, report)
        click.echo(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
        if result.get("blockers") or result.get("passed") is False:
            raise click.ClickException(
                "conversation storage verification has blockers",
            )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        raise click.ClickException(str(error)) from None


@click.group("conversation-storage")
def conversation_storage_group():
    """Offline maintenance. Freeze all writers; mutations require --apply."""


def _register(operation, mutable=False):
    @click.command(operation)
    @click.option(
        "--manifest",
        required=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )
    def command(manifest, **options):
        _command(operation, manifest, options.get("apply", False))

    if mutable:
        command = click.option(
            "--apply",
            is_flag=True,
            help="Apply the reviewed offline operation.",
        )(command)
    conversation_storage_group.add_command(command)


for _operation in ("inventory", "plan", "verify", "inspect"):
    _register(_operation)
for _operation in ("import", "export", "install"):
    _register(_operation, mutable=True)
