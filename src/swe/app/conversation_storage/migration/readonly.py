# -*- coding: utf-8 -*-
"""Explicit offline readers for a frozen target, with SQL writes rejected."""

from contextlib import asynccontextmanager
import asyncio
import re
from types import SimpleNamespace

from ..errors import ConflictError
from ..retention import PinBatch, RetentionStore
from .manifest import SourceBoundary


class ReadOnlyDatabase:
    """A narrow SQL facade; transactions yield this facade, never raw sessions."""

    def __init__(self, database, boundary):
        if not isinstance(boundary, SourceBoundary):
            raise ValueError(
                "offline verification requires a fixed stopped-writer boundary",
            )
        self._database = database
        self.boundary = boundary

    @asynccontextmanager
    async def acquire(self):
        async with self._database.acquire() as session:
            yield SimpleNamespace(
                max_allowed_packet=session.max_allowed_packet,
                fetch_one=self.fetch_one,
                fetch_all=self.fetch_all,
            )

    @property
    def is_connected(self):
        return self._database.is_connected

    @staticmethod
    def _query(query):
        if (
            not query.lstrip().upper().startswith("SELECT ")
            or ";" in query
            or re.search(
                r"\b(?:INTO|OUTFILE|DUMPFILE|GET_LOCK|RELEASE_LOCK)\b",
                query,
                re.IGNORECASE,
            )
        ):
            raise RuntimeError(
                "offline verification is read-only; SQL mutation rejected",
            )
        return query

    async def fetch_one(self, query, params=None):
        return await self._database.fetch_one(self._query(query), params)

    async def fetch_all(self, query, params=None):
        return await self._database.fetch_all(self._query(query), params)

    async def execute(self, *_args, **_kwargs):
        raise RuntimeError(
            "offline verification is read-only; SQL mutation rejected",
        )

    insert = execute
    execute_many = execute
    execute_result = execute
    insert_prepared = execute

    @asynccontextmanager
    async def transaction(self):
        async with self._database.transaction():
            yield self


class ReadOnlyRetention(RetentionStore):
    """Only offline verification may omit pins while all writers/GC are stopped."""

    def __init__(self, database, scope, *, scope_kind="workspace"):
        if not isinstance(database, ReadOnlyDatabase):
            raise ValueError(
                "nonpersistent pins require the offline read-only database",
            )
        super().__init__(database, scope, scope_kind=scope_kind)

    async def pin(self, publication_ids, *, kind, retention_key=None):
        if kind != "reader" or retention_key is not None:
            raise ValueError(
                "offline verification only supports transient reader batches",
            )
        rows = await self._lock_publications(publication_ids)
        if any(
            row["state"] != "committed" or row["gc_state"] != "live"
            for row in rows
        ):
            raise ConflictError(
                "publication is not committed/live for offline reading",
            )
        return PinBatch(b"offline-verification-no-persistent-pin", tuple(rows))

    async def finish_reader(self, batch):
        if await batch.drain():
            raise asyncio.CancelledError


def read_only_adapter(adapter):
    """Inject offline retention into this explicit adapter and its Chat reader."""
    current = adapter.retentions
    adapter.retentions = ReadOnlyRetention(
        adapter.db,
        current.scope,
        scope_kind=current.scope_kind,
    )
    if hasattr(adapter, "chats"):
        read_only_adapter(adapter.chats)
    return adapter
