# -*- coding: utf-8 -*-
import pytest

from swe.observability.outbox_recovery import OutboxPage, OutboxRecoveryWorker


class Store:
    def __init__(self):
        self.owner = None
        self.cursor = None
        self.acked = set()
        self.lease_valid = True
        self.fail_ack = False

    async def claim(self, owner, _seconds):
        if self.owner is not None:
            return False
        self.owner = owner
        return self.cursor

    async def renew(self, owner, _seconds):
        return self.owner == owner and self.lease_valid

    async def finish(self, owner, cursor):
        if not await self.renew(owner, 1):
            return False
        self.cursor = cursor
        self.owner = None
        return True

    async def acknowledged(self, event_id):
        if self.fail_ack:
            raise ConnectionError("database unavailable")
        return event_id in self.acked


class Scanner:
    def __init__(self):
        self.records = [{"event_id": "one"}, {"event_id": "two"}]
        self.calls = []

    async def scan_page(self, cursor, limit):
        self.calls.append((cursor, limit))
        return OutboxPage(self.records, None)

    async def verified_event(self, record):
        return record if record.get("valid", True) else None


class Emitter:
    def __init__(self):
        self.events = []
        self.accept = True

    def emit(self, event):
        self.events.append(event)
        return self.accept


@pytest.mark.asyncio
async def test_replays_until_inbox_ack_and_ignores_unverified_records():
    store, scanner, emitter = Store(), Scanner(), Emitter()
    scanner.records.append({"event_id": "forged", "valid": False})
    worker = OutboxRecoveryWorker(store, scanner, emitter)
    assert await worker.run_once() == 2
    assert await worker.run_once() == 2
    store.acked.update({"one", "two"})
    assert await worker.run_once() == 0
    assert [event["event_id"] for event in emitter.events] == [
        "one",
        "two",
        "one",
        "two",
    ]


@pytest.mark.asyncio
async def test_connection_failure_keeps_checkpoint_until_lease_expires():
    store, scanner, emitter = Store(), Scanner(), Emitter()
    worker = OutboxRecoveryWorker(store, scanner, emitter)
    store.fail_ack = True
    with pytest.raises(ConnectionError):
        await worker.run_once()
    assert store.cursor is None
    assert await worker.run_once() == 0
    store.owner = None  # lease expires after the crashed worker
    store.fail_ack = False
    assert await worker.run_once() == 2


@pytest.mark.asyncio
async def test_expired_owner_cannot_advance_checkpoint():
    store, scanner, emitter = Store(), Scanner(), Emitter()
    worker = OutboxRecoveryWorker(store, scanner, emitter)
    store.lease_valid = False
    assert await worker.run_once() == 0
    assert store.cursor is None
    assert emitter.events == []


@pytest.mark.asyncio
async def test_queue_rejection_does_not_count_as_inbox_ack():
    store, scanner, emitter = Store(), Scanner(), Emitter()
    emitter.accept = False
    worker = OutboxRecoveryWorker(store, scanner, emitter)
    assert await worker.run_once() == 0
    emitter.accept = True
    assert await worker.run_once() == 2
    assert [event["event_id"] for event in emitter.events] == [
        "one",
        "two",
        "one",
        "two",
    ]


@pytest.mark.asyncio
async def test_scan_page_limit_and_continuation():
    store, scanner, emitter = Store(), Scanner(), Emitter()
    worker = OutboxRecoveryWorker(store, scanner, emitter, page_size=1)
    with pytest.raises(ValueError, match="page limit"):
        await worker.run_once()
    assert store.cursor is None
    store.owner = None
    scanner.records = [{"event_id": "one"}]

    async def next_page(cursor, limit):
        assert cursor is None and limit == 1
        return OutboxPage(scanner.records, "next")

    scanner.scan_page = next_page
    assert await worker.run_once() == 1
    assert store.cursor == "next"


@pytest.mark.asyncio
async def test_ack_query_uses_exact_event_id_and_only_processed_inbox_rows():
    from contextlib import asynccontextmanager
    from swe.observability.outbox_recovery import OutboxRecoveryStore

    class Connection:
        def __init__(self):
            self.query = None
            self.rows = {}

        @asynccontextmanager
        async def acquire(self):
            yield self

        async def fetch_one(self, query, params):
            self.query = (query, params)
            return self.rows.get(params[0])

    connection = Connection()
    connection.rows["accepted"] = {"acknowledged": 1}
    store = OutboxRecoveryStore(connection, bytes(32))
    assert not await store.acknowledged("pending")
    assert await store.acknowledged("accepted")
    assert connection.query[1] == ("accepted",)
    assert "event_id=%s" in connection.query[0]
    assert "processing_state=1" in connection.query[0]


