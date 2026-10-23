# -*- coding: utf-8 -*-
"""Offline maintenance for versioned memory storage."""

import asyncio
import json
import os
from pathlib import Path
import tempfile

import click

_OPERATIONS = click.Choice(["preview", "migrate", "validate", "export"])


def _publish_json(destination, state):
    """Publish a streaming export without replacing any existing destination."""
    from swe.app.runner.turn_ledger import atomic_json, sync_directory

    destination = Path(destination)
    if destination.exists():
        raise ValueError("Export destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.export-",
        dir=destination.parent,
    )
    os.close(descriptor)
    staging = Path(temporary)
    try:
        atomic_json(staging, state)
        os.link(staging, destination)
        sync_directory(destination.parent)
    finally:
        staging.unlink(missing_ok=True)


def _run(operation):
    try:
        report = operation()
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))


@click.group("memory-storage")
def memory_storage_group():
    """Maintain local NAS objects. Stop all writers before migration/export.

    New formats are opt-in. Migrations preview by default; --apply publishes.
    Paths are trusted local maintenance paths, never HTTP request inputs.
    """


@memory_storage_group.command("session")
@click.option(
    "--sessions-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--session-id", required=True)
@click.option("--user-id", default="")
@click.argument("operation", type=_OPERATIONS)
@click.option("--apply", is_flag=True, help="Publish migration after preview.")
@click.option("--destination", type=click.Path(path_type=Path))
def session_cmd(
    sessions_dir,
    session_id,
    user_id,
    operation,
    apply,
    destination,
):
    """Preview, migrate, validate, or export one session."""
    from swe.app.runner.session import SafeJSONSession

    session = SafeJSONSession(str(sessions_dir))

    async def execute():
        path = Path(session._get_save_path(session_id, user_id))
        if not path.is_file():
            raise ValueError("Session root does not exist")
        # Do not let a legacy permissive read turn corrupt input into empty data.
        state = json.loads(path.read_bytes())
        if not isinstance(state, dict):
            raise ValueError("Session root must contain a JSON object")
        if operation in {"preview", "migrate"}:
            return await session.migrate_terminal_turns(
                session_id,
                user_id,
                dry_run=operation == "preview" or not apply,
            )
        if operation == "validate":
            return await session.migrate_terminal_turns(
                session_id,
                user_id,
                dry_run=True,
            )
        if destination is None:
            raise ValueError("--destination is required for export")
        state = await session.export_legacy_state(session_id, user_id)
        _publish_json(destination, state)
        return {
            "format": 2,
            "destination": str(destination),
            "total": len(state.get("turn_states", {})),
        }

    _run(lambda: asyncio.run(execute()))


@memory_storage_group.command("journal")
@click.option(
    "--dialog-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--chat-id", required=True)
@click.argument("operation", type=_OPERATIONS)
@click.option("--apply", is_flag=True, help="Publish migration after preview.")
@click.option("--destination", type=click.Path(path_type=Path))
def journal_cmd(dialog_root, chat_id, operation, apply, destination):
    """Preview, migrate, validate, or export one checkpoint journal."""
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    def execute():
        store = ConversationArchiveStore(
            dialog_root,
            cursor_secret=b"offline-memory-storage-maintenance",
        )
        if not store.path_for(chat_id).is_dir():
            raise ValueError("Conversation archive does not exist")
        if operation in {"preview", "migrate"}:
            return store.migrate_checkpoint_v2(
                chat_id,
                dry_run=operation == "preview" or not apply,
            )
        if operation == "validate":
            if not (store.path_for(chat_id) / "state-v2.json").exists():
                return store.migrate_checkpoint_v2(chat_id, dry_run=True)
            return store.validate_checkpoint_v2(chat_id)
        if destination is None:
            raise ValueError("--destination is required for export")
        return store.export_checkpoint_legacy(chat_id, destination)

    _run(execute)
