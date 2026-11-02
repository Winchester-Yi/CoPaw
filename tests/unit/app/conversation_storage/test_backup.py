# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.backup import (
    RestoreEvidence,
    decode_metadata,
    encode_metadata,
    snapshot_publication_ids,
    ConversationBackup,
)
from swe.app.conversation_storage.errors import IntegrityError


def test_publication_turn_must_keep_original_session_after_chat_rebind():
    from swe.app.conversation_storage.backup import (
        _validate_publication_associations,
    )

    fields = {
        "service_unit_key": b"A",
        "tenant_key": b"t",
        "source_key": b"s",
        "agent_key": b"a",
    }
    publication = {
        "id": 7,
        **fields,
        "scope_kind": "workspace",
        "session_id": 1,
        "chat_id": 3,
        "turn_id": 4,
    }
    with pytest.raises(IntegrityError, match="Turn"):
        _validate_publication_associations(
            {7: publication},
            {1: {"id": 1, **fields}},
            {3: {"id": 3, "session_id": 2, **fields}},
            {4: {"id": 4, "session_id": 2, "chat_id": 3}},
        )


def snapshot():
    return {
        "sessions": [{"recovery_publication_id": 2}],
        "chats": [],
        "turns": [],
        "messages": [],
        "shares": [],
        "retentions": [],
        "publications": [
            {"id": 1, "state": "committed", "gc_state": "live"},
            {"id": 2, "state": "committed", "gc_state": "live"},
        ],
    }


def test_snapshot_preserves_binary_identity_timestamps_and_opaque_metadata():
    value = {
        "key": b"\x00\xff",
        "at": datetime(2026, 10, 30, tzinfo=timezone.utc),
        "body": {"future": [False, None, 123]},
    }
    assert decode_metadata(encode_metadata(value)) == value


def test_snapshot_includes_all_live_publications_and_retained_deleted_rows():
    value = snapshot()
    value["messages"] = [
        {"publication_id": 1, "deleted_at": datetime(2026, 10, 30)},
    ]
    assert snapshot_publication_ids(value) == [1, 2]


@pytest.mark.parametrize("state", ["prepared", "aborted"])
def test_snapshot_rejects_unsettled_or_noncommitted_references(state):
    value = snapshot()
    value["publications"][1]["state"] = state
    with pytest.raises(IntegrityError):
        snapshot_publication_ids(value)


def test_snapshot_rejects_cross_scope_dependency_or_missing_locator_target():
    value = snapshot()
    value["retentions"] = [{"state": "active", "publication_id": 99}]
    with pytest.raises(IntegrityError):
        snapshot_publication_ids(value)


def test_backup_fences_use_fixed_namespace_order(tmp_path):
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    service = ConversationBackup(
        object(),
        BundleStore(tmp_path),
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
    )
    assert [store.scope_kind for store in service.stores] == [
        "legacy_share",
        "workspace",
    ]


def test_restore_requires_explicit_stopped_source_and_isolated_target_evidence():
    with pytest.raises(ValueError):
        RestoreEvidence("", "isolated-clone")
    assert RestoreEvidence("writers-stopped-job", "isolated-clone")


def test_session_recovery_uses_exact_locator_instead_of_a_block_name(tmp_path):
    from swe.app.conversation_storage.backup import _recovery_payload
    from swe.app.conversation_storage.bundle import (
        BundleBlock,
        BundleStore,
        new_operation_key,
    )

    operation = new_operation_key()
    bundles = BundleStore(tmp_path)
    index = bundles.publish(
        operation,
        [BundleBlock("custom-root", "recovery", b'{"future":true}')],
        metadata={"operation_key": operation},
    )
    data = {
        "publications": [
            {
                "id": 1,
                "session_id": 2,
                "relative_path": index.relative_path.encode(),
                "byte_length": index.byte_length,
                "operation_key": operation.encode(),
            },
        ],
        "messages": [],
    }
    extent = index.blocks["custom-root"]
    assert _recovery_payload(
        data,
        bundles,
        1,
        offset=extent.offset,
        length=extent.length,
    ) == {"future": True}