def test_scope_digest_uses_all_exact_identities():
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import scope_digest

    first = WorkspaceScope("tenant", "source", "agent", service_unit_id="unit")
    assert len(scope_digest(first)) == 32
    for different in (
        WorkspaceScope("other", "source", "agent", service_unit_id="unit"),
        WorkspaceScope("tenant", "other", "agent", service_unit_id="unit"),
        WorkspaceScope("tenant", "source", "other", service_unit_id="unit"),
        WorkspaceScope("tenant", "source", "agent", service_unit_id="other"),
    ):
        assert scope_digest(first) != scope_digest(different)


@pytest.mark.asyncio
async def test_scheduler_checks_scope_and_continues_across_bounded_pages():
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryScheduler,
        scope_digest,
    )

    first = WorkspaceScope("t", "s", "a", service_unit_id="unit")
    other = WorkspaceScope("t", "s", "b", service_unit_id="unit")

    def row(scope):
        return {
            "scope_key": scope_digest(scope),
            **scope.fields,
            "workspace_root": b"/root",
            "storage_authority": "legacy",
        }

    rows = sorted([row(first), row(other)], key=lambda item: item["scope_key"])
    rows[0]["agent_key"] = b"tampered"

    class Registry:
        async def list_scopes(self, unit, after, limit):
            assert unit == b"unit"
            return [item for item in rows if item["scope_key"] > after][:limit]

    seen = []

    async def resolve(scope, _root, _authority):
        seen.append(scope)
        return None

    scheduler = OutboxRecoveryScheduler(
        Registry(),
        "unit",
        resolve,
        page_size=1,
    )
    assert await scheduler.run_once() == 1
    assert await scheduler.run_once() == 1
    assert await scheduler.run_once() == 0
    assert len(seen) == 1
    assert scope_digest(seen[0]) == rows[1]["scope_key"]


@pytest.mark.asyncio
async def test_capture_gate_opens_only_after_scope_registration_commit():
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="unit")
    scope_dict = dict(
        zip(
            ("service_unit_id", "tenant_id", "source_id", "agent_id"),
            scope.values,
        ),
    )
    clear_registered_scopes()
    enable_registered_scopes()

    class Connection:
        fail = True

        @asynccontextmanager
        async def transaction(self):
            yield self
            if self.fail:
                raise ConnectionError("commit failed")

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    connection = Connection()
    store = OutboxRecoveryStore(connection, scope_digest(scope))
    with pytest.raises(ConnectionError):
        await store.register_scope(scope, "/root", "legacy")
    assert not scope_registered_bound(scope_dict)
    connection.fail = False
    await store.register_scope(scope, "/root", "legacy")
    assert scope_registered_bound(scope_dict)
    assert not scope_registered_bound({**scope_dict, "source_id": "other"})
    clear_registered_scopes()
    assert not scope_registered_bound(scope_dict)


@pytest.mark.asyncio
async def test_shutdown_during_registration_does_not_reopen_capture_gate():
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="unit")
    scope_dict = dict(
        zip(
            ("service_unit_id", "tenant_id", "source_id", "agent_id"),
            scope.values,
        ),
    )

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield self
            clear_registered_scopes()

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    clear_registered_scopes()
    enable_registered_scopes()
    await OutboxRecoveryStore(
        Connection(),
        scope_digest(scope),
    ).register_scope(scope, "/root", "legacy")
    assert not scope_registered_bound(scope_dict)


