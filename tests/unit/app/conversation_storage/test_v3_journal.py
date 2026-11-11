# -*- coding: utf-8 -*-
# pylint: disable=use-dict-literal
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
import pytest


def manifest():
    return SimpleNamespace(
        migration_id="00000000-0000-0000-0000-000000000001",
        data={
            "version": 3,
            "target": {"schema": "business", "nas_roots": {"svc": "/nas"}},
            "source_boundary": {"snapshot_id": "frozen"},
            "services": [{"service_unit_id": "svc", "nas_root": "/nas"}],
            "workspaces": [],
            "assignments": {},
            "batches": {},
        },
        _persist=lambda: None,
    )


class Database:
    def __init__(self):
        self.next_id = 83
        self.entries = []
        self.commits = 0
        self.depth = 0
        self.queries = []
        self.hydration_pages = []
        self.rows = {}

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy(self.entries)
        self.depth += 1
        try:
            yield self
        except BaseException:
            self.entries = before
            raise
        else:
            self.commits += 1
        finally:
            self.depth -= 1

    async def fetch_one(self, query, params=None):
        self.queries.append(query)
        if "migration_entries" in query:
            return next(
                (
                    row
                    for row in self.entries
                    if row["entry_kind"] == params[1]
                    and row["source_key"] == params[2]
                ),
                None,
            )
        return self.rows.get(query.split(" FROM ")[-1].split()[0])

    async def fetch_all(self, query, params=None):
        self.queries.append(query)
        if "migration_entries" in query and "id>%s" in query:
            migration_id, last_id, limit = params
            rows = sorted(
                (
                    row
                    for row in self.entries
                    if row["migration_id"] == migration_id
                    and row["id"] > last_id
                ),
                key=lambda row: row["id"],
            )[:limit]
            self.hydration_pages.append((last_id, limit, len(rows)))
            return deepcopy(rows)
        return deepcopy(self.entries) if "migration_entries" in query else []

    async def insert(self, query, params):
        assert self.depth
        self.queries.append(query)
        self.next_id += 1
        self.entries.append(
            dict(
                id=self.next_id,
                migration_id=params[0],
                entry_kind=params[1],
                source_key=params[2],
                state="mapped",
                target_id=None,
                operation_key=params[3],
                details_text=params[4],
            ),
        )
        return self.next_id

    async def execute(self, query, params):
        assert self.depth
        self.queries.append(query)
        if "SET target_id" in query:
            self.entries[-1]["target_id"] = params[0]
        elif "SET details_text" in query:
            self.entries[-1]["details_text"] = params[0]
        return 1


async def test_sql_ticket_is_committed_before_manifest_mirror():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db, frozen = Database(), manifest()
    observed = []
    frozen._persist = lambda: observed.append(db.commits)
    journal = MigrationJournal(db, frozen)
    assigned = await journal.allocate("session", "svc/session:original")
    assert assigned["target_id"] == 84
    assert observed == [1]
    assert db.entries[0]["target_id"] == db.entries[0]["id"]
    assert (
        await journal.allocate("session", "svc/session:original") == assigned
    )
    assert len(db.entries) == 1
    assert not any(
        word in query.upper()
        for query in db.queries
        for word in ("CREATE ", "ALTER ", "RENAME ", "TRUNCATE ", "DROP ")
    )


async def test_hydrate_rebuilds_lost_external_mirror():
    from swe.app.conversation_storage.migration.journal import MigrationJournal
    from swe.app.conversation_storage.identity import WorkspaceScope

    db, frozen = Database(), manifest()
    assigned = await MigrationJournal(db, frozen).allocate(
        "publication",
        "svc/body",
        scope=WorkspaceScope("t", "s", "a", service_unit_id="svc"),
    )
    frozen.data["assignments"] = {}
    await MigrationJournal(db, frozen).hydrate()
    assert frozen.data["assignments"]["publication"]["svc/body"] == assigned