def test_restore_fences_active_owners_without_dropping_original_turn_or_pins():
    from swe.app.conversation_storage.backup import _restored_rows

    value = {
        "sessions": [
            {
                "id": 1,
                "generation": 3,
                "owner_key": b"old",
                "lease_until": datetime(2026, 10, 30),
                "active_turn_id": 2,
            },
        ],
        "turns": [{"id": 2, "state": "running"}],
        "retentions": [
            {"id": 3, "kind": "reader", "state": "active"},
            {"id": 4, "kind": "dependency", "state": "active"},
        ],
    }
    restored, report = _restored_rows(
        value,
        RestoreEvidence("stopped", "isolated"),
    )
    assert restored["sessions"][0]["generation"] == 4
    assert restored["sessions"][0]["owner_key"] is None
    assert restored["sessions"][0]["active_turn_id"] == 2
    assert restored["turns"][0]["state"] == "settling"
    assert restored["retentions"][0]["state"] == "released"
    assert restored["retentions"][1]["state"] == "active"
    assert len(report["changes"]) == 3
    assert value["sessions"][0]["owner_key"] == b"old"


@pytest.mark.parametrize(
    "state",
    [{}, {"turn_states": {"q": {"status": "failed"}}}],
)
def test_capture_rejects_missing_or_mismatched_terminal_turn_state(state):
    from swe.app.conversation_storage.backup import _validate_turn_recovery

    with pytest.raises(IntegrityError, match="Turn"):
        _validate_turn_recovery(
            {
                "question_msg_key": b"q",
                "runtime_turn_key": None,
                "history_status": "completed",
            },
            state,
            terminal=True,
        )


