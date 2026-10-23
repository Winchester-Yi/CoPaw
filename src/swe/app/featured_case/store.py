# -*- coding: utf-8 -*-
"""Database-backed featured-case storage and ordering."""

import json
import logging
from typing import Any, Optional

from .models import CaseStep, FeaturedCase, FeaturedCaseReorderResult

logger = logging.getLogger(__name__)

HEAD_OFFICE_BBK_ID = "100"


def normalize_featured_case_bbk_id(bbk_id: Optional[str]) -> str:
    """Return the canonical ordering scope for a featured-case BBK value."""
    normalized = str(bbk_id or "").strip()
    if normalized and normalized != HEAD_OFFICE_BBK_ID:
        return normalized
    return HEAD_OFFICE_BBK_ID


def is_head_office_bbk_id(bbk_id: Optional[str]) -> bool:
    """Return whether a BBK value identifies the shared head-office scope."""
    return normalize_featured_case_bbk_id(bbk_id) == HEAD_OFFICE_BBK_ID


def _scope_condition(
    bbk_id: Optional[str],
    column: str = "bbk_id",
) -> tuple[str, list[str]]:
    """Build an exact logical-scope predicate and its parameters."""
    scope_bbk_id = normalize_featured_case_bbk_id(bbk_id)
    if scope_bbk_id == HEAD_OFFICE_BBK_ID:
        return (
            f"({column} IS NULL OR TRIM({column}) = '' OR {column} = %s)",
            [HEAD_OFFICE_BBK_ID],
        )
    return f"{column} = %s", [scope_bbk_id]


