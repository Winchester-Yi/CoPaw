# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
import threading

import pytest

from swe.app.conversation_storage.errors import ConflictError, IntegrityError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.retention import RetentionStore


class RetentionDatabase:
    max_allowed_packet = 1024 * 1024

    @asynccontextmanager
    async def acquire(self):
        yield self

    def __init__(self):
        self.pins = []
        self.depth = 0
        self.objects = {
            value: {
                "service_unit_key": b"schema-test-service",
                "id": value,
                "tenant_key": b"t",
                "source_key": b"s",
                "agent_key": b"a",
                "scope_kind": "workspace",
                "state": "committed",
                "gc_state": "live",
            }
            for value in range(1, 5)
        }
        self.queries = []

    @property
    def _in_transaction(self):
        return self.depth > 0

    async def insert_prepared(self, template, rows, **kwargs):
        count = 0
        for row in rows:
            await self.insert(
                template.prefix + template.row_template + template.suffix,
                row,
            )
            count += 1
        return count

    @asynccontextmanager
    async def transaction(self):
        self.depth += 1
        before = deepcopy(self.pins)
        try:
            yield self
        except BaseException:
            self.pins = before
            raise
        finally:
            self.depth -= 1

    async def insert(self, query, params):
        assert query.count("%s") == len(params)
        self.queries.append(query)
        fields = query.split("(", 1)[1].split(")", 1)[0].split(",")
        row = dict(zip((field.strip() for field in fields), params))
        if row.get("guard_marker") == 1:
            existing = next(
                (
                    item
                    for item in self.pins
                    if item.get("guard_marker") == 1
                    and item["service_unit_key"] == row["service_unit_key"]
                ),
                None,
            )
        else:
            existing = next(
                (
                    item
                    for item in self.pins
                    if (
                        item["service_unit_key"],
                        item["retention_key"],
                        item["member_key"],
                    )
                    == (
                        row["service_unit_key"],
                        row["retention_key"],
                        row["member_key"],
                    )
                ),
                None,
            )
        if existing:
            return existing["id"]
        row.update(id=len(self.pins) + 1, state="active")
        self.pins.append(row)
        return row["id"]

    async def fetch_one(self, query, params=None):
        if "UTC_TIMESTAMP(6) AS created_at" in query:
            return {"created_at": "database-time"}
        assert query.count("%s") == len(params)
        self.queries.append(query)
        if "guard_marker=1" in query:
            return next(
                (
                    row
                    for row in self.pins
                    if row.get("guard_marker") == 1
                    and row["service_unit_key"] == params[1]
                ),
                None,
            )
        if "retention_key=%s" in query:
            return next(
                (
                    deepcopy(row)
                    for row in self.pins
                    if row["retention_key"] == params[0]
                    and row["service_unit_key"] == params[1]
                ),
                None,
            )
        return None

    async def fetch_all(self, query, params):
        assert query.count("%s") == len(params)
        self.queries.append(query)
        if "swe_conversation_publications" in query:
            if "service_unit_key=%s AND id IN (" in query:
                service, ids = params[0], params[1:]
                columns = query[7:].split(" FROM ")[0].split(",")
                return [
                    {
                        key: deepcopy(self.objects[value].get(key))
                        for key in columns
                    }
                    for value in sorted(set(ids))
                    if value in self.objects
                    and self.objects[value]["service_unit_key"] == service
                ]
            return [
                deepcopy(self.objects[value])
                for value in sorted(set(params[:-1]))
                if value in self.objects
                and self.objects[value]["service_unit_key"] == params[-1]
            ]
        if "retention_key=%s" in query:
            rows = sorted(
                [
                    deepcopy(row)
                    for row in self.pins
                    if row["retention_key"] == params[0]
                    and row["service_unit_key"] == params[1]
                    and (
                        "member_key>%s" not in query
                        or row["member_key"] > params[2]
                    )
                ],
                key=lambda row: row["member_key"],
            )
            return rows[: params[-1]] if "LIMIT %s" in query else rows
        if "kind='backup_fence'" in query:
            return [
                deepcopy(row)
                for row in self.pins
                if row["kind"] == "backup_fence"
                and row["state"] == "active"
                and row["service_unit_key"] == params[1]
            ]
        return [deepcopy(row) for row in self.pins if row["state"] == "active"]

    async def execute(self, query, params):
        assert query.count("%s") == len(params)
        self.queries.append(query)
        count = 0
        for row in self.pins:
            if "kind IN ('reader','backup')" in query and row["kind"] not in (
                "reader",
                "backup",
            ):
                continue
            if (
                row["retention_key"] == params[0]
                and row["kind"] != "gc_guard"
                and row["service_unit_key"] == params[2]
            ):
                row["state"] = "released"
                count += 1
        return count


