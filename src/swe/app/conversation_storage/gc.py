# -*- coding: utf-8 -*-
"""Explicit, scope-fenced reclamation of unreferenced immutable publications.

Candidate scans are advisory. Mark and retry re-read every protection under
scope guard -> publication locks using READ COMMITTED reads, then unlink outside SQL.
No age-based expiry, automatic pin release, directory scan, or tombstone delete.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as keyword_field
import inspect
import logging

from .errors import ConflictError, IntegrityError
from .identity import identity_bytes, service_unit_bytes
from .retention import RetentionStore

logger = logging.getLogger(__name__)
TABLE = "swe_conversation_publications"
SCOPE = "scope_kind=%s AND service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s"


@dataclass(frozen=True)
class StopProof:
    operation_key: str
    owner_key: str
    generation: int
    evidence_ref: str
    owner_stopped: bool
    io_drained: bool
    service_unit_id: str = keyword_field(kw_only=True)

    def __post_init__(self):
        service_unit_bytes(self.service_unit_id)
        for value in (self.operation_key, self.owner_key, self.evidence_ref):
            identity_bytes(value, 1024)
            if not value:
                raise ValueError("writer stop proof fields must be nonempty")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("writer stop proof generation is invalid")


@dataclass(frozen=True)
class Candidate:
    publication_id: int
    operation_key: str
    state: str
    gc_state: str
    eligible: bool
    reasons: tuple[str, ...]


def _text(value):
    return (
        bytes(value).decode("utf-8")
        if isinstance(value, (bytes, bytearray))
        else value
    )


def _identity(value):
    if type(value) is not int or value <= 0:
        raise ValueError("GC publication identity must be a positive integer")


class GarbageCollector:
    def __init__(
        self,
        database,
        scope,
        bundles,
        *,
        physical_enabled=False,
        verify_stopped=None,
        scope_kind="workspace",
    ):
        if type(physical_enabled) is not bool:
            raise TypeError("physical GC enablement must be explicit boolean")
        self.db, self.scope, self.bundles = database, scope, bundles
        self.physical_enabled = physical_enabled
        self.verify_stopped = verify_stopped
        self.retentions = RetentionStore(
            database,
            scope,
            scope_kind=scope_kind,
        )

    def _enabled(self):
        if not self.physical_enabled:
            raise ConflictError("conversation GC is report-only")

    def _scope(self, row):
        if (
            tuple(
                row[key]
                for key in (
                    "scope_kind",
                    "service_unit_key",
                    "tenant_key",
                    "source_key",
                    "agent_key",
                )
            )
            != self.retentions.scope_values
        ):
            raise IntegrityError("GC publication scope mismatch")
        expected = self.bundles.path_for_row(row)
        if _text(row["relative_path"]) != expected:
            raise IntegrityError(
                "GC publication path differs from generated identity",
            )

    async def _row(self, publication_id, *, lock=False):
        _identity(publication_id)
        row = await self.db.fetch_one(
            f"SELECT * FROM {TABLE} WHERE service_unit_key=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (
                self.scope.sql_values[0],
                publication_id,
            ),
        )
        if row is None:
            raise KeyError(publication_id)
        self._scope(row)
        return row

    async def _proof(self, row, proof):
        if proof is not None and (
            not isinstance(proof, StopProof)
            or proof.service_unit_id != self.scope.service_unit_id
        ):
            raise ConflictError("GC proof service differs")
        if row["state"] != "aborted":
            return
        expected = (
            _text(row["operation_key"]),
            _text(row["owner_key"]),
            row["generation"],
        )
        if (
            proof is None
            or (proof.operation_key, proof.owner_key, proof.generation)
            != expected
            or proof.owner_stopped is not True
            or proof.io_drained is not True
            or self.verify_stopped is None
        ):
            raise ConflictError(
                "aborted writer requires verified incarnation stop and drained IO evidence",
            )
        accepted = self.verify_stopped(proof)
        if inspect.isawaitable(accepted):
            accepted = await accepted
        if accepted is not True:
            raise ConflictError("writer stop evidence was not verified")

    @asynccontextmanager
    async def _write_transaction(self):
        # Reverse references must see committed changes after publication-lock
        # waits, without locking Session/Chat/Turn rows in reverse order.
        # SET TRANSACTION applies only to the next transaction on this lease.
        async with self.db.acquire() as session:
            if session._in_transaction:
                raise ConflictError(
                    "GC requires a dedicated READ COMMITTED transaction",
                )
            await session.execute(
                "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
            )
            async with self.db.transaction():
                yield

    async def _unlink(self, relative_path):
        worker = asyncio.create_task(
            asyncio.to_thread(self.bundles.reclaim, relative_path),
        )
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            worker.exception()
            raise asyncio.CancelledError
        return worker.result()

    async def _protections(self, row):
        identity = row["id"]
        reasons = []
        if row["state"] not in ("committed", "aborted"):
            reasons.append("operation_not_terminal")
        if row["retained_history"]:
            reasons.append("retained_history")
        queries = (
            (
                "message",
                "SELECT m.id FROM swe_conversation_messages m LEFT JOIN swe_conversation_chats c ON c.id=m.archive_chat_id AND c.service_unit_key=m.service_unit_key WHERE m.publication_id=%s AND m.deleted_at IS NULL AND ((m.layer<>'archive' AND m.current_visible=1) OR (m.layer='archive' AND (c.id IS NULL OR c.deleted_at IS NULL))) LIMIT 1",
                (identity,),
            ),
            (
                "archive_boundary",
                "SELECT m.id FROM swe_conversation_messages m LEFT JOIN swe_conversation_chats c ON c.id=m.archive_chat_id AND c.service_unit_key=m.service_unit_key WHERE m.archive_publication_id=%s AND m.layer='archive' AND m.deleted_at IS NULL AND (c.id IS NULL OR c.deleted_at IS NULL) LIMIT 1",
                (identity,),
            ),
            (
                "session_root",
                "SELECT id FROM swe_conversation_sessions WHERE recovery_publication_id=%s LIMIT 1",
                (identity,),
            ),
            (
                "turn_admission",
                "SELECT id FROM swe_conversation_turns WHERE admission_publication_id=%s LIMIT 1",
                (identity,),
            ),
            (
                "turn_terminal",
                "SELECT id FROM swe_conversation_turns WHERE terminal_publication_id=%s LIMIT 1",
                (identity,),
            ),
            (
                "chat_checkpoint",
                "SELECT id FROM swe_conversation_chats WHERE active_checkpoint_publication_id=%s AND deleted_at IS NULL LIMIT 1",
                (identity,),
            ),
            (
                "chat_content",
                "SELECT id FROM swe_conversation_chats WHERE content_meta_publication_id=%s AND deleted_at IS NULL LIMIT 1",
                (identity,),
            ),
            (
                "share",
                "SELECT token FROM swe_chat_shares WHERE publication_id=%s LIMIT 1",
                (identity,),
            ),
            (
                "retention",
                "SELECT id FROM swe_conversation_retentions WHERE publication_id=%s AND state='active' LIMIT 1",
                (identity,),
            ),
        )
        for reason, query, params in queries:
            alias = "m." if "SELECT m.id" in query else ""
            query = query.replace(
                " WHERE ",
                " WHERE " + alias + "service_unit_key=%s AND ",
                1,
            )
            params = (self.scope.sql_values[0], *params)
            if await self.db.fetch_one(query, params) is not None:
                reasons.append(reason)
        if await self._anomaly(row):
            reasons.append("scope_or_dangling_reference")
        return tuple(reasons)

    async def _anomaly(self, row):
        values = self.scope.sql_values
        for table, field in (
            ("sessions", "session_id"),
            ("chats", "chat_id"),
            ("turns", "turn_id"),
        ):
            if row[field] is None:
                continue
            association = await self.db.fetch_one(
                f"SELECT * FROM swe_conversation_{table} WHERE service_unit_key=%s AND id=%s",
                (
                    values[0],
                    row[field],
                ),
            )
            if association is None:
                return True
            if (
                table != "turns"
                and tuple(
                    association[key]
                    for key in (
                        "service_unit_key",
                        "tenant_key",
                        "source_key",
                        "agent_key",
                    )
                )
                != values
            ):
                return True
            if (
                table == "turns"
                and association["session_id"] != row["session_id"]
            ):
                return True
            if table == "turns" and association["chat_id"] != row["chat_id"]:
                return True
        queries = (
            (
                "SELECT m.id FROM swe_conversation_messages m LEFT JOIN swe_conversation_sessions s ON s.id=m.session_id AND s.service_unit_key=m.service_unit_key WHERE m.service_unit_key=%s AND (m.publication_id=%s OR m.archive_publication_id=%s) AND (s.id IS NULL OR s.service_unit_key<>%s OR s.tenant_key<>%s OR s.source_key<>%s OR s.agent_key<>%s OR NOT(m.session_id<=>%s)) LIMIT 1",
                (values[0], row["id"], row["id"], *values, row["session_id"]),
            ),
            (
                "SELECT id FROM swe_conversation_retentions WHERE service_unit_key=%s AND publication_id=%s AND (scope_kind<>%s OR service_unit_key<>%s OR tenant_key<>%s OR source_key<>%s OR agent_key<>%s) LIMIT 1",
                (values[0], row["id"], *self.retentions.scope_values),
            ),
        )
        for query, params in queries:
            if await self.db.fetch_one(query, params) is not None:
                return True
        return False

    async def report(self, *, limit=100, after_id=0):
        if (
            type(limit) is not int
            or not 0 < limit <= 1000
            or type(after_id) is not int
            or after_id < 0
        ):
            raise ValueError("GC report requires bounded keyset parameters")
        rows = await self.db.fetch_all(
            f"SELECT * FROM {TABLE} WHERE {SCOPE} AND id>%s ORDER BY id LIMIT %s",
            (*self.retentions.scope_values, after_id, limit),
        )
        result = []
        for row in rows:
            try:
                self._scope(row)
                reasons = await self._protections(row)
            except (ValueError, IntegrityError):
                reasons = ("invalid_publication_identity",)
            if row["state"] == "aborted":
                reasons = (*reasons, "writer_stop_evidence_required")
            if row["gc_state"] != "live":
                reasons = (*reasons, "gc_" + row["gc_state"])
            result.append(
                Candidate(
                    row["id"],
                    _text(row["operation_key"]),
                    row["state"],
                    row["gc_state"],
                    not reasons,
                    reasons,
                ),
            )
        return result

    async def mark(self, publication_id, *, proof=None):
        self._enabled()
        before = await self._row(publication_id)
        await self._proof(before, proof)
        async with self._write_transaction():
            await self.retentions.assert_writes_allowed()
            row = await self._row(publication_id, lock=True)
            if (
                row["operation_key"],
                row["owner_key"],
                row["generation"],
                row["state"],
            ) != (
                before["operation_key"],
                before["owner_key"],
                before["generation"],
                before["state"],
            ):
                raise ConflictError("GC publication authority changed")
            if row["gc_state"] in ("deleting", "reclaimed"):
                return row
            reasons = await self._protections(row)
            if reasons:
                raise ConflictError(
                    "GC publication is protected: " + ",".join(reasons),
                )
            changed = await self.db.execute(
                f"UPDATE {TABLE} SET gc_state='deleting' WHERE service_unit_key=%s AND id=%s AND gc_state='live'",
                (
                    self.scope.sql_values[0],
                    publication_id,
                ),
            )
            if changed != 1:
                raise ConflictError("GC deleting transition was not confirmed")
        logger.info(
            "conversation_gc_mark publication=%s operation=%s proof=%s",
            publication_id,
            _text(row["operation_key"]),
            proof.evidence_ref if proof else None,
        )
        return {**row, "gc_state": "deleting"}

    async def reclaim(self, publication_id, *, proof=None):
        self._enabled()
        before = await self._row(publication_id)
        await self._proof(before, proof)
        async with self._write_transaction():
            await self.retentions.assert_writes_allowed()
            row = await self._row(publication_id, lock=True)
            if row["gc_state"] == "reclaimed" and row["state"] != "aborted":
                return row
            if row["gc_state"] not in ("deleting", "reclaimed"):
                raise ConflictError(
                    "physical reclaim requires deleting tombstone",
                )
            if (row["owner_key"], row["generation"], row["state"]) != (
                before["owner_key"],
                before["generation"],
                before["state"],
            ):
                raise ConflictError("GC publication authority changed")
            reasons = await self._protections(row)
            if reasons:
                raise ConflictError(
                    "GC publication is protected: " + ",".join(reasons),
                )
        # Aborted tombstones are intentionally rechecked on explicit retries:
        # a delayed NAS object must never escape its exact operation tombstone.
        await self._unlink(_text(row["relative_path"]))
        async with self.db.transaction():
            await self.retentions.lock_guard()
            current = await self._row(publication_id, lock=True)
            if current["gc_state"] not in ("deleting", "reclaimed"):
                raise IntegrityError(
                    "GC publication state changed after unlink",
                )
            await self.db.execute(
                f"UPDATE {TABLE} SET gc_state='reclaimed',reclaimed_at=COALESCE(reclaimed_at,UTC_TIMESTAMP(6)) WHERE service_unit_key=%s AND id=%s AND gc_state='deleting'",
                (
                    self.scope.sql_values[0],
                    publication_id,
                ),
            )
            await self.retentions.release_dependencies(publication_id)
        logger.info(
            "conversation_gc_reclaimed publication=%s operation=%s",
            publication_id,
            _text(row["operation_key"]),
        )
        return {**row, "gc_state": "reclaimed"}
