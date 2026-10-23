# -*- coding: utf-8 -*-
"""Unit and route tests for featured-case management and ordering."""

import asyncio
import json
from copy import deepcopy
from datetime import datetime
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.app.featured_case import featured_case_router
from swe.app.featured_case import featured_case_router as exported_router
from swe.app.featured_case.models import (
    CaseStep,
    FeaturedCase,
    FeaturedCaseCreate,
    FeaturedCaseReorderRequest,
    FeaturedCaseReorderResult,
    FeaturedCaseUpdate,
)
from swe.app.featured_case.service import FeaturedCaseService
from swe.app.featured_case.store import (
    FeaturedCaseStore,
    normalize_featured_case_bbk_id,
)

router_module = import_module("swe.app.featured_case.router")


def _case_row(
    row_id: int,
    *,
    source_id: str = "source1",
    bbk_id: str | None = "branch-a",
    sort_order: int = 1,
    is_active: int = 1,
    label: str | None = None,
) -> dict:
    return {
        "id": row_id,
        "source_id": source_id,
        "bbk_id": bbk_id,
        "label": label or f"案例{row_id}",
        "value": f"内容{row_id}",
        "image_url": None,
        "iframe_url": None,
        "iframe_title": None,
        "steps": None,
        "sort_order": sort_order,
        "is_active": is_active,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }


def _matches_scope(row: dict, scope: str) -> bool:
    if scope == "100":
        return row["bbk_id"] is None or str(row["bbk_id"]).strip() in {
            "",
            "100",
        }
    return row["bbk_id"] == scope


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection"):
        self.connection = connection
        self.rows: list[tuple] = []
        self.lastrowid = 0
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, query: str, params: tuple | None = None):
        params = params or ()
        compact_query = " ".join(query.split())
        database = self.connection.database

        if compact_query.startswith("SELECT id FROM swe_featured_case"):
            assert compact_query.endswith("FOR UPDATE")
            source_id = str(params[0])
            scope = (
                "100" if "bbk_id IS NULL" in compact_query else str(params[1])
            )
            await database.acquire_queue_lock(
                self.connection,
                (source_id, scope),
            )
            matching = [
                row
                for row in database.rows
                if row["source_id"] == source_id and _matches_scope(row, scope)
            ]
            matching.sort(key=lambda row: (row["sort_order"], row["id"]))
            self.rows = [(row["id"],) for row in matching]
            self.rowcount = len(self.rows)
            return self.rowcount

        if compact_query.startswith("INSERT INTO swe_featured_case"):
            next_id = max((row["id"] for row in database.rows), default=0) + 1
            (
                source_id,
                bbk_id,
                label,
                value,
                image_url,
                iframe_url,
                iframe_title,
                steps,
                sort_order,
                is_active,
            ) = params
            database.rows.append(
                {
                    **_case_row(
                        next_id,
                        source_id=source_id,
                        bbk_id=bbk_id,
                        sort_order=sort_order,
                        is_active=is_active,
                        label=label,
                    ),
                    "value": value,
                    "image_url": image_url,
                    "iframe_url": iframe_url,
                    "iframe_title": iframe_title,
                    "steps": steps,
                },
            )
            self.lastrowid = next_id
            self.rowcount = 1
            return 1

        if compact_query.startswith(
            "SELECT created_at, updated_at FROM swe_featured_case",
        ):
            if database.fail_on_create_reload:
                raise RuntimeError("simulated create reload failure")
            case_id = int(params[0])
            row = next(
                (row for row in database.rows if row["id"] == case_id),
                None,
            )
            self.rows = (
                [(row["created_at"], row["updated_at"])]
                if row is not None
                else []
            )
            self.rowcount = len(self.rows)
            return self.rowcount

        if compact_query.startswith("DELETE FROM swe_featured_case"):
            case_id, source_id = params
            previous_length = len(database.rows)
            database.rows[:] = [
                row
                for row in database.rows
                if not (row["id"] == case_id and row["source_id"] == source_id)
            ]
            self.rowcount = previous_length - len(database.rows)
            return self.rowcount

        raise AssertionError(f"Unexpected transactional SQL: {compact_query}")

    async def executemany(self, query: str, params_list: list[tuple]):
        if self.connection.database.fail_on_persist:
            raise RuntimeError("simulated queue persistence failure")
        compact_query = " ".join(query.split())
        assert compact_query.startswith(
            "UPDATE swe_featured_case SET sort_order",
        )
        for sort_order, bbk_id, case_id, source_id in params_list:
            row = next(
                row
                for row in self.connection.database.rows
                if row["id"] == case_id and row["source_id"] == source_id
            )
            row["sort_order"] = sort_order
            row["bbk_id"] = bbk_id
        self.rowcount = len(params_list)
        return self.rowcount

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakeConnection:
    def __init__(self, database: "_TransactionDatabase"):
        self.database = database
        self.snapshot: list[dict] | None = None
        self.queue_locks: list[asyncio.Lock] = []

    async def begin(self):
        self.snapshot = deepcopy(self.database.rows)

    async def commit(self):
        self.snapshot = None
        self._release_queue_locks()

    async def rollback(self):
        if self.snapshot is not None:
            self.database.rows[:] = deepcopy(self.snapshot)
        self.snapshot = None
        self._release_queue_locks()

    def _release_queue_locks(self):
        for lock in self.queue_locks:
            lock.release()
        self.queue_locks.clear()

    async def execute(self, query, params=None):
        cursor = _FakeCursor(self)
        await cursor.execute(query, params)
        return cursor.rowcount

    async def insert(self, query, params=None):
        cursor = _FakeCursor(self)
        await cursor.execute(query, params)
        return cursor.lastrowid

    async def fetch_all(self, query, params=None):
        cursor = _FakeCursor(self)
        await cursor.execute(query, params)
        return [{"id": row[0]} for row in await cursor.fetchall()]

    async def fetch_one(self, query, params=None):
        cursor = _FakeCursor(self)
        await cursor.execute(query, params)
        row = await cursor.fetchone()
        return {"created_at": row[0], "updated_at": row[1]} if row else None

    async def execute_many(self, query, params_list):
        return await _FakeCursor(self).executemany(query, params_list)


