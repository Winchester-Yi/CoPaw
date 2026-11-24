#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebind a drained evidence outbox after all writers and workers stop."""

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.conversation_storage.identity import (
    WorkspaceScope,
    service_unit_from_env,
)
from swe.app.conversation_storage.metadata_repository import (
    MySQLMetadataRepository,
    SOURCES,
    SOURCE_PREDICATE,
)
from swe.app.runner.manager import ChatManager
from swe.app.runner.repo.json_repo import JsonChatRepository
from swe.app.runner.session import SafeJSONSession
from swe.config.context import resolve_storage_tenant_id
from swe.database import DatabaseConnection, get_database_config
from swe.observability.db_config import get_observability_database_config
from swe.observability.outbox_recovery import (
    OutboxRecoveryStore,
    rebind_offline,
    scope_digest,
)
from swe.observability.outbox_runtime import recovery_worker_for_scope
from swe.observability.outbox_scanner import WorkspaceOutboxScanner


async def old_scanner(scope, root, authority, metadata_db):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("old workspace is unavailable")
    if authority == "mysql-metadata":
        source = await metadata_db.fetch_one(
            f"SELECT published FROM {SOURCES} WHERE " + SOURCE_PREDICATE,
            scope.sql_values[:3],
        )
        if not source or not source["published"]:
            raise ValueError("old metadata source is not published")
        repo = MySQLMetadataRepository(metadata_db, scope)
    else:
        repo = JsonChatRepository(root / "chats.json")
    workspace = SimpleNamespace(
        agent_id=scope.agent_id,
        tenant_id=resolve_storage_tenant_id(scope.tenant_id, scope.source_id),
        source_id=scope.source_id,
        conversation_scope=scope,
        workspace_dir=root,
        chat_manager=ChatManager(
            repo=repo,
            archive_store=ConversationArchiveStore(root / "dialog"),
        ),
        runner=SimpleNamespace(
            session=SafeJSONSession(save_dir=str(root / "sessions")),
        ),
    )
    return WorkspaceOutboxScanner(workspace, scope)


async def execute(args):
    scope = WorkspaceScope(
        args.tenant,
        args.source,
        args.agent,
        service_unit_id=service_unit_from_env(),
    )
    config = get_observability_database_config()
    if config is None:
        raise ValueError("observability database is not configured")
    obs = DatabaseConnection(config)
    metadata = (
        DatabaseConnection(get_database_config())
        if "mysql-metadata" in (args.old_authority, args.new_authority)
        else None
    )
    try:
        await obs.connect()
        if metadata is not None:
            await metadata.connect()
        old_root = str(Path(args.old_root).expanduser().resolve(strict=True))
        new_root = str(Path(args.new_root).expanduser().resolve(strict=True))
        old = await old_scanner(scope, old_root, args.old_authority, metadata)
        new = await recovery_worker_for_scope(
            scope,
            metadata_db=metadata,
            observability_db=obs,
            emitter=None,
            expected_root=new_root,
            expected_authority=args.new_authority,
        )
        await rebind_offline(
            OutboxRecoveryStore(obs, scope_digest(scope)),
            scope,
            old,
            new.scanner,
            old_root=old_root,
            old_authority=args.old_authority,
            new_root=new_root,
            new_authority=args.new_authority,
            writers_stopped=args.all_writers_and_workers_stopped,
        )
        print(
            "Outbox binding updated after complete verification and inbox acknowledgement.",
        )
    finally:
        if metadata is not None:
            await metadata.close()
        await obs.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tenant", "source", "agent", "old-root", "new-root"):
        parser.add_argument("--" + name, required=True)
    for name in ("old-authority", "new-authority"):
        parser.add_argument(
            "--" + name,
            choices=("legacy", "mysql-metadata"),
            required=True,
        )
    parser.add_argument(
        "--all-writers-and-workers-stopped",
        action="store_true",
        required=True,
    )
    asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    main()