async def test_copy_failure_retains_backup_pins_after_fence_release(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage import backup
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    events = []
    scope = WorkspaceScope(
        "t",
        "s",
        "a",
        service_unit_id="schema-test-service",
    )
    service = ConversationBackup(
        SimpleNamespace(execute=AsyncMock(return_value=1)),
        BundleStore(tmp_path / "nas"),
        scope,
    )
    data = {name: [] for name in backup.TABLES}
    data["publications"] = [
        {
            "id": 1,
            "state": "committed",
            "gc_state": "live",
            "scope_kind": "workspace",
            "service_unit_key": b"schema-test-service",
            "tenant_key": b"t",
            "source_key": b"s",
            "agent_key": b"a",
            "session_id": None,
            "chat_id": None,
            "turn_id": None,
        },
    ]
    service._snapshot = AsyncMock(return_value=data)

    def store(scope_values):
        async def fence(*_):
            events.append("fence")

        async def pin(*_, **__):
            events.append("pin")

        async def release(*_):
            assert "pin" in events
            events.append("release_fence")

        return SimpleNamespace(
            scope_values=scope_values,
            install_backup_fence=fence,
            pin=pin,
            release_backup_fence=release,
            release=AsyncMock(),
        )

    service.stores = (
        store(("workspace", *scope.sql_values)),
        store(("legacy_share", b"schema-test-service", b"t", b"", b"")),
    )

    async def install_fences(keys, snapshot_ref):
        for retention, key in zip(service.stores, keys):
            await retention.install_backup_fence(key, snapshot_ref)

    service._install_fences = install_fences

    def failed_copy(*_):
        assert events == [
            "fence",
            "fence",
            "pin",
            "release_fence",
            "release_fence",
        ]
        raise OSError("copy failed")

    monkeypatch.setattr(backup, "_copy_bundle", failed_copy)
    with pytest.raises(OSError, match="copy failed"):
        await service.capture(tmp_path / "backup")
    for retention in service.stores:
        retention.release.assert_not_awaited()
    assert (tmp_path / "backup" / "metadata.json").exists()
    assert not (tmp_path / "backup" / "report.json").exists()


async def test_capture_cannot_mark_unsettled_restore_ready(tmp_path):
    from swe.app.conversation_storage import backup
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    scope = WorkspaceScope(
        "t",
        "s",
        "a",
        service_unit_id="schema-test-service",
    )
    service = ConversationBackup(
        SimpleNamespace(),
        BundleStore(tmp_path),
        scope,
    )
    service._snapshot = AsyncMock(
        return_value={name: [] for name in backup.TABLES},
    )
    service.stores = tuple(
        SimpleNamespace(
            install_backup_fence=AsyncMock(),
            release_backup_fence=AsyncMock(),
            scope_values=values,
        )
        for values in (
            ("legacy_share", b"schema-test-service", b"t", b"", b""),
            ("workspace", *scope.sql_values),
        )
    )

    service._install_fences = AsyncMock()

    async def incomplete(path, identity):
        return {
            "passed": True,
            "complete_metadata_and_readers": True,
            "backup_id": identity,
            "snapshot_ref": str(path),
            "isolated_target_ref": "clone",
            "reader_counts": {
                name: 0 for name in ("sessions", "chats", "turns", "shares")
            },
            "restore_ready": False,
            "needs_settlement": [{"turn_id": 2}],
        }

    with pytest.raises(IntegrityError, match="verification"):
        await service.capture(
            tmp_path / "snapshot",
            restore_verifier=incomplete,
        )


@pytest.mark.parametrize(
    "name",
    ["business", "test_conversation_restore_notgenerated"],
)
def test_restore_refuses_non_generated_database_before_mutating(name):
    from swe.app.conversation_storage.backup import _restore_target

    with pytest.raises(ValueError, match="generated isolated"):
        _restore_target(
            SimpleNamespace(config=SimpleNamespace(database=name)),
            "source",
        )


async def test_snapshot_rejects_read_committed_isolation(tmp_path):
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    @asynccontextmanager
    async def transaction():
        yield

    db = SimpleNamespace(
        transaction=transaction,
        fetch_one=AsyncMock(return_value={"level": "READ-COMMITTED"}),
        fetch_all=AsyncMock(),
    )
    with pytest.raises(IntegrityError, match="REPEATABLE READ"):
        await ConversationBackup(
            db,
            BundleStore(tmp_path),
            WorkspaceScope(
                "t",
                "s",
                "a",
                service_unit_id="schema-test-service",
            ),
        )._snapshot()
    db.fetch_all.assert_not_awaited()


def test_bundle_copy_rejects_symlink_destination_parent(tmp_path):
    from swe.app.conversation_storage.backup import _copy_bundle
    from swe.app.conversation_storage.bundle import (
        BundleBlock,
        BundleStore,
        new_operation_key,
    )

    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "bundles").symlink_to(outside, target_is_directory=True)
    operation = new_operation_key()
    bundles = BundleStore(source)
    index = bundles.publish(
        operation,
        [BundleBlock("body", "share", b"{}")],
        metadata={"operation_key": operation},
    )
    with pytest.raises((IntegrityError, OSError)):
        _copy_bundle(
            bundles,
            target,
            {
                "relative_path": index.relative_path.encode(),
                "byte_length": index.byte_length,
                "operation_key": operation.encode(),
            },
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["offline_import", "chat_meta"])
def test_imported_scope_batch_content_metadata_has_no_single_session(kind):
    from swe.app.conversation_storage.backup import _validate_associations

    identity = {
        "service_unit_key": b"schema-test-service",
        "tenant_key": b"t",
        "source_key": b"s",
        "agent_key": b"a",
        "user_key": b"u",
        "channel_key": b"console",
    }
    data = {
        "shares": [],
        "sessions": [{"id": 1, **identity, "recovery_publication_id": None}],
        "chats": [
            {
                "id": 2,
                **identity,
                "session_id": 1,
                "content_meta_publication_id": 3,
                "active_checkpoint_publication_id": None,
            },
        ],
        "turns": [],
        "messages": [],
        "publications": [
            {
                "id": 3,
                **identity,
                "scope_kind": "workspace",
                "kind": kind,
                "session_id": None,
                "chat_id": None,
                "turn_id": None,
            },
        ],
    }
    if kind == "offline_import":
        _validate_associations(data)
    else:
        with pytest.raises(IntegrityError, match="Session differs"):
            _validate_associations(data)