class FeaturedCaseStore:
    """Store for featured-case display, management, and queue mutations."""

    def __init__(self, db: Optional[Any] = None):
        self.db = db
        self._use_db = db is not None and db.is_connected

    # ==================== Case display queries ====================

    async def get_cases_for_dimension(
        self,
        source_id: str,
        bbk_id: Optional[str] = None,
    ) -> list[dict]:
        """Return all active cases for runtime display in context order."""
        if not self._use_db:
            return []

        scope_bbk_id = normalize_featured_case_bbk_id(bbk_id)
        head_condition, head_params = _scope_condition(HEAD_OFFICE_BBK_ID)

        if scope_bbk_id == HEAD_OFFICE_BBK_ID:
            query = f"""
                SELECT id, label, value, image_url,
                       iframe_url, iframe_title, steps, sort_order
                FROM swe_featured_case
                WHERE source_id = %s
                  AND {head_condition}
                  AND is_active = 1
                ORDER BY sort_order ASC, id ASC
            """
            rows = await self.db.fetch_all(
                query,
                tuple([source_id, *head_params]),
            )
        else:
            query = f"""
                SELECT id, label, value, image_url,
                       iframe_url, iframe_title, steps, sort_order
                FROM swe_featured_case
                WHERE source_id = %s
                  AND (bbk_id = %s OR {head_condition})
                  AND is_active = 1
                ORDER BY
                    CASE WHEN bbk_id = %s THEN 0 ELSE 1 END,
                    sort_order ASC,
                    id ASC
            """
            rows = await self.db.fetch_all(
                query,
                tuple(
                    [
                        source_id,
                        scope_bbk_id,
                        *head_params,
                        scope_bbk_id,
                    ],
                ),
            )

        result = []
        for row in rows:
            steps = None
            if row["steps"]:
                try:
                    steps = json.loads(row["steps"])
                except (json.JSONDecodeError, TypeError):
                    steps = None

            detail = None
            if row["iframe_url"] or steps:
                detail = {
                    "iframe_url": row["iframe_url"] or "",
                    "iframe_title": row["iframe_title"] or "",
                    "steps": steps or [],
                }

            result.append(
                {
                    "id": row["id"],
                    "label": row["label"],
                    "value": row["value"],
                    "image_url": row["image_url"],
                    "sort_order": row["sort_order"],
                    "detail": detail,
                },
            )
        return result

    async def get_case_by_id(self, case_id: int) -> Optional[FeaturedCase]:
        if not self._use_db:
            return None
        row = await self.db.fetch_one(
            "SELECT * FROM swe_featured_case WHERE id = %s",
            (case_id,),
        )
        return self._row_to_case(row) if row else None

    async def get_case_for_scope(
        self,
        case_id: int,
        source_id: str,
        bbk_id: Optional[str],
    ) -> Optional[FeaturedCase]:
        """Return a case only when it belongs to the exact logical scope."""
        if not self._use_db:
            return None
        scope_condition, scope_params = _scope_condition(bbk_id)
        row = await self.db.fetch_one(
            f"""
                SELECT * FROM swe_featured_case
                WHERE id = %s AND source_id = %s AND {scope_condition}
            """,
            tuple([case_id, source_id, *scope_params]),
        )
        return self._row_to_case(row) if row else None

    # ==================== Exact-scope management ====================

    async def list_cases(
        self,
        source_id: str,
        bbk_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FeaturedCase], int]:
        """List one exact logical BBK scope with deterministic pagination."""
        if not self._use_db:
            return [], 0

        scope_condition, scope_params = _scope_condition(bbk_id)
        where_sql = f"source_id = %s AND {scope_condition}"
        where_params = [source_id, *scope_params]

        count_row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS total FROM swe_featured_case WHERE {where_sql}",
            tuple(where_params),
        )
        total = int(count_row["total"]) if count_row else 0

        offset = (page - 1) * page_size
        rows = await self.db.fetch_all(
            f"""
                SELECT * FROM swe_featured_case
                WHERE {where_sql}
                ORDER BY sort_order ASC, id ASC
                LIMIT %s OFFSET %s
            """,
            tuple([*where_params, page_size, offset]),
        )
        return [self._row_to_case(row) for row in rows], total

    # ==================== Transactional queue mutations ====================

    async def create_case(self, case: FeaturedCase) -> FeaturedCase:
        """Append a case and normalize its exact queue atomically."""
        if not self._use_db:
            return case

        scope_bbk_id = normalize_featured_case_bbk_id(case.bbk_id)
        case.bbk_id = scope_bbk_id

        async with self.db.transaction() as conn:
            case_ids = await self._lock_queue(
                conn,
                case.source_id,
                scope_bbk_id,
            )
            await self._persist_queue(
                conn,
                case.source_id,
                scope_bbk_id,
                case_ids,
            )
            steps_json = (
                json.dumps([step.model_dump() for step in case.steps])
                if case.steps
                else None
            )
            created_id = int(
                await conn.insert(
                    """
                    INSERT INTO swe_featured_case
                        (source_id, bbk_id, label, value, image_url,
                         iframe_url, iframe_title, steps, sort_order,
                         is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        case.source_id,
                        scope_bbk_id,
                        case.label,
                        case.value,
                        case.image_url,
                        case.iframe_url,
                        case.iframe_title,
                        steps_json,
                        len(case_ids) + 1,
                        int(case.is_active),
                    ),
                ),
            )
            timestamps = await conn.fetch_one(
                """
                    SELECT created_at, updated_at
                    FROM swe_featured_case
                    WHERE id = %s
                """,
                (created_id,),
            )
            if timestamps is None:
                raise RuntimeError(
                    "Created featured case could not be reloaded",
                )
            created = case.model_copy(
                update={
                    "id": created_id,
                    "sort_order": len(case_ids) + 1,
                    "created_at": timestamps["created_at"],
                    "updated_at": timestamps["updated_at"],
                },
            )
        return created

    async def update_case(
        self,
        case_id: int,
        source_id: str,
        bbk_id: Optional[str],
        label: Optional[str] = None,
        value: Optional[str] = None,
        image_url: Optional[str] = None,
        iframe_url: Optional[str] = None,
        iframe_title: Optional[str] = None,
        steps: Optional[list[CaseStep]] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[FeaturedCase]:
        """Update content without moving the case between ordering scopes."""
        if not self._use_db:
            return None

        updates: list[str] = []
        params: list[Any] = []
        for column, value_to_set in (
            ("label", label),
            ("value", value),
            ("image_url", image_url),
            ("iframe_url", iframe_url),
            ("iframe_title", iframe_title),
        ):
            if value_to_set is not None:
                updates.append(f"{column} = %s")
                params.append(value_to_set)
        if steps is not None:
            updates.append("steps = %s")
            params.append(
                (
                    json.dumps([step.model_dump() for step in steps])
                    if steps
                    else None
                ),
            )
        if is_active is not None:
            updates.append("is_active = %s")
            params.append(int(is_active))

        current = await self.get_case_for_scope(case_id, source_id, bbk_id)
        if current is None:
            return None
        if not updates:
            return current

        scope_condition, scope_params = _scope_condition(bbk_id)
        await self.db.execute(
            f"""
                UPDATE swe_featured_case
                SET {', '.join(updates)}
                WHERE id = %s AND source_id = %s AND {scope_condition}
            """,
            tuple([*params, case_id, source_id, *scope_params]),
        )
        return await self.get_case_for_scope(case_id, source_id, bbk_id)

    async def reorder_case(
        self,
        case_id: int,
        source_id: str,
        bbk_id: Optional[str],
        sort_order: int,
    ) -> Optional[FeaturedCaseReorderResult]:
        """Move a case within its exact queue and normalize positions."""
        if not self._use_db:
            return None

        scope_bbk_id = normalize_featured_case_bbk_id(bbk_id)
        async with self.db.transaction() as conn:
            case_ids = await self._lock_queue(
                conn,
                source_id,
                scope_bbk_id,
            )
            if case_id not in case_ids:
                return None

            case_ids.remove(case_id)
            final_sort_order = min(sort_order, len(case_ids) + 1)
            case_ids.insert(final_sort_order - 1, case_id)
            await self._persist_queue(
                conn,
                source_id,
                scope_bbk_id,
                case_ids,
            )

        return FeaturedCaseReorderResult(
            case_id=case_id,
            sort_order=final_sort_order,
            total=len(case_ids),
        )

    async def delete_case(
        self,
        case_id: int,
        source_id: str,
        bbk_id: Optional[str],
    ) -> bool:
        """Delete a case and compact its exact queue atomically."""
        if not self._use_db:
            return False

        scope_bbk_id = normalize_featured_case_bbk_id(bbk_id)
        async with self.db.transaction() as conn:
            case_ids = await self._lock_queue(
                conn,
                source_id,
                scope_bbk_id,
            )
            if case_id not in case_ids:
                return False

            affected_rows = await conn.execute(
                """
                    DELETE FROM swe_featured_case
                    WHERE id = %s AND source_id = %s
                """,
                (case_id, source_id),
            )
            if affected_rows <= 0:
                return False

            case_ids.remove(case_id)
            await self._persist_queue(
                conn,
                source_id,
                scope_bbk_id,
                case_ids,
            )
        return True

    async def _lock_queue(
        self,
        conn: Any,
        source_id: str,
        bbk_id: Optional[str],
    ) -> list[int]:
        """Lock and return one queue's case IDs in stable current order."""
        scope_condition, scope_params = _scope_condition(bbk_id)
        rows = await conn.fetch_all(
            f"""
                SELECT id
                FROM swe_featured_case
                WHERE source_id = %s AND {scope_condition}
                ORDER BY sort_order ASC, id ASC
                FOR UPDATE
            """,
            tuple([source_id, *scope_params]),
        )
        return [int(row["id"]) for row in rows]

    async def _persist_queue(
        self,
        conn: Any,
        source_id: str,
        bbk_id: Optional[str],
        case_ids: list[int],
    ) -> None:
        """Persist contiguous positions and the canonical BBK value."""
        if not case_ids:
            return
        scope_bbk_id = normalize_featured_case_bbk_id(bbk_id)
        await conn.execute_many(
            """
                UPDATE swe_featured_case
                SET sort_order = %s, bbk_id = %s
                WHERE id = %s AND source_id = %s
            """,
            [
                (position, scope_bbk_id, row_id, source_id)
                for position, row_id in enumerate(case_ids, start=1)
            ],
        )

    def _row_to_case(self, row: dict) -> FeaturedCase:
        steps = None
        if row.get("steps"):
            try:
                steps_data = json.loads(row["steps"])
                steps = [CaseStep(**step) for step in steps_data]
            except (json.JSONDecodeError, TypeError):
                steps = None

        return FeaturedCase(
            id=row["id"],
            source_id=row["source_id"],
            bbk_id=row["bbk_id"],
            label=row["label"],
            value=row["value"],
            image_url=row["image_url"],
            iframe_url=row["iframe_url"],
            iframe_title=row["iframe_title"],
            steps=steps,
            sort_order=row["sort_order"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
