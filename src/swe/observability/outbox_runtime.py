# -*- coding: utf-8 -*-
"""Build scoped recovery readers without starting workspace services."""

from __future__ import annotations

from pathlib import Path
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
import stat
import threading
from types import SimpleNamespace

from ..agents.memory.conversation_archive import ConversationArchiveStore
from ..app.conversation_storage.identity import service_unit_from_env
from ..app.conversation_storage.runtime import (
    METADATA_AUTHORITY,
    storage_authority,
)
from ..app.runner.manager import ChatManager
from ..app.runner.repo.json_repo import JsonChatRepository
from ..app.runner.session import SafeJSONSession
from ..app.tenant_context import bind_tenant_context
from ..config.context import resolve_storage_tenant_id
from ..config.utils import (
    get_tenant_storage_config_path,
    load_config_bytes,
)
from .outbox_recovery import (
    OutboxRecoveryStore,
    OutboxRecoveryWorker,
    scope_digest,
)
from .outbox_scanner import WorkspaceOutboxScanner

_CONFIG_LIMIT = 1024 * 1024
_CONFIG_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="agentobs-config",
)
_CONFIG_SLOTS = threading.BoundedSemaphore(2)
_CONFIG_TIMEOUT_SECONDS = 5.0
_CONFIG_IN_FLIGHT: set[tuple[Path, str]] = set()
_CONFIG_IN_FLIGHT_LOCK = threading.Lock()


