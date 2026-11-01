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
    assert store.reclaim(operation) is True
    assert not (tmp_path / first.relative_path).exists()
    assert (tmp_path / second.relative_path).is_file()
    assert store.reclaim(operation) is False
    with pytest.raises(ValueError):
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
        store.reclaim(operation)
    assert target.read_bytes() == b"keep"
    path.unlink()
    path.mkdir()
    with pytest.raises(IntegrityError):
        store.reclaim(operation)


async def test_gc_defaults_to_report_only_and_cannot_mark_or_unlink(tmp_path):
    from swe.app.conversation_storage.gc import GarbageCollector
    from swe.app.conversation_storage.identity import WorkspaceScope

    db = SimpleNamespace(execute=AsyncMock())
    store = BundleStore(tmp_path)
    gc = GarbageCollector(db, WorkspaceScope("t", "s", "a"), store)
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
        WorkspaceScope("t", "s", "a"),
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
