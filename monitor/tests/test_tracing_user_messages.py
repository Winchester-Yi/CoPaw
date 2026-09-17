"""Tests for tracing user-message query behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from monitor.app.services.tracing.query_service import TracingQueryService


class _FakeDb:
    def __init__(self) -> None:
        self.fetch_one_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_all_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch_one(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> dict[str, Any]:
        self.fetch_one_calls.append((query, params))
        return {"total": 0}

    async def fetch_all(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        self.fetch_all_calls.append((query, params))
        return []


def test_user_messages_can_exclude_cron_task_sessions():
    async def _run() -> _FakeDb:
        db = _FakeDb()
        service = TracingQueryService(db)  # type: ignore[arg-type]

        await service.get_user_messages(
            source_id="RMASSIST",
            start_date=datetime(2026, 9, 1),
            end_date=datetime(2026, 9, 2),
            exclude_cron_task_sessions=True,
        )
        return db

    db = asyncio.run(_run())

    count_sql, count_params = db.fetch_one_calls[0]
    list_sql, list_params = db.fetch_all_calls[0]
    assert "session_id NOT LIKE %s" in count_sql
    assert "session_id NOT LIKE %s" in list_sql
    assert "cron-task%" in count_params
    assert "cron-task%" in list_params


def test_user_messages_keeps_cron_task_sessions_by_default():
    async def _run() -> _FakeDb:
        db = _FakeDb()
        service = TracingQueryService(db)  # type: ignore[arg-type]

        await service.get_user_messages(
            source_id="RMASSIST",
            start_date=datetime(2026, 9, 1),
            end_date=datetime(2026, 9, 2),
        )
        return db

    db = asyncio.run(_run())

    count_sql, count_params = db.fetch_one_calls[0]
    list_sql, list_params = db.fetch_all_calls[0]
    assert "session_id NOT LIKE %s" not in count_sql
    assert "session_id NOT LIKE %s" not in list_sql
    assert "cron-task%" not in count_params
    assert "cron-task%" not in list_params