def _config_signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_config_snapshot(path: Path) -> bytes | None:
    """Read a stable regular file without following a replaced symlink."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raise ValueError("recovery config changed during read")
    if not stat.S_ISREG(before.st_mode) or before.st_size > _CONFIG_LIMIT:
        raise ValueError("recovery config unavailable")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _config_signature(
                before,
            ) != _config_signature(opened):
                raise ValueError("recovery config changed during read")
            content = handle.read(_CONFIG_LIMIT + 1)
            after = os.fstat(handle.fileno())
        current = path.lstat()
    except OSError as exc:
        raise ValueError("recovery config unavailable") from exc
    if (
        len(content) > _CONFIG_LIMIT
        or _config_signature(opened) != _config_signature(after)
        or _config_signature(after) != _config_signature(current)
    ):
        raise ValueError("recovery config changed during read")
    return content


def _load_recovery_root(path: Path, agent_id: str) -> Path:
    config = load_config_bytes(_read_config_snapshot(path))
    profile = config.agents.profiles.get(agent_id)
    if profile is None:
        raise ValueError("recovery agent unavailable")
    root = Path(profile.workspace_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("recovery workspace unavailable")
    return root


async def _resolve_recovery_root(path: Path, agent_id: str) -> Path:
    key = (path, agent_id)
    with _CONFIG_IN_FLIGHT_LOCK:
        # The slot remains held until the submitted thread actually exits.
        if (
            key in _CONFIG_IN_FLIGHT
            or not _CONFIG_SLOTS.acquire(  # pylint: disable=consider-using-with
                blocking=False,
            )
        ):
            raise RuntimeError("recovery config readers are busy")
        _CONFIG_IN_FLIGHT.add(key)

    def release(_work=None):
        with _CONFIG_IN_FLIGHT_LOCK:
            _CONFIG_IN_FLIGHT.remove(key)
            _CONFIG_SLOTS.release()

    try:
        context = copy_context()
        work = _CONFIG_EXECUTOR.submit(
            context.run,
            _load_recovery_root,
            path,
            agent_id,
        )
    except BaseException:
        release()
        raise
    work.add_done_callback(release)
    future = asyncio.wrap_future(work)
    try:
        return await asyncio.wait_for(
            asyncio.shield(future),
            timeout=_CONFIG_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        future.cancel()
        raise


async def start_recovery(app, manager, metadata_db):
    """Enable capture only when durable recovery can be scheduled."""
    from .outbox import _keys
    from .outbox_recovery import (
        OutboxRecoveryScheduler,
        clear_registered_scopes,
        enable_registered_scopes,
    )
    from .emitter import default_emitter
    from .config import capture_enabled
    from ..app.conversation_storage.runtime import resolved_scope

    clear_registered_scopes()
    app.state.agent_observability_recovery = None
    connection = getattr(app.state, "agent_observability_connection", None)
    if (
        connection is None
        or getattr(app.state, "agent_observability_store", None) is None
    ):
        return False
    _keys()
    service = service_unit_from_env()
    async with connection.acquire() as session:
        await session.fetch_all(
            "SELECT scope_key,workspace_root,storage_authority FROM swe_agentobs_outbox_scopes LIMIT 0",
        )
        await session.fetch_all(
            "SELECT scope_key,lease_owner,lease_until,scan_cursor FROM swe_agentobs_outbox_recovery LIMIT 0",
        )
        indexes = await session.fetch_all(
            "SELECT INDEX_NAME,SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
            "AND TABLE_NAME='swe_agentobs_inbox' AND INDEX_NAME='ix_agentobs_inbox_event_ack'",
            (connection.config.database,),
        )
        if tuple(
            row["COLUMN_NAME"]
            for row in sorted(indexes, key=lambda row: row["SEQ_IN_INDEX"])
        ) != ("event_id", "processing_state"):
            raise ValueError("recovery inbox index unavailable")
    registry = OutboxRecoveryStore(connection, bytes(32))

    async def register(workspace):
        scope = resolved_scope(workspace, service_unit_id=service)
        root = str(
            Path(workspace.workspace_dir).expanduser().resolve(strict=True),
        )
        await OutboxRecoveryStore(
            connection,
            scope_digest(scope),
        ).register_scope(scope, root, storage_authority())

    async def resolve(scope, root, authority):
        return await recovery_worker_for_scope(
            scope,
            metadata_db=metadata_db,
            observability_db=connection,
            emitter=default_emitter(),
            expected_root=root,
            expected_authority=authority,
        )

    scheduler = OutboxRecoveryScheduler(registry, service, resolve)
    await scheduler.start()
    app.state.agent_observability_recovery = scheduler
    if capture_enabled():
        enable_registered_scopes()
        manager.set_outbox_register(register)
    return True


async def stop_recovery(app, manager):
    from .outbox_recovery import clear_registered_scopes

    if manager is not None:
        manager.set_outbox_register(None)
    clear_registered_scopes()
    scheduler = getattr(app.state, "agent_observability_recovery", None)
    if scheduler is not None:
        await scheduler.stop()
        app.state.agent_observability_recovery = None


async def recovery_worker_for_scope(
    scope,
    *,
    metadata_db,
    observability_db,
    emitter,
    expected_root,
    expected_authority,
):
    """Rebuild only readers and verify their scope against current config."""
    if scope.service_unit_id != service_unit_from_env():
        raise ValueError("recovery service unit mismatch")
    with bind_tenant_context(
        tenant_id=scope.tenant_id,
        source_id=scope.source_id,
    ):
        storage_tenant = resolve_storage_tenant_id(
            scope.tenant_id,
            scope.source_id,
        )
        path = get_tenant_storage_config_path(storage_tenant)
        root = await _resolve_recovery_root(path, scope.agent_id)
        if (
            str(root) != expected_root
            or storage_authority() != expected_authority
        ):
            raise ValueError("recovery storage binding changed")
        if storage_authority() == METADATA_AUTHORITY:
            from ..app.conversation_storage.metadata_repository import (
                MySQLMetadataRepository,
                WORKSPACES,
                SCOPE_PREDICATE,
            )

            if metadata_db is None:
                raise ValueError("recovery metadata database unavailable")
            binding = await metadata_db.fetch_one(
                f"SELECT workspace_root FROM {WORKSPACES} WHERE "
                + SCOPE_PREDICATE,
                scope.sql_values,
            )
            if (
                not binding
                or bytes(binding["workspace_root"]) != str(root).encode()
            ):
                raise ValueError("recovery workspace binding mismatch")
            repo = MySQLMetadataRepository(metadata_db, scope)
        else:
            repo = JsonChatRepository(root / "chats.json")
        manager = ChatManager(
            repo=repo,
            archive_store=ConversationArchiveStore(root / "dialog"),
        )
        workspace = SimpleNamespace(
            agent_id=scope.agent_id,
            tenant_id=storage_tenant,
            source_id=scope.source_id,
            conversation_scope=scope,
            workspace_dir=root,
            chat_manager=manager,
            runner=SimpleNamespace(
                session=SafeJSONSession(save_dir=str(root / "sessions")),
            ),
        )
        scanner = WorkspaceOutboxScanner(workspace, scope)
        return OutboxRecoveryWorker(
            OutboxRecoveryStore(observability_db, scope_digest(scope)),
            scanner,
            emitter,
        )