async def test_backup_drains_prepared_operations_before_atomically_fencing(
    tmp_path,
):
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    events = []

    @asynccontextmanager
    async def transaction():
        events.append("begin")
        yield
        events.append("commit")

    attempts = iter(([{"id": 1}], []))

    async def pending(*_):
        events.append("prepared")
        return next(attempts)

    service = ConversationBackup(
        SimpleNamespace(transaction=transaction, fetch_all=pending),
        BundleStore(tmp_path),
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
    )

    def retention(name):
        async def guard():
            events.append("guard:" + name)

        async def fence(*_):
            events.append("fence:" + name)

        return SimpleNamespace(lock_guard=guard, install_backup_fence=fence)

    service.stores = (retention("legacy"), retention("workspace"))
    await service._install_fences(["job:l", "job:w"], "snapshot")
    assert events == [
        "begin",
        "guard:legacy",
        "guard:workspace",
        "prepared",
        "commit",
        "begin",
        "guard:legacy",
        "guard:workspace",
        "prepared",
        "fence:legacy",
        "fence:workspace",
        "commit",
    ]


async def test_backup_settle_timeout_does_not_install_a_blocking_fence(
    tmp_path,
    monkeypatch,
):
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage import backup
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.errors import ConflictError

    @asynccontextmanager
    async def transaction():
        yield

    service = ConversationBackup(
        SimpleNamespace(
            transaction=transaction,
            fetch_all=AsyncMock(return_value=[{"id": 1}]),
        ),
        BundleStore(tmp_path),
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
    )
    service.stores = tuple(
        SimpleNamespace(
            lock_guard=AsyncMock(),
            install_backup_fence=AsyncMock(),
        )
        for _ in range(2)
    )
    monkeypatch.setattr(backup, "BACKUP_SETTLE_SECONDS", 0)
    with pytest.raises(ConflictError, match="settle"):
        await service._install_fences(["job:l", "job:w"], "snapshot")
    for store in service.stores:
        store.install_backup_fence.assert_not_awaited()


@pytest.mark.parametrize("gc_state", ["deleting", "reclaimed"])
@pytest.mark.parametrize(
    "inactive_kind",
    ["hidden", "message_deleted", "archive_chat_deleted", "chat_metadata"],
)
def test_snapshot_retains_inactive_locator_tombstones_without_requiring_body(
    gc_state,
    inactive_kind,
):
    data = snapshot()
    data["publications"][0]["gc_state"] = gc_state
    data["chats"] = [
        {
            "id": 9,
            "deleted_at": datetime(2026, 10, 30),
            "content_meta_publication_id": (
                1 if inactive_kind == "chat_metadata" else None
            ),
        },
    ]
    if inactive_kind != "chat_metadata":
        data["messages"] = [
            {
                "publication_id": 1,
                "archive_publication_id": None,
                "deleted_at": (
                    datetime(2026, 10, 30)
                    if inactive_kind == "message_deleted"
                    else None
                ),
                "layer": (
                    "archive"
                    if inactive_kind == "archive_chat_deleted"
                    else "memory"
                ),
                "archive_chat_id": 9,
                "current_visible": 0 if inactive_kind == "hidden" else 1,
            },
        ]
    assert snapshot_publication_ids(data) == [2]
    assert data["publications"][0]["gc_state"] == gc_state