class _AcquireConnection:
    def __init__(self, database: "_TransactionDatabase"):
        self.connection = _FakeConnection(database)

    async def __aenter__(self):
        await self.connection.begin()
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            await self.connection.commit()
        else:
            await self.connection.rollback()
        return False


class _TransactionDatabase:
    def __init__(self, rows: list[dict]):
        self.rows = deepcopy(rows)
        self.is_connected = True
        self.fail_on_persist = False
        self.fail_on_create_reload = False
        self.queue_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.queue_lock_attempts: dict[tuple[str, str], int] = {}
        self.second_same_queue_lock_attempted = asyncio.Event()

    async def acquire_queue_lock(
        self,
        connection: _FakeConnection,
        key: tuple[str, str],
    ) -> None:
        attempts = self.queue_lock_attempts.get(key, 0) + 1
        self.queue_lock_attempts[key] = attempts
        if attempts == 2:
            self.second_same_queue_lock_attempted.set()
        lock = self.queue_locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        connection.queue_locks.append(lock)

    def transaction(self):
        return _AcquireConnection(self)

    async def fetch_one(self, query: str, params: tuple | None = None):
        params = params or ()
        compact_query = " ".join(query.split())
        if "WHERE id = %s" in compact_query:
            case_id = int(params[0])
            row = next(
                (row for row in self.rows if row["id"] == case_id),
                None,
            )
            if row is None:
                return None
            if "source_id = %s" in compact_query:
                source_id = str(params[1])
                scope = (
                    "100"
                    if "bbk_id IS NULL" in compact_query
                    else str(params[2])
                )
                if row["source_id"] != source_id or not _matches_scope(
                    row,
                    scope,
                ):
                    return None
            return deepcopy(row)
        raise AssertionError(f"Unexpected fetch_one SQL: {compact_query}")

    async def fetch_all(self, query: str, params: tuple | None = None):
        params = params or ()
        compact_query = " ".join(query.split())
        if "FROM swe_featured_case" not in compact_query:
            raise AssertionError(f"Unexpected fetch_all SQL: {compact_query}")

        source_id = str(params[0])
        branch_scope = (
            str(params[1])
            if "CASE WHEN bbk_id = %s" in compact_query
            else None
        )
        matching = [
            row
            for row in self.rows
            if row["source_id"] == source_id
            and row["is_active"] == 1
            and (
                _matches_scope(row, "100")
                if branch_scope is None
                else row["bbk_id"] == branch_scope
                or _matches_scope(row, "100")
            )
        ]
        matching.sort(
            key=lambda row: (
                (
                    0
                    if branch_scope is not None
                    and row["bbk_id"] == branch_scope
                    else 1
                ),
                row["sort_order"],
                row["id"],
            ),
        )
        return [deepcopy(row) for row in matching]