@pytest.mark.parametrize(
    ("count", "page_lengths"),
    [(0, [0]), (2, [2, 0]), (4, [2, 2, 0]), (5, [2, 2, 1])],
)
async def test_hydrate_reads_ordered_keyset_pages(
    monkeypatch,
    count,
    page_lengths,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage.migration import journal

    monkeypatch.setattr(journal, "HYDRATE_PAGE_SIZE", 2)
    db, frozen = Database(), manifest()
    frozen._persist = Mock()
    migration_id = frozen.migration_id.encode()
    # Gaps and reversed storage order require an id cursor, not row offsets.
    db.entries = [
        {
            "id": 10 * (index + 1),
            "migration_id": migration_id,
            "entry_kind": "session" if index % 2 == 0 else "batch",
            "source_key": "position-" + str(index),
            "state": "mapped" if index % 2 == 0 else "prepared",
            "target_id": 10 * (index + 1),
            "operation_key": "a" * 64,
            "details_text": None,
        }
        for index in reversed(range(count))
    ]
    db.entries.append(
        dict(
            id=1,
            migration_id=b"other-migration",
            entry_kind="session",
            source_key="foreign",
        ),
    )

    await journal.MigrationJournal(db, frozen).hydrate()

    assert list(frozen.data["assignments"].get("session", {})) == [
        "position-" + str(index) for index in range(0, count, 2)
    ]
    assert frozen.data["batches"] == {
        "position-" + str(index): "importing" for index in range(1, count, 2)
    }
    assert [length for _, _, length in db.hydration_pages] == page_lengths
    assert [cursor for cursor, _, _ in db.hydration_pages] == [
        20 * index for index in range(len(page_lengths))
    ]
    assert all("ORDER BY id LIMIT %s" in query for query in db.queries)
    frozen._persist.assert_called_once_with()


async def test_hydrate_checks_mirror_after_all_pages_before_replacing_it(
    monkeypatch,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage.migration import journal

    monkeypatch.setattr(journal, "HYDRATE_PAGE_SIZE", 2)
    db, frozen = Database(), manifest()
    sql_journal = journal.MigrationJournal(db, frozen)
    for position in ("first", "second", "last-page"):
        await sql_journal.allocate("session", position)
    frozen.data["assignments"]["session"]["last-page"]["target_id"] += 1
    before = deepcopy(frozen.data)
    frozen._persist = Mock()

    with pytest.raises(ValueError, match="matching durable SQL ticket"):
        await sql_journal.hydrate()

    assert frozen.data == before
    assert [length for _, _, length in db.hydration_pages] == [2, 1]
    frozen._persist.assert_not_called()


async def test_hydrate_backfills_ticket_scope_from_manifest_assignment():
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db, frozen = Database(), manifest()
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    assigned = await MigrationJournal(db, frozen).allocate(
        "publication",
        "svc/body",
        scope=scope,
    )
    db.entries[0]["details_text"] = None

    await MigrationJournal(db, frozen).hydrate()

    assert db.entries[0]["details_text"] is not None
    assert frozen.data["assignments"]["publication"]["svc/body"] == assigned


async def test_hydrate_defers_legacy_ticket_until_import_scope_is_available():
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db, frozen = Database(), manifest()
    journal = MigrationJournal(db, frozen)
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    assigned = await journal.allocate("publication", "svc/body", scope=scope)
    db.entries[0]["details_text"] = None
    frozen.data["assignments"]["publication"]["svc/body"] = {
        "target_id": assigned["target_id"],
        "operation_key": assigned["operation_key"],
        "relative_path": "bundles/aa/" + assigned["operation_key"] + ".bundle",
    }

    await journal.hydrate()
    unresolved = frozen.data["assignments"]["publication"]["svc/body"]
    assert unresolved["operation_key"] == assigned["operation_key"]
    assert unresolved["relative_path"] is None
    assert db.entries[0]["details_text"] is None

    resumed = await journal.allocate("publication", "svc/body", scope=scope)
    assert resumed["relative_path"] == assigned["relative_path"]
    assert db.entries[0]["details_text"] is not None


@pytest.mark.parametrize(
    ("sql_state", "expected"),
    [("prepared", "importing"), ("committed", "committed")],
)
async def test_hydrate_rebuilds_batch_state_from_sql_after_mirror_loss(
    sql_state,
    expected,
):
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db, frozen = Database(), manifest()
    db.entries.append(
        {
            "id": 84,
            "migration_id": b"00000000-0000-0000-0000-000000000001",
            "entry_kind": b"batch",
            "source_key": b"svc/session/0",
            "state": sql_state,
            "target_id": None,
            "operation_key": None,
            "details_text": None,
        },
    )

    await MigrationJournal(db, frozen).hydrate()

    assert frozen.data["batches"] == {"svc/session/0": expected}


async def test_first_job_refuses_unknown_business_rows():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db = Database()
    db.rows["swe_conversation_messages"] = {"id": 9}
    with pytest.raises(ValueError, match="empty|unknown"):
        await MigrationJournal(db, manifest()).prepare()
    assert not db.entries


async def test_retention_cannot_use_integer_ticket_allocator():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    with pytest.raises(ValueError, match="ticket"):
        await MigrationJournal(Database(), manifest()).allocate(
            "retention",
            "guard",
        )


async def test_direct_share_updates_only_extension_columns_and_preserves_frozen_row():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.migration.direct_import import (
        DirectImportStore,
        DirectTarget,
    )
    from swe.app.conversation_storage.shares import ShareInstall
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.publications import PublicationRequest

    record = dict(
        token="old-token",
        chat_id="chat",
        creator_id="user",
        tenant_id="tenant",
        snapshot_key="tenant/chat_shares/old-token.json",
        created_at=datetime(2026, 1, 1),
        access_count=7,
        last_access_at=None,
    )
    row = {
        **record,
        "service_unit_key": None,
        "publication_id": None,
        "byte_offset": None,
        "byte_length": None,
    }
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value=row),
        execute=AsyncMock(return_value=1),
    )
    journal = SimpleNamespace(record_share=AsyncMock())
    store = DirectImportStore(
        db,
        None,
        DirectTarget("business", "business", "job"),
        service_unit_id="svc",
        journal=journal,
    )
    scope = WorkspaceScope("tenant", "", "", service_unit_id="svc")
    request = PublicationRequest(
        "a" * 64,
        scope,
        "share_import",
        "job",
        0,
        scope_kind="legacy_share",
    )
    plan = ShareInstall(
        **{
            **record,
            "created_at": record["created_at"]
            .replace(tzinfo=timezone.utc)
            .isoformat(),
        },
    )
    index = SimpleNamespace(
        blocks={"share": SimpleNamespace(offset=123, length=456)},
    )
    await store._install_share(request, plan, {"id": 84}, index)
    sql, params = db.execute.call_args.args
    assert sql.startswith("UPDATE swe_chat_shares SET service_unit_key=")
    assert "access_count=" not in sql and "created_at=" not in sql
    assert params == (b"svc", 84, 123, 456, "old-token")
    journal.record_share.assert_awaited_once()