@pytest.mark.parametrize(
    "reference",
    [
        "message",
        "archive",
        "chat",
        "root",
        "turn",
        "share",
        "pin",
        "retained_history",
    ],
)
def test_snapshot_still_rejects_reclaimed_effective_references(reference):
    data = snapshot()
    data["publications"][0]["gc_state"] = "reclaimed"
    if reference in ("message", "archive"):
        data["chats"] = [{"id": 9, "deleted_at": None}]
        data["messages"] = [
            {
                "publication_id": 1,
                "archive_publication_id": None,
                "deleted_at": None,
                "layer": "archive" if reference == "archive" else "memory",
                "archive_chat_id": 9,
                "current_visible": 1,
            },
        ]
    elif reference == "chat":
        data["chats"] = [
            {"id": 9, "deleted_at": None, "content_meta_publication_id": 1},
        ]
    elif reference == "root":
        data["sessions"][0]["recovery_publication_id"] = 1
    elif reference == "turn":
        data["turns"] = [{"admission_publication_id": 1}]
    elif reference == "share":
        data["shares"] = [{"publication_id": 1}]
    elif reference == "pin":
        data["retentions"] = [{"publication_id": 1, "state": "active"}]
    else:
        data["publications"][0]["retained_history"] = 1
    with pytest.raises(IntegrityError, match="committed/live"):
        snapshot_publication_ids(data)


@pytest.mark.parametrize("state", ["missing", "aborted"])
def test_inactive_locator_still_rejects_missing_or_aborted_publication(state):
    data = snapshot()
    data["messages"] = [
        {"publication_id": 1, "deleted_at": datetime(2026, 10, 30)},
    ]
    if state == "missing":
        data["publications"].pop(0)
    else:
        data["publications"][0]["state"] = "aborted"
    with pytest.raises(IntegrityError):
        snapshot_publication_ids(data)


def test_inactive_reclaimed_locator_still_rejects_cross_scope_association():
    from unittest.mock import Mock
    from swe.app.conversation_storage.backup import validate_backup

    identity = {
        "service_unit_key": b"schema-test-service",
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
        "user_key": b"alice",
        "channel_key": b"console",
    }
    data = {
        name: []
        for name in (
            "sessions",
            "chats",
            "turns",
            "messages",
            "shares",
            "retentions",
            "publications",
        )
    }
    data["sessions"] = [{"id": 1, **identity, "recovery_publication_id": None}]
    data["messages"] = [
        {
            "id": 2,
            "service_unit_key": b"schema-test-service",
            "session_id": 1,
            "publication_id": 3,
            "deleted_at": datetime(2026, 10, 30),
            "origin_chat_id": None,
            "archive_chat_id": None,
            "turn_id": None,
        },
    ]
    data["publications"] = [
        {
            "id": 3,
            **identity,
            "source_key": b"other",
            "scope_kind": "workspace",
            "state": "committed",
            "gc_state": "reclaimed",
            "session_id": 1,
            "chat_id": None,
            "turn_id": None,
        },
    ]
    bundles = SimpleNamespace(inspect=Mock())
    with pytest.raises(IntegrityError, match="scope mismatch"):
        validate_backup(data, bundles)
    bundles.inspect.assert_not_called()


def test_inactive_locator_rejects_unknown_gc_state():
    data = snapshot()
    data["messages"] = [
        {"publication_id": 1, "deleted_at": datetime(2026, 10, 30)},
    ]
    data["publications"][0]["gc_state"] = "unknown"
    with pytest.raises(IntegrityError):
        snapshot_publication_ids(data)


def test_restore_keeps_unpublished_admission_reserved_for_rejection_or_commit():
    from swe.app.conversation_storage.backup import _restored_rows

    snapshot = {
        "sessions": [
            {
                "id": 1,
                "generation": 3,
                "owner_key": b"old",
                "lease_until": None,
                "active_turn_id": 2,
            },
        ],
        "turns": [{"id": 2, "state": "reserved"}],
        "retentions": [],
    }
    restored, _ = _restored_rows(
        snapshot,
        RestoreEvidence("stopped", "isolated"),
    )
    assert restored["turns"][0]["state"] == "reserved"
    assert restored["sessions"][0]["generation"] == 4
    assert restored["sessions"][0]["owner_key"] is None
    assert restored["sessions"][0]["active_turn_id"] == 2


def test_restore_preserves_stop_intent_for_terminal_cas():
    from swe.app.conversation_storage.backup import _restored_rows

    snapshot = {
        "sessions": [
            {
                "id": 1,
                "generation": 3,
                "owner_key": b"old",
                "lease_until": None,
                "active_turn_id": 2,
            },
        ],
        "turns": [{"id": 2, "state": "stopping"}],
        "retentions": [],
    }
    restored, _ = _restored_rows(
        snapshot,
        RestoreEvidence("stopped", "isolated"),
    )
    assert restored["turns"][0]["state"] == "stopping"


