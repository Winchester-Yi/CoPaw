# -*- coding: utf-8 -*-
"""Explicit reference protection for readers, backups and older dependencies.

Reader batches pin committed/live publication rows in integer ID order. A
reader must select its locators and authorize them in the same outer database
transaction as pin(); NAS IO follows only after that transaction commits.
Released rows remain evidence; no TTL guesses that a reader or writer died.
"""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
import asyncio
import secrets

from ...database.prepared import PreparedInsert
from .errors import ConflictError, IntegrityError
from .identity import WorkspaceScope, identity_bytes
from .reference_queries import ReferenceQuery, fetch_reference_rows

TABLE = "swe_conversation_retentions"
SCOPE_WHERE = "scope_kind=%s AND service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s"
MAX_PIN_BATCH = 4096
MEMBER_FIELDS = (
    "retention_key",
    "member_key",
    "kind",
    "scope_kind",
    "service_unit_key",
    "tenant_key",
    "source_key",
    "agent_key",
    "publication_id",
    "guard_marker",
    "state",
    "created_at",
)
MEMBER_INSERT = PreparedInsert(
    f"INSERT INTO {TABLE} ({','.join(MEMBER_FIELDS)}) VALUES ",
    "(" + ",".join(["%s"] * len(MEMBER_FIELDS)) + ")",
    " ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",
)
MEMBER_CHECK_FIELDS = (
    "service_unit_key",
    "scope_kind",
    "tenant_key",
    "source_key",
    "agent_key",
    "member_key",
    "publication_id",
    "kind",
    "state",
    "guard_marker",
    "snapshot_ref",
)


def _ids(values, *, bounded=True):
    result = sorted(set(values))
    if (bounded and len(result) > MAX_PIN_BATCH) or any(
        type(value) is not int or value <= 0 for value in result
    ):
        raise ValueError(
            "publication pins require bounded positive integer IDs",
        )
    return result


@dataclass
class PinBatch:
    retention_key: bytes
    publications: tuple[dict, ...]
    _workers: set = field(default_factory=set, repr=False)
    _closed: bool = field(default=False, repr=False)

    async def read(self, function, *args, **kwargs):
        """Offload bounded NAS IO without releasing its pins on cancellation."""
        if self._closed:
            raise ConflictError("reader pin batch is closed")
        worker = asyncio.create_task(
            asyncio.to_thread(function, *args, **kwargs),
        )
        self._workers.add(worker)
        cancelled = False
        try:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                if not worker.cancelled():
                    worker.exception()
                raise asyncio.CancelledError
            return worker.result()
        finally:
            self._workers.discard(worker)

    async def drain(self):
        self._closed = True
        cancelled = False
        while self._workers:
            pending = asyncio.gather(*self._workers, return_exceptions=True)
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            self._workers.difference_update(
                {worker for worker in self._workers if worker.done()},
            )
        return cancelled


class ReaderPins:
    """Keep all bounded batches in one locator snapshot until IO has drained."""

    def __init__(self, store):
        self.store = store
        self.batches = []
        self._cleanup = AsyncExitStack()

    @property
    def publications(self):
        return tuple(
            row for batch in self.batches for row in batch.publications
        )

    async def pin(self, ids):
        ordered = sorted(set(ids))
        for start in range(0, len(ordered), MAX_PIN_BATCH):
            batch = await self.store.pin(
                ordered[start : start + MAX_PIN_BATCH],
                kind="reader",
            )
            self.batches.append(batch)
            self._cleanup.push_async_callback(self.store.finish_reader, batch)

    async def read(self, function, *args, **kwargs):
        # All batches remain pinned together; one tracks their shared NAS IO.
        return await self.batches[-1].read(function, *args, **kwargs)

    async def finish(self):
        await self._cleanup.aclose()