@pytest.mark.asyncio
async def test_offline_rebind_requires_matching_verified_copies_ack_and_no_lease(
    tmp_path,
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        rebind_offline,
        scope_digest,
    )

    scope = WorkspaceScope("t", "s", "a", service_unit_id="unit")
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    event = {"event_id": "id", "reference_id": "ref"}
    item = {"event": event, "content_sha256": "a" * 64}

    class Scanner:
        def __init__(self, root):
            self.scope = scope
            self.workspace = SimpleNamespace(workspace_dir=root)
            self.events = [item]

        async def scan_offline_complete(self, *, max_entries):
            return self.events

    class Database:
        active = False
        ack = False
        updates = []

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetch_one(self, query, _params):
            if "outbox_scopes" in query:
                return {
                    **scope.fields,
                    "workspace_root": str(old_root).encode(),
                    "storage_authority": "legacy",
                }
            if "outbox_recovery" in query:
                return {"active": self.active}
            return {"acknowledged": 1} if self.ack else None

        async def execute(self, query, params):
            if query.startswith("UPDATE"):
                self.updates.append((query, params))
            return 1

    old, new, db = Scanner(old_root), Scanner(new_root), Database()
    store = OutboxRecoveryStore(db, scope_digest(scope))

    async def attempt():
        await rebind_offline(
            store,
            scope,
            old,
            new,
            old_root=str(old_root),
            old_authority="legacy",
            new_root=str(new_root),
            new_authority="mysql-metadata",
            writers_stopped=True,
        )

    with pytest.raises(ValueError, match="unacknowledged"):
        await attempt()
    assert db.updates == []
    db.ack = True
    new.events = [{**item, "event": {**event, "reference_id": "changed"}}]
    with pytest.raises(ValueError, match="differs"):
        await attempt()
    new.events = [{**item, "content_sha256": "b" * 64}]
    with pytest.raises(ValueError, match="differs"):
        await attempt()
    assert db.updates == []
    new.events = [{"event": event}]
    with pytest.raises(ValueError, match="digest"):
        await attempt()
    assert db.updates == []
    new.events = [item]
    db.active = True
    with pytest.raises(ValueError, match="lease"):
        await attempt()
    db.active = False
    await attempt()
    assert len(db.updates) == 2
    assert db.updates[0][1] == (
        str(new_root).encode(),
        "mysql-metadata",
        scope_digest(scope),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tenant,source,runtime",
    [
        ("default", "source-a", "default_source-a"),
        ("tenant-a", "source-a", "dGVuYW50LWE.c291cmNlLWE"),
        ("tenant-a", "source-a", "scope.v1.dGVuYW50LWE.c291cmNlLWE"),
    ],
)
async def test_scope_gate_accepts_runtime_tenant_only_with_same_scope(
    tenant,
    source,
    runtime,
):
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    scope = WorkspaceScope(tenant, source, "agent", service_unit_id="unit")

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    clear_registered_scopes()
    enable_registered_scopes()
    await OutboxRecoveryStore(
        Connection(),
        scope_digest(scope),
    ).register_scope(scope, "/root", "legacy")
    carrier = {
        "service_unit_id": "unit",
        "source_id": source,
        "tenant_id": runtime,
        "agent_id": "agent",
    }
    assert scope_registered_bound(carrier)
    for field, value in (
        ("service_unit_id", "other"),
        ("source_id", "other"),
        ("agent_id", "other"),
        ("tenant_id", "other"),
    ):
        assert not scope_registered_bound({**carrier, field: value})
    assert not scope_registered_bound(
        {**carrier, "tenant_id": "dGVuYW50LWE.c291cmNlLWI"},
    )
    clear_registered_scopes()
    assert not scope_registered_bound(carrier)


@pytest.mark.asyncio
async def test_scope_gate_rejects_ambiguous_runtime_tenant():
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    class Connection:
        def __init__(self, scope):
            self.scope = scope

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **self.scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    clear_registered_scopes()
    enable_registered_scopes()
    for tenant in ("default", "default_source-a"):
        scope = WorkspaceScope(
            tenant,
            "source-a",
            "agent",
            service_unit_id="unit",
        )
        await OutboxRecoveryStore(
            Connection(scope),
            scope_digest(scope),
        ).register_scope(scope, "/root", "legacy")
    assert not scope_registered_bound(
        {
            "service_unit_id": "unit",
            "tenant_id": "default_source-a",
            "source_id": "source-a",
            "agent_id": "agent",
        },
    )
    clear_registered_scopes()