class TestModels:
    def test_case_step_and_featured_case(self):
        step = CaseStep(title="步骤1", content="内容1")
        case = FeaturedCase(
            id=1,
            source_id="source1",
            label="案例",
            value="内容",
            steps=[step],
        )
        assert case.steps == [step]

    def test_create_supports_initial_active_state(self):
        request = FeaturedCaseCreate(
            label="案例",
            value="内容",
            is_active=False,
        )
        assert request.is_active is False

    def test_reorder_requires_a_strict_positive_integer(self):
        assert FeaturedCaseReorderRequest(sort_order=2).sort_order == 2
        for invalid in (0, -1, 2.5, "2"):
            with pytest.raises(ValueError):
                FeaturedCaseReorderRequest(sort_order=invalid)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "100"),
            ("", "100"),
            ("  ", "100"),
            ("100", "100"),
            ("200", "200"),
        ],
    )
    def test_bbk_scope_normalization(self, raw, expected):
        assert normalize_featured_case_bbk_id(raw) == expected


class TestFeaturedCaseStoreWithoutDatabase:
    @pytest.fixture
    def store(self):
        return FeaturedCaseStore(db=None)

    @pytest.mark.asyncio
    async def test_no_database_returns_safe_empty_results(self, store):
        assert await store.get_cases_for_dimension("source1", "branch-a") == []
        assert await store.get_case_by_id(1) is None
        assert await store.list_cases("source1", "branch-a") == ([], 0)
        assert (
            await store.update_case(1, "source1", "branch-a", label="新")
            is None
        )
        assert await store.reorder_case(1, "source1", "branch-a", 2) is None
        assert await store.delete_case(1, "source1", "branch-a") is False