def store(database):
    return RetentionStore(
        database,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
    )


async def test_guard_initialization_is_unique_and_has_no_publication():
    db = RetentionDatabase()
    service = store(db)
    first = await service.ensure_guard()
    second = await service.ensure_guard()
    assert first["id"] == second["id"]
    assert len(db.pins) == 1
    assert first["publication_id"] is None
    assert first["guard_marker"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "reader"),
        ("state", "released"),
        ("member_key", 1),
        ("publication_id", 1),
    ],
)
async def test_malformed_scope_guard_blocks_gc_and_backup(field, value):
    db = RetentionDatabase()
    service = store(db)
    await service.ensure_guard()
    db.pins[0][field] = value
    with pytest.raises(IntegrityError, match="guard"):
        await service.ensure_guard()


async def test_reader_batch_is_deduplicated_sorted_and_explicitly_released():
    db = RetentionDatabase()
    service = store(db)
    async with service.reader([3, 1, 3]) as batch:
        assert [row["publication_id"] for row in db.pins] == [1, 3]
        assert [row["id"] for row in batch.publications] == [1, 3]
        assert all(row["state"] == "active" for row in db.pins)
        assert all(row["guard_marker"] is None for row in db.pins)
    assert all(row["state"] == "released" for row in db.pins)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "prepared"),
        ("state", "aborted"),
        ("gc_state", "deleting"),
        ("gc_state", "reclaimed"),
    ],
)
async def test_cannot_pin_uncommitted_or_deleting_publications(field, value):
    db = RetentionDatabase()
    db.objects[2][field] = value
    with pytest.raises(ConflictError):
        await store(db).pin([1, 2], kind="reader")
    assert not db.pins


async def test_scope_mismatch_and_missing_object_fail_without_partial_pins():
    db = RetentionDatabase()
    db.objects[2]["source_key"] = b"other"
    with pytest.raises(IntegrityError):
        await store(db).pin([1, 2], kind="reader")
    with pytest.raises(IntegrityError):
        await store(db).pin([99], kind="reader")
    assert not db.pins


async def test_reader_releases_pin_after_content_read_exception():
    db = RetentionDatabase()
    with pytest.raises(OSError):
        async with store(db).reader([1]):
            raise OSError("NAS unavailable")
    assert db.pins[0]["state"] == "released"


async def test_reader_pin_release_stays_in_transaction_owner_task():
    class TaskOwnedRetentionDatabase(RetentionDatabase):
        def __init__(self):
            super().__init__()
            self.owner = None

        @asynccontextmanager
        async def transaction(self):
            if self.depth == 0:
                self.owner = asyncio.current_task()
            async with super().transaction():
                yield self

        async def execute(self, query, params):
            if self.depth and asyncio.current_task() is not self.owner:
                raise RuntimeError(
                    "database session belongs to a different task",
                )
            return await super().execute(query, params)

    db = TaskOwnedRetentionDatabase()
    service = store(db)
    async with db.transaction():
        batch = await service.pin([1], kind="reader")
        await service.finish_reader(batch)
    assert db.pins[0]["state"] == "released"


async def test_dependency_must_point_to_older_committed_publication():
    db = RetentionDatabase()
    db.objects[3]["state"] = "prepared"
    service = store(db)
    await service.add_dependencies(3, [2, 1, 2])
    assert [row["publication_id"] for row in db.pins] == [1, 2]
    assert {row["retention_key"] for row in db.pins} == {b"dep:3"}
    with pytest.raises(IntegrityError, match="older"):
        await service.add_dependencies(3, [3])
    with pytest.raises(ConflictError):
        await service.release_dependencies(3)
    db.objects[3]["gc_state"] = "reclaimed"
    await service.release_dependencies(3)
    assert all(row["state"] == "released" for row in db.pins)


def test_legacy_share_scope_is_distinct_and_cannot_guess_workspace():
    db = RetentionDatabase()
    RetentionStore(
        db,
        WorkspaceScope("t", "", "", service_unit_id="schema-test-service"),
        scope_kind="workspace",
    )
    RetentionStore(
        db,
        WorkspaceScope("t", "", "", service_unit_id="schema-test-service"),
        scope_kind="legacy_share",
    )
    with pytest.raises(ValueError):
        RetentionStore(
            db,
            WorkspaceScope(
                "t",
                "s",
                "",
                service_unit_id="schema-test-service",
            ),
            scope_kind="legacy_share",
        )


async def test_released_pin_key_cannot_be_reused_or_silently_claim_protection():
    db = RetentionDatabase()
    service = store(db)
    batch = await service.pin(
        [1],
        kind="reader",
        retention_key="original-reader",
    )
    await service.release(batch.retention_key)
    with pytest.raises(ConflictError, match="retention"):
        await service.pin([1], kind="reader", retention_key="original-reader")
    assert db.pins[0]["state"] == "released"


