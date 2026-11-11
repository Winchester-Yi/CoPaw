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
        roots = options.get("nas_roots")
        if not isinstance(roots, dict) or not roots:
            raise ValueError(
                "offline.nas_roots requires explicit per-service NAS roots",
            )
        self.nas_roots = {}
        for service, value in roots.items():
            root = Path(value)
            if (
                not root.is_absolute()
                or root.is_symlink()
                or not root.is_dir()
                or str(root.resolve()) != str(root)
            ):
                raise ValueError(
                    "offline.nas_roots must contain existing original NAS roots",
                )
            self.nas_roots[service] = root
        binding = {
            "schema": self.staging_schema,
            "nas_roots": {
                key: str(root) for key, root in self.nas_roots.items()
            },
            "migration_id": self.manifest.migration_id,
        }
        self.manifest._validate_target(binding)
        if (
            self.manifest.data.get("target") is not None
            and self.manifest.data["target"] != binding
        ):
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
            if any(
                root == source_root
                or root.is_relative_to(source_root)
                or source_root.is_relative_to(root)
                for root in self.nas_roots.values()
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
                        service_unit_id=item["service_unit_id"],
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
            "version": 2,
            "migration_id": self.manifest.migration_id,
            "operation": operation,
            "preview": True,
            "staging_schema": self.staging_schema,
            "application_schema": self.application_schema,
            "nas_roots": {
                key: str(root) for key, root in self.nas_roots.items()
            },
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
        entries = options.get("services")
        if entries is None and len(self.nas_roots) == 1:
            entries = {next(iter(self.nas_roots)): options}
        if not isinstance(entries, dict) or set(entries) != set(
            self.nas_roots,
        ):
            raise ValueError(
                "export.services must explicitly cover every migration service",
            )
        prepared = []
        destinations = []
        for service, entry in entries.items():
            destination = Path(entry.get("destination", ""))
            if (
                not destination.is_absolute()
                or destination.is_symlink()
                or (
                    destination.exists()
                    and (
                        not destination.is_dir() or any(destination.iterdir())
                    )
                )
            ):
                raise ValueError(
                    "export destination must be an explicit empty directory",
                )
            destination = destination.resolve()
            if any(
                destination == other
                or destination.is_relative_to(other)
                or other.is_relative_to(destination)
                for other in destinations
            ):
                raise ValueError(
                    "export service destinations must be isolated",
                )
            if any(
                destination == root
                or destination.is_relative_to(root)
                or root.is_relative_to(destination)
                for root in self.nas_roots.values()
            ):
                raise ValueError(
                    "export destination overlaps a service NAS root",
                )
            destinations.append(destination)
            prepared.append((service, destination, entry))
        target = OldFormatTarget(
            self.manifest.data["source_boundary"]["old_image_digest"],
            options.get("session_version"),
            options.get("checkpoint_version"),
        )
        # Load and validate all service ownership before creating any output.
        exporters = [
            (
                service,
                destination,
                ConversationExporter(
                    entry["backup_path"],
                    target,
                    service_unit_id=service,
                ),
            )
            for service, destination, entry in prepared
        ]
        reports = {
            service: exporter.export(destination)
            for service, destination, exporter in exporters
        }
        if len(reports) == 1:
            return next(iter(reports.values()))
        return {
            "version": 2,
            "services": reports,
            "passed": all(report["passed"] for report in reports.values()),
        }

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
            bundles = {
                key: BundleStore(root) for key, root in self.nas_roots.items()
            }
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


def _command(operation, path, apply=False, workers=4, io_workers=4):
    try:
        from swe.app.conversation_storage.chat_mutations import (
            load_metadata_json,
        )

        from swe.app.conversation_storage.migration.manifest import (
            migration_workflow,
        )

        document = load_metadata_json(path.read_bytes())
        if migration_workflow(document) == "direct":
            from swe.cli.conversation_storage_v3_cmd import run_v3_command

            result = run_v3_command(
                operation,
                path,
                apply=apply,
                workers=workers,
                io_workers=io_workers,
            )
            click.echo(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )
            if result.get("passed") is False or result.get("blockers"):
                raise click.ClickException(
                    "conversation storage verification has blockers",
                )
            return
        job = OfflineJob(path)
        if operation == "import" and (workers, io_workers) != (4, 4):
            raise ValueError(
                "parallel worker options are supported only for "
                "v3 direct migrations",
            )
        report = job.inventory()
        if apply or operation == "verify":
            from swe.app.conversation_storage.migration.importer import (
                validate_inventory,
            )

            validate_inventory(report)
        if apply and operation in {"import", "install"}:
            raise ValueError(
                "legacy v2 import/install is disabled; use the v3 migration CLI",
            )
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


from swe.cli.conversation_storage_discovery_cmd import (
    register_discovery_commands,
)
from swe.cli.conversation_storage_v3_cmd import register_v3_commands

register_discovery_commands(conversation_storage_group)
register_v3_commands(conversation_storage_group)


def _register(operation, mutable=False):
    @click.command(operation)
    @click.option(
        "--manifest",
        required=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )
    def command(manifest, **options):
        _command(
            operation,
            manifest,
            options.get("apply", False),
            options.get("workers", 4),
            options.get("io_workers", 4),
        )

    if mutable:
        command = click.option(
            "--apply",
            is_flag=True,
            help="Apply the reviewed offline operation.",
        )(command)
    if operation == "import":
        command = click.option(
            "--io-workers",
            type=click.IntRange(1, 32),
            default=4,
            show_default=True,
        )(command)
        command = click.option(
            "--workers",
            type=click.IntRange(1, 32),
            default=4,
            show_default=True,
        )(command)
    conversation_storage_group.add_command(command)


for _operation in ("inventory", "plan", "verify", "inspect"):
    _register(_operation)
for _operation in ("import", "export", "install"):
    _register(_operation, mutable=True)


@conversation_storage_group.command("recover")
@click.option(
    "--evidence",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--apply", is_flag=True)
@click.option(
    "--confirm-ended-owner",
    help="Exact owner incarnation whose process has exited and IO has drained.",
)
@click.option("--receipt", type=click.Path(dir_okay=False, path_type=Path))
def recover(evidence, apply, confirm_ended_owner, receipt):
    """Settle a reviewed stopped incarnation; never resume model or tool work."""
    from swe.app.conversation_storage.recovery_cli import RecoveryJob

    try:
        job = RecoveryJob(evidence)
        result = (
            asyncio.run(job.apply(confirm_ended_owner, receipt))
            if apply
            else job.preview()
        )
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        raise click.ClickException(str(error)) from None