class TestFeaturedCaseQueries:
    @pytest.fixture
    def mock_db(self):
        database = MagicMock()
        database.is_connected = True
        database.fetch_one = AsyncMock()
        database.fetch_all = AsyncMock()
        database.execute = AsyncMock(return_value=1)
        return database

    @pytest.fixture
    def store(self, mock_db):
        return FeaturedCaseStore(mock_db)

    @pytest.mark.asyncio
    async def test_head_office_runtime_query_is_exact_and_active(
        self,
        store,
        mock_db,
    ):
        mock_db.fetch_all.return_value = []
        await store.get_cases_for_dimension("source1", None)
        query, params = mock_db.fetch_all.call_args.args
        assert "bbk_id IS NULL" in query
        assert "is_active = 1" in query
        assert "bbk_id = %s OR" not in query
        assert params == ("source1", "100")

    @pytest.mark.asyncio
    async def test_branch_runtime_query_keeps_branch_before_head_office(
        self,
        store,
        mock_db,
    ):
        mock_db.fetch_all.return_value = []
        await store.get_cases_for_dimension("source1", "branch-a")
        query, params = mock_db.fetch_all.call_args.args
        assert "CASE WHEN bbk_id = %s THEN 0 ELSE 1 END" in query
        assert params == ("source1", "branch-a", "100", "branch-a")

    @pytest.mark.asyncio
    async def test_runtime_response_parses_detail(self, store, mock_db):
        mock_db.fetch_all.return_value = [
            {
                "id": 1,
                "label": "案例",
                "value": "内容",
                "image_url": None,
                "iframe_url": "https://example.com",
                "iframe_title": "详情",
                "steps": json.dumps([{"title": "步骤1", "content": "内容1"}]),
                "sort_order": 1,
            },
        ]
        result = await store.get_cases_for_dimension("source1", "branch-a")
        assert result[0]["detail"]["steps"][0]["title"] == "步骤1"

    @pytest.mark.asyncio
    async def test_management_list_uses_exact_scope_and_stable_order(
        self,
        store,
        mock_db,
    ):
        mock_db.fetch_one.return_value = {"total": 0}
        mock_db.fetch_all.return_value = []
        await store.list_cases("source1", "branch-a", page=2, page_size=10)
        query, params = mock_db.fetch_all.call_args.args
        assert "bbk_id = %s" in query
        assert "OR bbk_id" not in query
        assert "ORDER BY sort_order ASC, id ASC" in query
        assert params == ("source1", "branch-a", 10, 10)


