# -*- coding: utf-8 -*-
"""Controlled GC requires explicit opt-in and preserves all live references."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import ConflictError, IntegrityError


def test_bundle_reclaim_uses_generated_exact_object_and_is_idempotent(
    tmp_path,
):
    store = BundleStore(tmp_path)
    operation = new_operation_key()
    other = new_operation_key()
    first = store.publish(operation, [BundleBlock("body", "message", b"one")])
    second = store.publish(other, [BundleBlock("body", "message", b"two")])
    assert store.reclaim(first.relative_path) is True
    assert not (tmp_path / first.relative_path).exists()
    assert (tmp_path / second.relative_path).is_file()
    assert store.reclaim(first.relative_path) is False
    with pytest.raises(IntegrityError):
        store.reclaim("../escape")


def test_bundle_reclaim_rejects_symlink_files_and_directories(tmp_path):
    store = BundleStore(tmp_path)
    operation = new_operation_key()
    relative = store.path_for(operation)
    target = tmp_path / "keep"
    target.write_bytes(b"keep")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    with pytest.raises(IntegrityError):
        store.reclaim(relative)
    assert target.read_bytes() == b"keep"
    path.unlink()
    path.mkdir()
    with pytest.raises(IntegrityError):
        store.reclaim(relative)


async def test_gc_defaults_to_report_only_and_cannot_mark_or_unlink(tmp_path):
    from swe.app.conversation_storage.gc import GarbageCollector
    from swe.app.conversation_storage.identity import WorkspaceScope

    db = SimpleNamespace(execute=AsyncMock())
    store = BundleStore(tmp_path)
    gc = GarbageCollector(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        store,
    )
    with pytest.raises(ConflictError, match="report-only"):
        await gc.mark(1)
    with pytest.raises(ConflictError, match="report-only"):
        await gc.reclaim(1)
    db.execute.assert_not_awaited()


async def test_gc_reverse_reference_checks_do_not_lock_domain_rows(tmp_path):
    from swe.app.conversation_storage.gc import GarbageCollector
    from swe.app.conversation_storage.identity import WorkspaceScope

    db = SimpleNamespace(fetch_one=AsyncMock(return_value=None))
    gc = GarbageCollector(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        BundleStore(tmp_path),
    )
    await gc._protections(
        {
            "id": 1,
            "state": "committed",
            "retained_history": False,
            "session_id": None,
            "chat_id": None,
            "turn_id": None,
        },
    )
    queries = [call.args[0] for call in db.fetch_one.await_args_list]
    assert queries
    assert all(
        "FOR SHARE" not in query and "FOR UPDATE" not in query
        for query in queries
    )


async def test_gc_rejects_foreign_service_proof_before_verifier(tmp_path):
    from swe.app.conversation_storage.gc import GarbageCollector, StopProof
    from swe.app.conversation_storage.identity import WorkspaceScope

    verifier = AsyncMock(return_value=True)
    gc = GarbageCollector(
        AsyncMock(),
        WorkspaceScope("t", "s", "a", service_unit_id="A"),
        BundleStore(tmp_path),
        verify_stopped=verifier,
    )
    proof = StopProof(
        "operation",
        "owner",
        1,
        "evidence",
        True,
        True,
        service_unit_id="B",
    )
    with pytest.raises(ConflictError):
        await gc._proof(
            {
                "state": "aborted",
                "operation_key": b"operation",
                "owner_key": b"owner",
                "generation": 1,
            },
            proof,
        )
    verifier.assert_not_awaited()


async def test_gc_foreign_publication_rejected_before_any_nas_access(tmp_path):
    from unittest.mock import Mock
    from swe.app.conversation_storage.gc import GarbageCollector
    from swe.app.conversation_storage.identity import WorkspaceScope

    db = SimpleNamespace(
        fetch_one=AsyncMock(
            return_value={
                "scope_kind": "workspace",
                "service_unit_key": b"B",
                "tenant_key": b"t",
                "source_key": b"s",
                "agent_key": b"a",
            },
        ),
    )
    bundles = Mock()
    collector = GarbageCollector(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="A"),
        bundles,
        physical_enabled=True,
    )
    with pytest.raises(IntegrityError, match="scope mismatch"):
        await collector.reclaim(1)
    bundles.path_for.assert_not_called()
    bundles.reclaim.assert_not_called()
    query, params = db.fetch_one.await_args.args
    assert "service_unit_key=%s" in query and params == (b"A", 1)


@pytest.mark.parametrize(
    "changed",
    [None, "foreign-chat", "wrong-turn-session"],
)
async def test_gc_historical_publication_allows_same_scope_chat_rebind(
    tmp_path,
    changed,
):
    from swe.app.conversation_storage.gc import GarbageCollector
    from swe.app.conversation_storage.identity import WorkspaceScope

    fields = {
        "service_unit_key": b"A",
        "tenant_key": b"t",
        "source_key": b"s",
        "agent_key": b"a",
    }
    associations = {
        1: {"id": 1, **fields},
        3: {"id": 3, **fields, "session_id": 2},
        4: {"id": 4, "service_unit_key": b"A", "session_id": 1, "chat_id": 3},
    }
    if changed == "foreign-chat":
        associations[3]["service_unit_key"] = b"B"
    if changed == "wrong-turn-session":
        associations[4]["session_id"] = 2

    async def fetch_one(query, params):
        if query.startswith("SELECT * FROM swe_conversation_"):
            return associations[params[1]]
        return None

    collector = GarbageCollector(
        SimpleNamespace(fetch_one=fetch_one),
        WorkspaceScope("t", "s", "a", service_unit_id="A"),
        BundleStore(tmp_path),
    )
    anomaly = await collector._anomaly(
        {"id": 10, "session_id": 1, "chat_id": 3, "turn_id": 4},
    )
    assert anomaly is (changed is not None)