async def test_direct_share_refuses_counter_change_before_any_update():
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.migration.direct_import import (
        DirectImportStore,
        DirectTarget,
    )
    from swe.app.conversation_storage.errors import IntegrityError

    store = DirectImportStore(
        SimpleNamespace(
            fetch_one=AsyncMock(
                return_value={"token": "token", "access_count": 8},
            ),
        ),
        None,
        DirectTarget("business", "business", "job"),
        service_unit_id="svc",
        journal=None,
    )
    store._verify_share_record = AsyncMock(
        side_effect=IntegrityError("counter changed"),
    )
    with pytest.raises(IntegrityError, match="counter"):
        await store._install_share(
            None,
            SimpleNamespace(token="token"),
            None,
            None,
        )


async def test_direct_batch_completion_occurs_inside_typed_install():
    from unittest.mock import AsyncMock, patch
    from swe.app.conversation_storage.migration.direct_import import (
        DirectImportStore,
        DirectTarget,
    )
    from swe.app.conversation_storage.migration.install_records import (
        OfflineImportStore,
    )

    journal = SimpleNamespace(set_batch_state=AsyncMock())
    store = DirectImportStore(
        None,
        None,
        DirectTarget("business", "business", "job"),
        service_unit_id="svc",
        journal=journal,
    )
    store.batch_positions["operation"] = "svc/batch"
    with patch.object(OfflineImportStore, "_install", new=AsyncMock()):
        await store._install({"operation_key": b"operation"}, None, None)
    journal.set_batch_state.assert_awaited_once_with(
        "svc/batch",
        "committed",
        mirror=False,
    )


async def test_share_journal_refuses_unowned_existing_locator():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db = Database()
    with pytest.raises(ValueError, match="unknown"):
        await MigrationJournal(db, manifest()).record_share(
            "token",
            (b"svc", 84, 1, 2),
            (b"svc", 84, 1, 2),
        )


async def test_share_journal_records_original_extension_before_update():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db = Database()
    journal = MigrationJournal(db, manifest())
    await journal.record_share(
        "token",
        (None, None, None, None),
        (b"svc", 84, 1, 2),
    )
    assert "details_text" in db.queries[-1]


async def test_retention_resume_refuses_dependency_without_owned_source():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    frozen = manifest()
    frozen.data["workspaces"] = [
        dict(
            service_unit_id="svc",
            tenant_id="tenant",
            source_id="source",
            agent_id="agent",
        ),
    ]
    row = dict(
        service_unit_key=b"svc",
        tenant_key=b"tenant",
        source_key=b"source",
        agent_key=b"agent",
        scope_kind="workspace",
        kind="dependency",
        publication_id=84,
        retention_key=b"dep:99",
    )
    db = Database()
    from unittest.mock import AsyncMock

    db.fetch_one = AsyncMock(side_effect=[{**row, "id": 84}, None])
    with pytest.raises(ValueError, match="dependency"):
        await MigrationJournal(db, frozen)._validate_retention(row)