class TestTransactionalOrdering:
    @staticmethod
    def _store(
        rows: list[dict],
    ) -> tuple[FeaturedCaseStore, _TransactionDatabase]:
        database = _TransactionDatabase(rows)
        return FeaturedCaseStore(database), database

    @pytest.mark.asyncio
    async def test_move_up_preserves_other_relative_order_and_branch_isolation(
        self,
    ):
        store, database = self._store(
            [
                *[_case_row(index, sort_order=index) for index in range(1, 5)],
                _case_row(10, bbk_id="100", sort_order=1),
            ],
        )
        result = await store.reorder_case(4, "source1", "branch-a", 2)
        assert result == FeaturedCaseReorderResult(
            case_id=4,
            sort_order=2,
            total=4,
        )
        branch_rows = sorted(
            (row for row in database.rows if row["bbk_id"] == "branch-a"),
            key=lambda row: row["sort_order"],
        )
        assert [(row["id"], row["sort_order"]) for row in branch_rows] == [
            (1, 1),
            (4, 2),
            (2, 3),
            (3, 4),
        ]
        assert (
            next(row for row in database.rows if row["id"] == 10)["sort_order"]
            == 1
        )

    @pytest.mark.asyncio
    async def test_move_to_end_clamps_to_queue_size(self):
        store, database = self._store(
            [_case_row(index, sort_order=index) for index in range(1, 5)],
        )
        result = await store.reorder_case(2, "source1", "branch-a", 30)
        assert result and result.sort_order == 4
        ordered_ids = [
            row["id"]
            for row in sorted(database.rows, key=lambda row: row["sort_order"])
        ]
        assert ordered_ids == [1, 3, 4, 2]

    @pytest.mark.asyncio
    async def test_reorder_repairs_gaps_duplicates_and_includes_inactive_cases(
        self,
    ):
        store, database = self._store(
            [
                _case_row(1, sort_order=1),
                _case_row(2, sort_order=1, is_active=0),
                _case_row(3, sort_order=8),
            ],
        )
        await store.reorder_case(3, "source1", "branch-a", 2)
        ordered = sorted(database.rows, key=lambda row: row["sort_order"])
        assert [(row["id"], row["sort_order"]) for row in ordered] == [
            (1, 1),
            (3, 2),
            (2, 3),
        ]

    @pytest.mark.asyncio
    async def test_head_office_mutation_canonicalizes_legacy_values(self):
        store, database = self._store(
            [
                _case_row(1, bbk_id=None, sort_order=4),
                _case_row(2, bbk_id="", sort_order=1),
                _case_row(3, bbk_id="100", sort_order=2),
            ],
        )
        await store.reorder_case(1, "source1", None, 1)
        assert [
            row["sort_order"]
            for row in sorted(database.rows, key=lambda row: row["id"])
        ] == [1, 2, 3]
        assert {row["bbk_id"] for row in database.rows} == {"100"}

    @pytest.mark.asyncio
    async def test_create_appends_and_returns_persisted_id(self):
        store, database = self._store(
            [_case_row(1, sort_order=3), _case_row(2, sort_order=8)],
        )
        created = await store.create_case(
            FeaturedCase(
                source_id="source1",
                bbk_id="branch-a",
                label="新案例",
                value="新内容",
                is_active=False,
            ),
        )
        assert created.id == 3
        assert created.sort_order == 3
        assert created.is_active is False
        assert [row["sort_order"] for row in database.rows] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_create_reload_failure_rolls_back_insert_and_normalization(
        self,
    ):
        original = [_case_row(1, sort_order=4), _case_row(2, sort_order=8)]
        store, database = self._store(original)
        database.fail_on_create_reload = True

        with pytest.raises(RuntimeError, match="create reload failure"):
            await store.create_case(
                FeaturedCase(
                    source_id="source1",
                    bbk_id="branch-a",
                    label="新案例",
                    value="新内容",
                ),
            )

        assert database.rows == original

    @pytest.mark.asyncio
    async def test_delete_compacts_only_the_exact_queue(self):
        store, database = self._store(
            [
                *[_case_row(index, sort_order=index) for index in range(1, 5)],
                _case_row(10, bbk_id="branch-b", sort_order=7),
            ],
        )
        assert await store.delete_case(2, "source1", "branch-a") is True
        branch_rows = [
            row for row in database.rows if row["bbk_id"] == "branch-a"
        ]
        assert [(row["id"], row["sort_order"]) for row in branch_rows] == [
            (1, 1),
            (3, 2),
            (4, 3),
        ]
        assert (
            next(row for row in database.rows if row["id"] == 10)["sort_order"]
            == 7
        )

    @pytest.mark.asyncio
    async def test_failed_persistence_rolls_back_entire_queue(self):
        original = [
            _case_row(index, sort_order=index) for index in range(1, 4)
        ]
        store, database = self._store(original)
        database.fail_on_persist = True
        with pytest.raises(RuntimeError, match="persistence failure"):
            await store.reorder_case(3, "source1", "branch-a", 1)
        assert database.rows == original

    @pytest.mark.asyncio
    async def test_later_reorder_uses_latest_committed_order(self):
        store, database = self._store(
            [_case_row(index, sort_order=index) for index in range(1, 5)],
        )
        first_persist_started = asyncio.Event()
        release_first_persist = asyncio.Event()
        original_persist_queue = store._persist_queue
        persist_calls = 0

        async def gated_persist_queue(*args, **kwargs):
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                first_persist_started.set()
                await release_first_persist.wait()
            await original_persist_queue(*args, **kwargs)

        store._persist_queue = gated_persist_queue
        first = asyncio.create_task(
            store.reorder_case(4, "source1", "branch-a", 2),
        )
        await asyncio.wait_for(first_persist_started.wait(), timeout=1)

        second = asyncio.create_task(
            store.reorder_case(2, "source1", "branch-a", 2),
        )
        await asyncio.wait_for(
            database.second_same_queue_lock_attempted.wait(),
            timeout=1,
        )
        assert not second.done()

        release_first_persist.set()
        await asyncio.gather(first, second)
        ordered_ids = [
            row["id"]
            for row in sorted(database.rows, key=lambda row: row["sort_order"])
        ]
        assert ordered_ids == [1, 2, 4, 3]

    @pytest.mark.asyncio
    async def test_different_branch_reorders_do_not_share_a_queue_lock(self):
        store, database = self._store(
            [
                _case_row(1, bbk_id="branch-a", sort_order=1),
                _case_row(2, bbk_id="branch-a", sort_order=2),
                _case_row(10, bbk_id="branch-b", sort_order=1),
                _case_row(11, bbk_id="branch-b", sort_order=2),
            ],
        )
        branch_a_persist_started = asyncio.Event()
        release_branch_a = asyncio.Event()
        original_persist_queue = store._persist_queue

        async def gated_persist_queue(conn, source_id, bbk_id, case_ids):
            if bbk_id == "branch-a":
                branch_a_persist_started.set()
                await release_branch_a.wait()
            await original_persist_queue(conn, source_id, bbk_id, case_ids)

        store._persist_queue = gated_persist_queue
        branch_a = asyncio.create_task(
            store.reorder_case(2, "source1", "branch-a", 1),
        )
        await asyncio.wait_for(branch_a_persist_started.wait(), timeout=1)
        branch_b = asyncio.create_task(
            store.reorder_case(11, "source1", "branch-b", 1),
        )

        try:
            done, _ = await asyncio.wait({branch_b}, timeout=1)
            assert branch_b in done
        finally:
            release_branch_a.set()
            await branch_a

        assert [
            row["id"]
            for row in sorted(
                (row for row in database.rows if row["bbk_id"] == "branch-a"),
                key=lambda row: row["sort_order"],
            )
        ] == [2, 1]
        assert [
            row["id"]
            for row in sorted(
                (row for row in database.rows if row["bbk_id"] == "branch-b"),
                key=lambda row: row["sort_order"],
            )
        ] == [11, 10]

    @pytest.mark.asyncio
    async def test_management_reorder_is_reflected_in_branch_first_runtime_order(
        self,
    ):
        store, _ = self._store(
            [
                _case_row(1, bbk_id="branch-a", sort_order=1),
                _case_row(2, bbk_id="branch-a", sort_order=2),
                _case_row(3, bbk_id="branch-a", sort_order=3, is_active=0),
                _case_row(10, bbk_id="100", sort_order=1),
                _case_row(11, bbk_id="100", sort_order=2),
            ],
        )

        await store.reorder_case(2, "source1", "branch-a", 1)
        await store.reorder_case(11, "source1", "100", 1)
        displayed = await store.get_cases_for_dimension("source1", "branch-a")

        assert [case["id"] for case in displayed] == [2, 1, 11, 10]


