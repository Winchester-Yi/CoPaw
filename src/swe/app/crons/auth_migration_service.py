# -*- coding: utf-8 -*-
"""File-authoritative migration; each call retains at most one bounded page."""

from __future__ import annotations

import asyncio
import socket
from collections import Counter
from pathlib import Path

from .auth_migration_models import (
    AuthMigrationItem,
    AuthMigrationPage,
    AuthMigrationRequest,
    Phase,
)
from .auth_migration_source import (
    check_root,
    matches,
    read_record,
    read_source_page,
)
from .auth_repository import (
    CronAuthRepository,
    STATE_FIELDS,
    TIME_FIELDS,
    _scope_metadata,
)
from .auth_state import CronAuthState, ensure_utc


def _normalized(state: CronAuthState) -> dict:
    result = state.model_dump(mode="json")
    for name in TIME_FIELDS:
        value = ensure_utc(getattr(state, name))
        result[name] = value.isoformat() if value else None
    return result


def _item(scope: str, status: str, **kwargs) -> AuthMigrationItem:
    tenant, source = _scope_metadata(scope)
    warnings = ["unknown_metadata"] if tenant is None or source is None else []
    return AuthMigrationItem(
        storage_scope_id=scope,
        tenant_id=tenant,
        source_id=source,
        status=status,
        warnings=warnings,
        **kwargs,
    )


class AuthMigrationService:
    def __init__(self, root: Path, repository: CronAuthRepository):
        self.root = root
        self.repository = repository
        self.instance_id = socket.gethostname()

    async def page(
        self,
        request: AuthMigrationRequest,
        *,
        phase: Phase,
        after: str | None,
    ) -> AuthMigrationPage:
        await self.repository.check_schema()
        if phase == "database":
            return await self._database_page(request, after)
        source = await asyncio.to_thread(
            read_source_page,
            self.root,
            request,
            after,
        )
        page = AuthMigrationPage(
            phase="files",
            scanned_files=source.scanned,
            next_phase="files" if source.next_after else "database",
            next_after=source.next_after,
        )
        for record in source.items:
            scope = record.storage_scope_id
            if record.state is None:
                page.items.append(
                    _item(scope, "invalid_source", reason=record.reason),
                )
                continue
            if request.operation == "import":
                await self.repository.upsert(scope, record.state)
                page.items.append(_item(scope, "imported"))
                continue
            row = await self.repository.get(scope)
            try:
                differences = self._differences(row, record.state)
            except (ValueError, TypeError, KeyError, RecursionError):
                page.items.append(
                    _item(
                        scope,
                        "invalid_database",
                        reason="invalid_database_state",
                    ),
                )
                continue
            if row is None:
                status = (
                    "would_insert"
                    if request.operation == "preview"
                    else "missing"
                )
            elif differences:
                status = (
                    "would_update"
                    if request.operation == "preview"
                    else "different"
                )
            else:
                status = "unchanged"
            page.items.append(_item(scope, status, differences=differences))
        page.counts = dict(Counter(item.status for item in page.items))
        return page

    def _differences(
        self,
        row: dict | None,
        state: CronAuthState,
    ) -> list[str]:
        if row is None:
            return []
        current = _normalized(self.repository.state_from_row(row))
        expected = _normalized(state)
        return [
            name for name in STATE_FIELDS if current[name] != expected[name]
        ]

    async def _database_page(self, request, after):
        await asyncio.to_thread(check_root, self.root)
        rows = await self.repository.list_rows(after, request.page_size + 1)
        selected = rows[: request.page_size]
        more = len(rows) > request.page_size
        page = AuthMigrationPage(
            phase="database",
            scanned_database_rows=len(selected),
            next_phase="database",
            scan_complete=not more,
            next_after=selected[-1]["storage_scope_id"] if more else None,
        )
        for row in selected:
            scope = row["storage_scope_id"]
            if not matches(scope, request):
                continue
            record = await asyncio.to_thread(read_record, self.root, scope)
            if record.status == "valid":
                if request.operation == "prune":
                    page.items.append(_item(scope, "kept"))
                continue
            if record.status == "invalid":
                page.items.append(
                    _item(scope, "invalid_source", reason=record.reason),
                )
                continue
            status = "database_only"
            if request.operation == "prune":
                # Recheck current absence immediately before each delete.
                record = await asyncio.to_thread(read_record, self.root, scope)
                if record.status != "absent":
                    page.items.append(
                        _item(
                            scope,
                            "kept",
                            reason="source_changed_or_unreadable",
                        ),
                    )
                    continue
                await self.repository.delete(scope)
                status = "pruned"
            page.items.append(_item(scope, status))
        page.counts = dict(Counter(item.status for item in page.items))
        return page