class RetentionStore:
    """One trusted namespace; legacy shares have an explicit separate kind."""

    def __init__(
        self,
        database,
        scope: WorkspaceScope,
        *,
        scope_kind="workspace",
    ):
        if scope_kind not in ("workspace", "legacy_share"):
            raise ValueError("invalid retention scope kind")
        if scope_kind == "legacy_share" and (
            scope.source_id or scope.agent_id
        ):
            raise ValueError(
                "legacy share scope cannot invent source or agent identity",
            )
        self.db = database
        self.scope = scope
        self.scope_kind = scope_kind

    @property
    def scope_values(self):
        return (self.scope_kind, *self.scope.sql_values)

    def _validate_scope(self, row):
        values = tuple(
            row[key]
            for key in (
                "scope_kind",
                "service_unit_key",
                "tenant_key",
                "source_key",
                "agent_key",
            )
        )
        if values != self.scope_values:
            raise IntegrityError("retention publication scope mismatch")

    async def _insert(
        self,
        key,
        publication_id,
        kind,
        *,
        guard=False,
        snapshot_ref=None,
    ):
        fields = {
            "retention_key": key,
            "member_key": publication_id or 0,
            "kind": kind,
            "scope_kind": self.scope_kind,
            **self.scope.fields,
            "publication_id": publication_id,
            "guard_marker": 1 if guard else None,
        }
        if snapshot_ref is not None:
            fields["snapshot_ref"] = snapshot_ref
        columns = ",".join(fields)
        placeholders = ",".join(["%s"] * len(fields))
        await self.db.insert(
            f"INSERT INTO {TABLE} ({columns},state,created_at) "
            f"VALUES ({placeholders},'active',UTC_TIMESTAMP(6)) "
            "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",
            tuple(fields.values()),
        )

    async def _insert_members(self, key, ids, kind):
        if len(ids) == 1:
            await self._insert(key, ids[0], kind)
        elif ids:
            clock = await self.db.fetch_one(
                "SELECT UTC_TIMESTAMP(6) AS created_at",
            )
            await self.db.insert_prepared(
                MEMBER_INSERT,
                (
                    (
                        key,
                        identity,
                        kind,
                        self.scope_kind,
                        *self.scope.sql_values,
                        identity,
                        None,
                        "active",
                        clock["created_at"],
                    )
                    for identity in ids
                ),
            )

    async def ensure_guard(self):
        """The unique scope marker arbitrates first-use races without a Session."""
        async with self.db.transaction():
            await self._insert(
                secrets.token_hex(32).encode("ascii"),
                None,
                "gc_guard",
                guard=True,
            )
            row = await self.db.fetch_one(
                f"SELECT * FROM {TABLE} WHERE {SCOPE_WHERE} AND guard_marker=1 FOR UPDATE",
                self.scope_values,
            )
            if row is None:
                raise IntegrityError("scope GC guard is missing")
            self._validate_scope(row)
            shape = (
                row["kind"],
                row["state"],
                row["member_key"],
                row["publication_id"],
                row["guard_marker"],
            )
            if shape != ("gc_guard", "active", 0, None, 1):
                raise IntegrityError(
                    "scope GC guard has invalid fixed metadata",
                )
            return row

    async def lock_guard(self):
        """Use inside the caller transaction before publication row locks."""
        await self.ensure_guard()
        return await self.db.fetch_one(
            f"SELECT * FROM {TABLE} WHERE {SCOPE_WHERE} AND guard_marker=1 FOR UPDATE",
            self.scope_values,
        )

    async def assert_writes_allowed(self):
        """Call inside a short write transaction before Session/publication locks."""
        await self.lock_guard()
        fences = await self.db.fetch_all(
            f"SELECT * FROM {TABLE} WHERE {SCOPE_WHERE} AND kind='backup_fence' AND state='active' ORDER BY id FOR UPDATE",
            self.scope_values,
        )
        if fences:
            raise ConflictError(
                "conversation scope has an active backup fence",
            )

    @staticmethod
    def _backup_identity(retention_key, snapshot_ref):
        key = identity_bytes(retention_key, 128)
        snapshot = identity_bytes(snapshot_ref, 1024)
        if not key or not snapshot:
            raise ValueError(
                "backup identity and snapshot reference must be nonempty",
            )
        return key, snapshot

    async def _backup_fence(self, key, snapshot):
        row = await self.db.fetch_one(
            f"SELECT * FROM {TABLE} WHERE retention_key=%s AND service_unit_key=%s "
            "AND member_key=0 FOR UPDATE",
            (key, self.scope.sql_values[0]),
        )
        if row is None:
            raise ConflictError("backup fence identity is missing")
        self._validate_scope(row)
        if (
            row["kind"],
            row["publication_id"],
            row["guard_marker"],
            row["snapshot_ref"],
        ) != ("backup_fence", None, None, snapshot):
            raise ConflictError("backup fence identity or snapshot changed")
        return row

    async def install_backup_fence(self, retention_key, snapshot_ref):
        """Persist an explicit job fence; failures never remove protection."""
        key, snapshot = self._backup_identity(retention_key, snapshot_ref)
        async with self.db.transaction():
            await self.lock_guard()
            active = await self.db.fetch_all(
                f"SELECT * FROM {TABLE} WHERE {SCOPE_WHERE} "
                "AND kind='backup_fence' AND state='active' "
                "ORDER BY id FOR UPDATE",
                self.scope_values,
            )
            if any(row["retention_key"] != key for row in active):
                raise ConflictError("another backup fence is active")
            await self._insert(
                key,
                None,
                "backup_fence",
                snapshot_ref=snapshot,
            )
            row = await self._backup_fence(key, snapshot)
            if row["state"] != "active":
                raise ConflictError("backup fence identity is released")
            return row

    async def release_backup_fence(self, retention_key, snapshot_ref):
        """Release only this captured job after its complete pin set exists."""
        key, snapshot = self._backup_identity(retention_key, snapshot_ref)
        async with self.db.transaction():
            await self.lock_guard()
            row = await self._backup_fence(key, snapshot)
            if row["state"] == "released":
                return
            if row["state"] != "active":
                raise ConflictError("backup fence has invalid state")
            changed = await self.db.execute(
                f"UPDATE {TABLE} SET state='released',"
                "released_at=UTC_TIMESTAMP(6) WHERE retention_key=%s "
                f"AND {SCOPE_WHERE} AND kind='backup_fence' "
                "AND state='active' AND snapshot_ref=%s",
                (key, *self.scope_values, snapshot),
            )
            if changed != 1:
                raise ConflictError("backup fence release was not confirmed")

    async def _lock_publications(self, values):
        ids = _ids(values)
        if not ids:
            return []
        by_id = await fetch_reference_rows(
            self.db,
            ReferenceQuery.PUBLICATION_LOCK,
            ids,
            self.scope.sql_values[0],
        )
        if set(by_id) != set(ids):
            raise IntegrityError("retention target publication is missing")
        rows = [by_id[identity] for identity in ids]
        for row in rows:
            self._validate_scope(row)
        return rows

    async def pin(self, publication_ids, *, kind, retention_key=None):
        if kind not in ("reader", "backup"):
            raise ValueError("ordinary pin kind must be reader or backup")
        key = (
            identity_bytes(retention_key, 128)
            if retention_key is not None
            else secrets.token_hex(32).encode("ascii")
        )
        async with self.db.transaction():
            rows = await self._lock_publications(publication_ids)
            for row in rows:
                if row["state"] != "committed" or row["gc_state"] != "live":
                    raise ConflictError(
                        "publication is not committed/live for pinning",
                    )
            await self._insert_members(key, [row["id"] for row in rows], kind)
            await self._validate_members(
                key,
                [row["id"] for row in rows],
                kind,
            )
        return PinBatch(key, tuple(rows))

    async def _fetch_members(self, key, limit, *, after=None):
        where = "retention_key=%s AND service_unit_key=%s"
        params = (key, self.scope.sql_values[0])
        if after is not None:
            where += " AND member_key>%s"
            params += (after,)
        return await self.db.fetch_all(
            f"SELECT {','.join(MEMBER_CHECK_FIELDS)} FROM {TABLE} WHERE {where} "
            "ORDER BY member_key LIMIT %s FOR UPDATE",
            (*params, limit),
        )

    async def _validate_members(self, key, publication_ids, kind):
        position = 0
        after = None
        limit = (
            len(publication_ids) + 1
            if len(publication_ids) <= MAX_PIN_BATCH
            else 256
        )
        while True:
            members = await self._fetch_members(key, limit, after=after)
            for row in members:
                if position >= len(publication_ids):
                    raise ConflictError(
                        "retention identity already has a different member set",
                    )
                self._validate_scope(row)
                identity = publication_ids[position]
                current = (
                    row["publication_id"],
                    row["member_key"],
                    row["kind"],
                    row["state"],
                )
                if (
                    current != (identity, identity, kind, "active")
                    or row["guard_marker"] is not None
                ):
                    raise ConflictError(
                        "retention identity is released or differs from this pin",
                    )
                position += 1
            if len(members) < limit:
                break
            after = members[-1]["member_key"]
        if position != len(publication_ids):
            raise ConflictError(
                "retention identity already has a different member set",
            )

    async def release(self, retention_key: bytes):
        await self.db.execute(
            f"UPDATE {TABLE} SET state='released',released_at=UTC_TIMESTAMP(6) "
            f"WHERE retention_key=%s AND {SCOPE_WHERE} AND kind IN ('reader','backup') AND state='active'",
            (retention_key, *self.scope_values),
        )

    @asynccontextmanager
    async def reader(self, publication_ids):
        batch = await self.pin(publication_ids, kind="reader")
        try:
            yield batch
        finally:
            await self.finish_reader(batch)

    async def finish_reader(self, batch: PinBatch):
        """Drain IO, then release on the active database session's task."""
        cancelled = await batch.drain()
        if getattr(self.db, "has_current_session", False):
            await self.release(batch.retention_key)
        elif hasattr(self.db, "create_independent_task"):
            release = self.db.create_independent_task(
                self.release(batch.retention_key),
            )
            while not release.done():
                try:
                    await asyncio.shield(release)
                except asyncio.CancelledError:
                    cancelled = True
            release.result()
        else:
            try:
                await self.release(batch.retention_key)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def add_dependencies(self, source_id: int, target_ids):
        targets = _ids(target_ids, bounded=False)
        if any(value >= source_id for value in targets):
            raise IntegrityError(
                "dependencies must point to older publications",
            )
        async with self.db.transaction():
            ordered = [*targets, source_id]
            rows = []
            for start in range(0, len(ordered), MAX_PIN_BATCH):
                rows.extend(
                    await self._lock_publications(
                        ordered[start : start + MAX_PIN_BATCH],
                    ),
                )
            await self._install_locked_dependencies(rows[-1], rows[:-1])

    async def _install_locked_dependencies(self, source, rows):
        """Consume locks held by the caller's current publication transaction."""
        async with self.db.acquire() as session:
            if not session._in_transaction:
                raise RuntimeError(
                    "dependency installation requires the locking transaction",
                )
            self._validate_scope(source)
            targets = [row["id"] for row in rows]
            if targets != _ids(targets, bounded=False) or any(
                value >= source["id"] for value in targets
            ):
                raise IntegrityError(
                    "dependencies must point to older publications",
                )
            if source["gc_state"] != "live" or source["state"] not in (
                "prepared",
                "committed",
            ):
                raise ConflictError("dependency source is not publishable")
            for row in rows:
                self._validate_scope(row)
                if row["state"] != "committed" or row["gc_state"] != "live":
                    raise ConflictError(
                        "dependency target is not committed/live",
                    )
            key = f"dep:{source['id']}".encode("ascii")
            await self._insert_members(key, targets, "dependency")
            await self._validate_members(key, targets, "dependency")

    async def release_dependencies(self, source_id: int):
        async with self.db.transaction():
            rows = await self._lock_publications([source_id])
            if rows[0]["gc_state"] != "reclaimed":
                raise ConflictError(
                    "dependency source must be physically reclaimed",
                )
            await self.db.execute(
                f"UPDATE {TABLE} SET state='released',released_at=UTC_TIMESTAMP(6) "
                f"WHERE retention_key=%s AND {SCOPE_WHERE} AND kind='dependency' AND state='active'",
                (f"dep:{source_id}".encode("ascii"), *self.scope_values),
            )