class TestFeaturedCaseService:
    @pytest.fixture
    def mock_store(self):
        store = MagicMock(spec=FeaturedCaseStore)
        store.get_cases_for_dimension = AsyncMock(return_value=[])
        store.get_case_by_id = AsyncMock()
        store.list_cases = AsyncMock(return_value=([], 0))
        store.create_case = AsyncMock()
        store.update_case = AsyncMock()
        store.reorder_case = AsyncMock()
        store.delete_case = AsyncMock()
        return store

    @pytest.fixture
    def service(self, mock_store):
        return FeaturedCaseService(mock_store)

    @pytest.mark.asyncio
    async def test_create_uses_caller_scope_and_active_state(
        self,
        service,
        mock_store,
    ):
        mock_store.create_case.side_effect = lambda case: case
        result = await service.create_case(
            "source1",
            None,
            FeaturedCaseCreate(label="案例", value="内容", is_active=False),
        )
        assert result.bbk_id == "100"
        assert result.is_active is False

    @pytest.mark.asyncio
    async def test_update_forwards_scope_and_rejects_missing_case(
        self,
        service,
        mock_store,
    ):
        mock_store.update_case.return_value = None
        with pytest.raises(ValueError, match="无权"):
            await service.update_case(
                1,
                "source1",
                "branch-a",
                FeaturedCaseUpdate(label="新"),
            )
        assert (
            mock_store.update_case.call_args.kwargs["source_id"] == "source1"
        )
        assert mock_store.update_case.call_args.kwargs["bbk_id"] == "branch-a"

    @pytest.mark.asyncio
    async def test_reorder_and_delete_forward_exact_scope(
        self,
        service,
        mock_store,
    ):
        mock_store.reorder_case.return_value = FeaturedCaseReorderResult(
            case_id=1,
            sort_order=2,
            total=3,
        )
        result = await service.reorder_case(1, "source1", "branch-a", 2)
        assert result.sort_order == 2
        mock_store.delete_case.return_value = True
        await service.delete_case(1, "source1", "branch-a")
        mock_store.delete_case.assert_awaited_once_with(
            1,
            "source1",
            "branch-a",
        )