async def test_retention_logs_actual_id_without_consuming_entity_ticket():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    frozen = manifest()
    frozen.data["workspaces"] = [
        dict(
            service_unit_id="svc",
            tenant_id="tenant",
            source_id="source",
            agent_id="agent",
        ),
    ]
    row = dict(
        id=307,
        service_unit_key=b"svc",
        tenant_key=b"tenant",
        source_key=b"source",
        agent_key=b"agent",
        scope_kind="workspace",
        kind="gc_guard",
        publication_id=None,
        retention_key=b"guard",
        member_key=0,
        guard_marker=1,
    )
    db = Database()
    await MigrationJournal(db, frozen).record_retention(row)
    assert "target_id,details_text" in db.queries[-1]
    assert not frozen.data["assignments"]


def test_sql_target_binding_includes_server_identity_without_mutating_manifest():
    import json
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    frozen = manifest()
    target, _ = MigrationJournal(
        Database(),
        frozen,
        server_uuid="server-uuid",
    )._bindings()
    assert json.loads(target)["server_uuid"] == "server-uuid"
    assert "server_uuid" not in frozen.data["target"]


async def test_readonly_hydrate_does_not_persist_external_journal():
    from unittest.mock import Mock
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    frozen = manifest()
    frozen._persist = Mock()
    await MigrationJournal(Database(), frozen).hydrate(persist=False)
    frozen._persist.assert_not_called()


async def test_committed_share_resume_rejects_changed_locator():
    from unittest.mock import AsyncMock, patch
    from swe.app.conversation_storage.migration.direct_import import (
        DirectImportStore,
        DirectTarget,
    )
    from swe.app.conversation_storage.migration.install_records import (
        OfflineImportStore,
    )
    from swe.app.conversation_storage.errors import IntegrityError

    db = SimpleNamespace(
        fetch_one=AsyncMock(
            return_value={
                "service_unit_key": b"svc",
                "publication_id": 84,
                "byte_offset": 999,
                "byte_length": 20,
            },
        ),
    )
    store = DirectImportStore(
        db,
        SimpleNamespace(inspect=None),
        DirectTarget("business", "business", "job"),
        service_unit_id="svc",
        journal=None,
    )
    store._nas_io = AsyncMock(
        return_value=SimpleNamespace(
            blocks={"share": SimpleNamespace(offset=10, length=20)},
        ),
    )
    request = SimpleNamespace(
        operation_key="operation",
        scope=SimpleNamespace(sql_values=(b"svc",)),
    )
    plan = SimpleNamespace(
        share=SimpleNamespace(token="token", block_name="share"),
    )
    with patch.object(
        OfflineImportStore,
        "verify_batch",
        new=AsyncMock(
            return_value={
                "id": 84,
                "relative_path": b"path",
                "byte_length": 100,
            },
        ),
    ):
        with pytest.raises(IntegrityError, match="locator"):
            await store.verify_batch(request, 84, plan, {})


async def test_deferred_updates_keep_sql_authority_without_full_mirror_rewrites():
    from unittest.mock import Mock
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db, frozen = Database(), manifest()
    frozen._persist = Mock()
    journal = MigrationJournal(db, frozen)
    journal.persist_updates = False
    first = await journal.allocate("session", "scope/session")
    assert first["target_id"] == db.entries[0]["id"]
    assert db.commits == 1
    frozen._persist.assert_not_called()
    db.entries.append(
        {
            "migration_id": journal.migration_id,
            "entry_kind": b"batch",
            "source_key": b"scope/batch",
            "state": "committed",
        },
    )
    await journal.refresh_batch("scope/batch")
    assert frozen.data["batches"] == {"scope/batch": "committed"}
    frozen._persist.assert_not_called()
    assert not any("ORDER BY id" in query for query in db.queries)
    # A restart can reconstruct the ticket from SQL even if the mirror was lost.
    restarted = manifest()
    recovered = await MigrationJournal(db, restarted).allocate(
        "session",
        "scope/session",
    )
    assert recovered == first


async def test_refresh_batch_rejects_incomplete_sql_state():
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    db, frozen = Database(), manifest()
    journal = MigrationJournal(db, frozen)
    with pytest.raises(ValueError, match="committed SQL"):
        await journal.refresh_batch("missing")
    assert frozen.data["batches"] == {}