@pytest.mark.asyncio
async def test_failed_equivalent_registration_does_not_reuse_other_gate():
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    class Connection:
        def __init__(self, scope, fail=False):
            self.scope, self.fail = scope, fail

        @asynccontextmanager
        async def transaction(self):
            yield self
            if self.fail:
                raise ConnectionError("commit failed")

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **self.scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    clear_registered_scopes()
    enable_registered_scopes()
    first = WorkspaceScope(
        "default",
        "source-a",
        "agent",
        service_unit_id="unit",
    )
    await OutboxRecoveryStore(
        Connection(first),
        scope_digest(first),
    ).register_scope(first, "/root", "legacy")
    second = WorkspaceScope(
        "default_source-a",
        "source-a",
        "agent",
        service_unit_id="unit",
    )
    with pytest.raises(ConnectionError):
        await OutboxRecoveryStore(
            Connection(second, fail=True),
            scope_digest(second),
        ).register_scope(second, "/root", "legacy")
    assert not scope_registered_bound(
        {
            "service_unit_id": "unit",
            "tenant_id": "default_source-a",
            "source_id": "source-a",
            "agent_id": "agent",
        },
    )
    clear_registered_scopes()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_first", ["a", "b"])
@pytest.mark.parametrize("failure", [True, False])
async def test_concurrent_registration_waits_for_all_and_preserves_failure(
    finish_first,
    failure,
):
    import asyncio
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="unit")
    carrier = {
        "service_unit_id": "unit",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
    }
    release = {name: asyncio.Event() for name in ("a", "b")}
    entered = {name: asyncio.Event() for name in ("a", "b")}

    class Connection:
        def __init__(self, name):
            self.name = name

        @asynccontextmanager
        async def transaction(self):
            entered[self.name].set()
            await release[self.name].wait()
            yield self
            if self.name == "b" and failure:
                raise ConnectionError("commit failed")

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    clear_registered_scopes()
    enable_registered_scopes()

    async def register(name):
        await OutboxRecoveryStore(
            Connection(name),
            scope_digest(scope),
        ).register_scope(scope, "/root", "legacy")

    a = asyncio.create_task(register("a"))
    b = asyncio.create_task(register("b"))
    await asyncio.gather(*(event.wait() for event in entered.values()))
    assert not scope_registered_bound(carrier)
    release[finish_first].set()
    first = a if finish_first == "a" else b
    first_result = await asyncio.gather(first, return_exceptions=True)
    if finish_first == "b" and failure:
        assert isinstance(first_result[0], ConnectionError)
    assert not scope_registered_bound(carrier)
    release["b" if finish_first == "a" else "a"].set()
    results = await asyncio.gather(a, b, return_exceptions=True)
    assert isinstance(results[1], ConnectionError) is failure
    assert scope_registered_bound(carrier) is not failure
    if failure:
        release["b"].set()
        await register("a")
        assert scope_registered_bound(carrier)
    clear_registered_scopes()


@pytest.mark.asyncio
async def test_concurrent_different_root_conflict_keeps_scope_closed():
    import asyncio
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="unit")
    carrier = {
        "service_unit_id": "unit",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
    }
    release = asyncio.Event()

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            await release.wait()
            yield self

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    clear_registered_scopes()
    enable_registered_scopes()
    store = OutboxRecoveryStore(Connection(), scope_digest(scope))
    a = asyncio.create_task(store.register_scope(scope, "/root", "legacy"))
    b = asyncio.create_task(store.register_scope(scope, "/other", "legacy"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(a, b, return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], ValueError)
    assert not scope_registered_bound(carrier)
    clear_registered_scopes()


def scope_registered_bound(scope):
    from swe.observability.outbox_recovery import scope_registered

    return scope_registered(scope, ("/root", "legacy"))


@pytest.mark.asyncio
async def test_conflicting_active_root_cannot_borrow_other_root_retry():
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox_recovery import (
        OutboxRecoveryStore,
        clear_registered_scopes,
        enable_registered_scopes,
        scope_digest,
        scope_registered,
    )

    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="unit")
    carrier = {
        "service_unit_id": "unit",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
    }

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": b"/root",
                "storage_authority": "legacy",
            }

    clear_registered_scopes()
    enable_registered_scopes()
    store = OutboxRecoveryStore(Connection(), scope_digest(scope))
    await store.register_scope(scope, "/root", "legacy")
    assert scope_registered(carrier, ("/root", "legacy"))
    with pytest.raises(ValueError):
        await store.register_scope(scope, "/other", "legacy")
    await store.register_scope(scope, "/root", "legacy")
    assert not scope_registered(carrier, ("/other", "legacy"))
    assert not scope_registered(carrier, ("/root", "legacy"))
    assert not scope_registered(carrier, None)
    clear_registered_scopes()