class TestFeaturedCaseRouter:
    @pytest.fixture
    def service(self, monkeypatch):
        service = MagicMock(spec=FeaturedCaseService)
        service.list_cases = AsyncMock(return_value=([], 0))
        service.create_case = AsyncMock()
        service.update_case = AsyncMock()
        service.reorder_case = AsyncMock()
        service.delete_case = AsyncMock()
        monkeypatch.setattr(router_module, "_service", service)
        return service

    @pytest.fixture
    def client(self, service):
        assert featured_case_router is exported_router
        app = FastAPI()
        app.include_router(featured_case_router)
        return TestClient(app)

    def test_branch_can_read_exact_head_office_management_scope(
        self,
        client,
        service,
    ):
        response = client.get(
            "/featured-cases/admin/cases?bbk_id=100&page=1&page_size=20",
            headers={"X-Source-Id": "source1", "X-Bbk-Id": "branch-a"},
        )
        assert response.status_code == 200
        assert service.list_cases.call_args.kwargs["bbk_id"] == "100"

    def test_reorder_returns_final_position_and_scope(self, client, service):
        service.reorder_case.return_value = FeaturedCaseReorderResult(
            case_id=7,
            sort_order=2,
            total=20,
        )
        response = client.put(
            "/featured-cases/admin/cases/7/order",
            json={"sort_order": 2},
            headers={"X-Source-Id": "source1", "X-Bbk-Id": "branch-a"},
        )
        assert response.status_code == 200
        assert response.json()["data"] == {
            "case_id": 7,
            "sort_order": 2,
            "total": 20,
        }
        service.reorder_case.assert_awaited_once_with(
            7,
            "source1",
            "branch-a",
            2,
        )

    @pytest.mark.parametrize("invalid", [0, -1, 2.5, "2"])
    def test_reorder_rejects_invalid_values_without_calling_service(
        self,
        client,
        service,
        invalid,
    ):
        response = client.put(
            "/featured-cases/admin/cases/7/order",
            json={"sort_order": invalid},
            headers={"X-Source-Id": "source1", "X-Bbk-Id": "branch-a"},
        )
        assert response.status_code == 422
        service.reorder_case.assert_not_awaited()

    def test_scope_mismatch_is_non_revealing_not_found(self, client, service):
        service.reorder_case.side_effect = ValueError("案例不存在或无权操作")
        response = client.put(
            "/featured-cases/admin/cases/7/order",
            json={"sort_order": 1},
            headers={"X-Source-Id": "source1", "X-Bbk-Id": "branch-a"},
        )
        assert response.status_code == 404

    def test_create_returns_persisted_database_id(self, client, service):
        service.create_case.return_value = FeaturedCase(
            id=88,
            source_id="source1",
            bbk_id="100",
            label="案例",
            value="内容",
            sort_order=1,
        )
        response = client.post(
            "/featured-cases/admin/cases",
            json={"label": "案例", "value": "内容"},
            headers={"X-Source-Id": "source1"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["id"] == 88
        assert service.create_case.call_args.args[1] == "100"
