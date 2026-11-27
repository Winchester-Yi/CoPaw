# -*- coding: utf-8 -*-
"""Bounded, lease-coordinated recovery of committed evidence events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from .emitter import JsonlEmitter
from .identity import same_tenant_scope

logger = logging.getLogger(__name__)
_registered_scopes: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
_denied_scopes: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
_registration_states: dict[tuple, "_RegistrationState"] = {}
_gate_epoch = 0
_gate_enabled = False


@dataclass
class _RegistrationState:
    active: int = 0
    failure_revision: int = 0
    success_revision: int = -1
    failed: bool = False


def scope_registered(
    scope: dict[str, str],
    binding: tuple[str, str] | None,
) -> bool:
    keys = ("service_unit_id", "tenant_id", "source_id", "agent_id")
    if (
        not _gate_enabled
        or not binding
        or not all(isinstance(scope.get(k), str) and scope[k] for k in keys)
    ):
        return False
    key = (scope["service_unit_id"], scope["source_id"], scope["agent_id"])
    denied = _denied_scopes.get(key, set())
    if any(
        same_tenant_scope(tenant, scope["tenant_id"], scope["source_id"])
        for tenant, _root, _authority in denied
    ):
        return False
    tenants = _registered_scopes.get(key, set())
    return (
        sum(
            same_tenant_scope(tenant, scope["tenant_id"], scope["source_id"])
            and (root, authority) == binding
            for tenant, root, authority in tenants
        )
        == 1
    )


def clear_registered_scopes() -> None:
    global _gate_epoch, _gate_enabled
    _gate_epoch += 1
    _gate_enabled = False
    _registered_scopes.clear()
    _denied_scopes.clear()
    _registration_states.clear()


def enable_registered_scopes() -> None:
    global _gate_enabled
    _gate_enabled = True


def scope_digest(scope) -> bytes:
    """Hash the exact trusted identity, including service-unit boundaries."""
    return hashlib.sha256(
        json.dumps(
            scope.values,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode(
            "utf-8",
        ),
    ).digest()


@dataclass(frozen=True)
class OutboxPage:
    records: list[Any]
    next_cursor: str | None


class CommittedOutboxScanner(Protocol):
    async def scan_page(self, cursor: str | None, limit: int) -> OutboxPage:
        """Enumerate only committed roots and manifests in authorized scopes."""

    async def verified_event(self, record: Any) -> dict[str, Any] | None:
        """Revalidate committed occurrence, complete scope and signature."""


class OutboxRecoveryStore:
    def __init__(self, connection, scope_key: bytes):
        if len(scope_key) != 32:
            raise ValueError("scope key must be a 32-byte digest")
        self.connection = connection
        self.scope_key = scope_key

    async def register_scope(self, scope, root: str, authority: str) -> None:
        epoch = _gate_epoch
        if not _gate_enabled:
            raise RuntimeError("recovery scheduler is not enabled")
        key = (scope.service_unit_id, scope.source_id, scope.agent_id)
        instance = (scope.tenant_id, root, authority)
        state = _registration_states.setdefault(
            (*scope.values, root, authority),
            _RegistrationState(),
        )
        revision = state.failure_revision
        state.active += 1
        _denied_scopes.setdefault(key, set()).add(instance)
        tenants = _registered_scopes.get(key)
        if tenants is not None:
            tenants.discard(instance)
            if not tenants:
                del _registered_scopes[key]
        succeeded = False
        try:
            await self._register_scope_transaction(scope, root, authority)
            succeeded = True
        finally:
            if _gate_enabled and epoch == _gate_epoch:
                state.active -= 1
                if succeeded:
                    _registered_scopes.setdefault(key, set()).add(instance)
                    if state.failure_revision == revision:
                        state.success_revision = revision
                else:
                    state.failed = True
                    state.failure_revision += 1
                if (
                    not state.active
                    and state.success_revision == state.failure_revision
                ):
                    state.failed = False
                if not state.active and not state.failed:
                    denied = _denied_scopes.get(key)
                    if denied is not None:
                        denied.discard(instance)
                        if not denied:
                            del _denied_scopes[key]

    async def _register_scope_transaction(
        self,
        scope,
        root: str,
        authority: str,
    ) -> None:
        if scope_digest(scope) != self.scope_key:
            raise ValueError("recovery scope identity mismatch")
        if (
            not root
            or len(root.encode()) > 2048
            or authority not in ("legacy", "mysql-metadata")
        ):
            raise ValueError("recovery storage binding invalid")
        values = scope.sql_values
        async with self.connection.transaction() as session:
            await session.execute(
                "INSERT IGNORE INTO swe_agentobs_outbox_scopes "
                "(scope_key,service_unit_key,tenant_key,source_key,agent_key,workspace_root,storage_authority) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (self.scope_key, *values, root.encode(), authority),
            )
            row = await session.fetch_one(
                "SELECT service_unit_key,tenant_key,source_key,agent_key,workspace_root,storage_authority "
                "FROM swe_agentobs_outbox_scopes WHERE scope_key=%s FOR UPDATE",
                (self.scope_key,),
            )
            if (
                row is None
                or tuple(
                    bytes(row[name])
                    for name in (
                        "service_unit_key",
                        "tenant_key",
                        "source_key",
                        "agent_key",
                    )
                )
                != values
                or bytes(row["workspace_root"]) != root.encode()
                or row["storage_authority"] != authority
            ):
                raise ValueError("recovery scope registry mismatch")

    async def list_scopes(self, service_unit: bytes, after: bytes, limit: int):
        if not 1 <= limit <= 100 or len(after) not in (0, 32):
            raise ValueError("invalid scope page")
        async with self.connection.acquire() as session:
            return await session.fetch_all(
                "SELECT scope_key,service_unit_key,tenant_key,source_key,agent_key,workspace_root,storage_authority "
                "FROM swe_agentobs_outbox_scopes WHERE service_unit_key=%s "
                "AND scope_key>%s ORDER BY scope_key LIMIT %s",
                (service_unit, after, limit),
            )

    async def acknowledged(self, event_id: str) -> bool:
        async with self.connection.acquire() as session:
            row = await session.fetch_one(
                "SELECT 1 AS acknowledged FROM swe_agentobs_inbox "
                "WHERE event_id=%s AND processing_state=1 LIMIT 1",
                (event_id,),
            )
        return row is not None

    async def claim(
        self,
        owner: str,
        lease_seconds: int,
    ) -> str | None | Literal[False]:
        async with self.connection.transaction() as session:
            await session.execute(
                "INSERT IGNORE INTO swe_agentobs_outbox_recovery "
                "(scope_key, lease_owner, lease_until, scan_cursor) VALUES (%s, '', NULL, NULL)",
                (self.scope_key,),
            )
            changed = await session.execute(
                "UPDATE swe_agentobs_outbox_recovery SET lease_owner=%s, "
                "lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)) "
                "WHERE scope_key=%s AND (lease_until IS NULL OR lease_until < UTC_TIMESTAMP(6))",
                (owner, lease_seconds, self.scope_key),
            )
            if not changed:
                return False
            row = await session.fetch_one(
                "SELECT scan_cursor FROM swe_agentobs_outbox_recovery WHERE scope_key=%s",
                (self.scope_key,),
            )
            return row["scan_cursor"]

    async def renew(self, owner: str, lease_seconds: int) -> bool:
        async with self.connection.acquire() as session:
            return bool(
                await session.execute(
                    "UPDATE swe_agentobs_outbox_recovery SET "
                    "lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)) "
                    "WHERE scope_key=%s AND lease_owner=%s AND lease_until >= UTC_TIMESTAMP(6)",
                    (lease_seconds, self.scope_key, owner),
                ),
            )

    async def finish(self, owner: str, cursor: str | None) -> bool:
        async with self.connection.acquire() as session:
            return bool(
                await session.execute(
                    "UPDATE swe_agentobs_outbox_recovery SET scan_cursor=%s, "
                    "lease_owner='', lease_until=NULL WHERE scope_key=%s AND lease_owner=%s "
                    "AND lease_until >= UTC_TIMESTAMP(6)",
                    (cursor, self.scope_key, owner),
                ),
            )


class OutboxRecoveryWorker:
    def __init__(
        self,
        store: OutboxRecoveryStore,
        scanner: CommittedOutboxScanner,
        emitter: JsonlEmitter,
        *,
        page_size: int = 32,
        lease_seconds: int = 60,
        poll_seconds: float = 5.0,
    ):
        if not 1 <= page_size <= 100 or lease_seconds < 5 or poll_seconds <= 0:
            raise ValueError("invalid recovery limits")
        self.store = store
        self.scanner = scanner
        self.emitter = emitter
        self.page_size = page_size
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    async def run_once(self) -> int:
        owner = str(uuid4())
        cursor = await self.store.claim(owner, self.lease_seconds)
        if cursor is False:
            return 0
        page = await self.scanner.scan_page(cursor, self.page_size)
        if len(page.records) > self.page_size:
            raise ValueError("scanner exceeded recovery page limit")
        emitted = 0
        pending = 0
        oldest_seconds = 0.0
        for record in page.records:
            if not await self.store.renew(owner, self.lease_seconds):
                return emitted
            event = await self.scanner.verified_event(record)
            if event is None:
                logger.warning("Evidence outbox record failed verification")
                continue
            if not await self.store.acknowledged(event["event_id"]):
                pending += 1
                try:
                    occurred = datetime.fromisoformat(
                        event["occurred_at"].replace("Z", "+00:00"),
                    )
                    if occurred.tzinfo is not None:
                        oldest_seconds = max(
                            oldest_seconds,
                            (
                                datetime.now(timezone.utc) - occurred
                            ).total_seconds(),
                        )
                except (KeyError, ValueError, TypeError, AttributeError):
                    pass
                if self.emitter.emit(event):
                    emitted += 1
                else:
                    logger.warning("Evidence outbox replay was not accepted")
        if not await self.store.finish(owner, page.next_cursor):
            logger.warning("Evidence outbox recovery lease expired")
        if pending and oldest_seconds >= 60:
            logger.warning(
                "Evidence outbox backlog: pending=%d oldest_seconds=%d",
                pending,
                oldest_seconds,
            )
        return emitted

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Evidence outbox recovery cycle failed: %s",
                    type(exc).__name__,
                )
            await asyncio.sleep(self.poll_seconds)


class OutboxRecoveryScheduler:
    """Visit registered scopes in bounded pages; resolve each through trusted config."""

    def __init__(
        self,
        registry,
        service_unit: str,
        resolve_worker,
        *,
        page_size=16,
        poll_seconds=5.0,
    ):
        if not 1 <= page_size <= 100 or poll_seconds <= 0:
            raise ValueError("invalid scheduler limits")
        self.registry = registry
        self.service_unit = service_unit
        self.resolve_worker = resolve_worker
        self.page_size = page_size
        self.poll_seconds = poll_seconds
        self._cursor = b""
        self._task: asyncio.Task | None = None

    async def run_once(self):
        from ..app.conversation_storage.identity import (
            WorkspaceScope,
            service_unit_bytes,
        )

        rows = await self.registry.list_scopes(
            service_unit_bytes(self.service_unit),
            self._cursor,
            self.page_size,
        )
        for row in rows:
            key = bytes(row["scope_key"])
            self._cursor = key
            try:
                scope = WorkspaceScope.from_row(row)
                if (
                    scope.service_unit_id != self.service_unit
                    or scope_digest(scope) != key
                ):
                    raise ValueError("recovery registry identity mismatch")
                worker = await self.resolve_worker(
                    scope,
                    bytes(row["workspace_root"]).decode("utf-8"),
                    row["storage_authority"],
                )
                if worker is not None:
                    await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Evidence scope recovery failed: %s",
                    type(exc).__name__,
                )
        if len(rows) < self.page_size:
            self._cursor = b""
        return len(rows)

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Evidence recovery registry failed: %s",
                    type(exc).__name__,
                )
            await asyncio.sleep(self.poll_seconds)


async def rebind_offline(
    store,
    scope,
    old_scanner,
    new_scanner,
    *,
    old_root,
    old_authority,
    new_root,
    new_authority,
    writers_stopped,
    max_entries=10000,
):
    """Move a drained binding after an operator has stopped all writers/workers.

    Both scanners must perform strict complete verification and refuse malformed
    or ambiguous records. This operation never moves conversation data.
    """
    if writers_stopped is not True or not 1 <= max_entries <= 10000:
        raise ValueError("offline writer shutdown is required")
    if scope_digest(scope) != store.scope_key:
        raise ValueError("recovery scope identity mismatch")
    for scanner, root in ((old_scanner, old_root), (new_scanner, new_root)):
        if (
            scanner.scope != scope
            or str(scanner.workspace.workspace_dir.resolve(strict=True))
            != root
        ):
            raise ValueError("offline scanner binding mismatch")
    if old_authority not in (
        "legacy",
        "mysql-metadata",
    ) or new_authority not in ("legacy", "mysql-metadata"):
        raise ValueError("invalid storage authority")
    old_events = await old_scanner.scan_offline_complete(
        max_entries=max_entries,
    )
    new_events = await new_scanner.scan_offline_complete(
        max_entries=max_entries,
    )

    def index(events):
        if len(events) > max_entries:
            raise ValueError("offline scan exceeded limit")
        result = {}
        for item in events:
            if not isinstance(item, dict) or set(item) != {
                "event",
                "content_sha256",
            }:
                raise ValueError("invalid offline evidence digest")
            event = item["event"]
            digest = item["content_sha256"]
            if (
                not isinstance(event, dict)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef" for character in digest
                )
            ):
                raise ValueError("invalid offline evidence digest")
            event_id = event["event_id"]
            if event_id in result:
                raise ValueError("duplicate evidence event")
            result[event_id] = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        return result

    old = index(old_events)
    if old != index(new_events):
        raise ValueError("target outbox differs from source")
    async with store.connection.transaction() as session:
        row = await session.fetch_one(
            "SELECT service_unit_key,tenant_key,source_key,agent_key,workspace_root,storage_authority "
            "FROM swe_agentobs_outbox_scopes WHERE scope_key=%s FOR UPDATE",
            (store.scope_key,),
        )
        if (
            not row
            or tuple(
                bytes(row[name])
                for name in (
                    "service_unit_key",
                    "tenant_key",
                    "source_key",
                    "agent_key",
                )
            )
            != scope.sql_values
            or bytes(row["workspace_root"]) != old_root.encode()
            or row["storage_authority"] != old_authority
        ):
            raise ValueError("source binding changed")
        await session.execute(
            "INSERT IGNORE INTO swe_agentobs_outbox_recovery "
            "(scope_key,lease_owner,lease_until,scan_cursor) VALUES (%s,'',NULL,NULL)",
            (store.scope_key,),
        )
        lease = await session.fetch_one(
            "SELECT lease_until IS NOT NULL AND lease_until >= UTC_TIMESTAMP(6) AS active "
            "FROM swe_agentobs_outbox_recovery WHERE scope_key=%s FOR UPDATE",
            (store.scope_key,),
        )
        if lease is None or lease["active"]:
            raise ValueError("recovery lease is active")
        for event_id in old:
            ack = await session.fetch_one(
                "SELECT 1 AS acknowledged FROM swe_agentobs_inbox "
                "WHERE event_id=%s AND processing_state=1 LIMIT 1",
                (event_id,),
            )
            if not ack:
                raise ValueError("outbox has unacknowledged events")
        await session.execute(
            "UPDATE swe_agentobs_outbox_scopes SET workspace_root=%s,storage_authority=%s WHERE scope_key=%s",
            (new_root.encode(), new_authority, store.scope_key),
        )
        await session.execute(
            "UPDATE swe_agentobs_outbox_recovery SET scan_cursor=NULL, lease_owner='', lease_until=NULL WHERE scope_key=%s",
            (store.scope_key,),
        )