async def test_reader_cancellation_waits_for_nas_worker_before_releasing_pin():
    db = RetentionDatabase()
    started = threading.Event()
    finish = threading.Event()

    def slow_read():
        started.set()
        finish.wait(5)
        return b"content"

    async def read():
        async with store(db).reader([1]) as batch:
            await batch.read(slow_read)

    task = asyncio.create_task(read())
    try:
        for _ in range(100):
            if started.is_set() or task.done():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert db.pins[0]["state"] == "active"
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
    assert db.pins[0]["state"] == "released"


async def test_released_reader_batch_cannot_start_new_nas_io():
    db = RetentionDatabase()
    async with store(db).reader([1]) as batch:
        assert await batch.read(lambda: b"valid") == b"valid"
    with pytest.raises(ConflictError, match="closed"):
        await batch.read(lambda: b"unprotected")


async def test_write_gate_ignores_reader_pins_and_checks_fence_under_guard():
    db = RetentionDatabase()
    service = store(db)
    await service.pin([1], kind="reader")
    async with db.transaction():
        await service.assert_writes_allowed()
    guard_lock = next(
        index
        for index, query in enumerate(db.queries)
        if "guard_marker=1 FOR UPDATE" in query
    )
    fence_lock = next(
        index
        for index, query in enumerate(db.queries)
        if "kind='backup_fence'" in query
    )
    assert guard_lock < fence_lock


async def test_backup_fence_is_idempotent_and_only_explicit_release_allows_writes():
    db = RetentionDatabase()
    service = store(db)
    first = await service.install_backup_fence(
        "backup-job",
        "snapshot-1",
    )
    second = await service.install_backup_fence(
        "backup-job",
        "snapshot-1",
    )
    assert first["id"] == second["id"]
    assert first["publication_id"] is None
    assert first["guard_marker"] is None
    assert first["member_key"] == 0
    with pytest.raises(ConflictError, match="backup fence"):
        async with db.transaction():
            await service.assert_writes_allowed()
    await service.release(b"backup-job")
    with pytest.raises(ConflictError, match="backup fence"):
        async with db.transaction():
            await service.assert_writes_allowed()
    await service.release_backup_fence(
        "backup-job",
        "snapshot-1",
    )
    async with db.transaction():
        await service.assert_writes_allowed()
    with pytest.raises(ConflictError):
        await service.install_backup_fence(
            "backup-job",
            "snapshot-1",
        )


async def test_backup_fence_rejects_other_job_and_changed_snapshot():
    db = RetentionDatabase()
    service = store(db)
    await service.install_backup_fence(
        "backup-job",
        "snapshot-1",
    )
    with pytest.raises(ConflictError):
        await service.install_backup_fence(
            "other-job",
            "snapshot-2",
        )
    with pytest.raises(ConflictError):
        await service.install_backup_fence(
            "backup-job",
            "snapshot-2",
        )
    with pytest.raises(ConflictError):
        await service.release_backup_fence(
            "backup-job",
            "snapshot-2",
        )
    assert (
        next(row for row in db.pins if row["kind"] == "backup_fence")["state"]
        == "active"
    )


async def test_foreign_service_cannot_be_pinned_or_used_as_dependency():
    db = RetentionDatabase()
    db.objects[1]["service_unit_key"] = b"foreign-service"
    service = store(db)
    with pytest.raises(IntegrityError):
        await service.pin([1], kind="reader")
    with pytest.raises(IntegrityError):
        await service.add_dependencies(3, [1])
    assert not db.pins


async def test_retention_release_uses_full_service_scope():
    from unittest.mock import AsyncMock

    db = AsyncMock()
    service = store(db)
    await service.release(b"same-key")
    query, params = db.execute.call_args.args
    assert "service_unit_key=%s" in query
    assert params == (
        b"same-key",
        "workspace",
        b"schema-test-service",
        b"t",
        b"s",
        b"a",
    )


async def test_guard_and_same_pin_key_are_independent_between_services():
    db = RetentionDatabase()
    other = RetentionStore(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="other-service"),
    )
    local = store(db)
    assert (await local.ensure_guard())["id"] != (await other.ensure_guard())[
        "id"
    ]
    db.objects[2]["service_unit_key"] = b"other-service"
    first = await local.pin([1], kind="reader", retention_key="same-key")
    second = await other.pin([2], kind="reader", retention_key="same-key")
    await local.release(first.retention_key)
    pins = [row for row in db.pins if row["kind"] == "reader"]
    assert [(row["service_unit_key"], row["state"]) for row in pins] == [
        (b"schema-test-service", "released"),
        (b"other-service", "active"),
    ]
    await other.release(second.retention_key)
    assert all(row["state"] == "released" for row in pins)