def test_backup_preserves_same_service_historical_session_after_chat_rebind():
    from swe.app.conversation_storage.backup import _validate_associations

    fields = {
        "service_unit_key": b"service-A",
        "tenant_key": b"t",
        "source_key": b"s",
        "agent_key": b"a",
        "user_key": b"u",
        "channel_key": b"console",
    }
    data = {
        name: []
        for name in (
            "sessions",
            "chats",
            "turns",
            "messages",
            "publications",
            "shares",
            "retentions",
        )
    }
    data["sessions"] = [
        {"id": identity, **fields, "recovery_publication_id": None}
        for identity in (1, 2)
    ]
    data["chats"] = [
        {
            "id": 3,
            **fields,
            "session_id": 2,
            "content_meta_publication_id": None,
            "active_checkpoint_publication_id": None,
        },
    ]
    data["turns"] = [
        {
            "id": 4,
            "service_unit_key": b"service-A",
            "session_id": 1,
            "chat_id": 3,
            "admission_publication_id": 5,
            "terminal_publication_id": None,
        },
    ]
    data["publications"] = [
        {
            "id": 5,
            **fields,
            "scope_kind": "workspace",
            "kind": "admission",
            "session_id": 1,
            "chat_id": 3,
            "turn_id": 4,
        },
    ]
    data["messages"] = [
        {
            "id": 6,
            "service_unit_key": b"service-A",
            "session_id": 1,
            "origin_chat_id": 3,
            "archive_chat_id": 3,
            "turn_id": 4,
            "publication_id": 5,
            "archive_publication_id": 5,
        },
    ]
    data["publications"].append(
        {
            "id": 7,
            **fields,
            "scope_kind": "workspace",
            "kind": "chat_meta",
            "session_id": 1,
            "chat_id": 3,
            "turn_id": None,
        },
    )
    data["chats"][0]["content_meta_publication_id"] = 7
    _validate_associations(data)
    data["publications"][0]["service_unit_key"] = b"service-B"
    with pytest.raises(IntegrityError, match="service mismatch"):
        _validate_associations(data)


async def test_backup_rejects_share_linked_to_foreign_service_publication(
    tmp_path,
):
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.backup import ConversationBackup
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    class ForeignShareDatabase:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetch_all(self, query, params):
            return []

        async def fetch_one(self, query, params=None):
            if "transaction_isolation" in query:
                return {"level": "REPEATABLE-READ"}
            if "p.service_unit_key<>r.service_unit_key" in query:
                return {"token": "foreign-reference"}
            return None

    service = ConversationBackup(
        ForeignShareDatabase(),
        BundleStore(tmp_path),
        WorkspaceScope("t", "s", "a", service_unit_id="service-A"),
    )
    with pytest.raises(IntegrityError, match="publication locator"):
        await service._snapshot()


async def test_foreign_backup_release_rejects_before_bundle_inspection(
    tmp_path,
    monkeypatch,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage import backup
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    path = tmp_path / "foreign"
    path.mkdir()
    backup._atomic_json(
        path / "metadata.json",
        {
            "version": 2,
            "service_unit_id": "B",
            "backup_id": "job",
            "scope": [b"B", b"t", b"s", b"a"],
            "snapshot": {name: [] for name in backup.TABLES},
            "pin_keys": [],
        },
    )
    inspect_backup = Mock(side_effect=AssertionError("foreign NAS inspected"))
    monkeypatch.setattr(backup, "validate_backup", inspect_backup)
    service = backup.ConversationBackup(
        SimpleNamespace(),
        BundleStore(tmp_path / "nas"),
        WorkspaceScope("t", "s", "a", service_unit_id="A"),
    )
    with pytest.raises(IntegrityError, match="scope differs"):
        await service.release(path)
    inspect_backup.assert_not_called()
