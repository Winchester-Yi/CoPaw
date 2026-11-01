# -*- coding: utf-8 -*-
"""Durability failures must leave SQL roots unpublished on the real adapter."""

import errno
import os
import stat

import pytest

from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
from swe.app.conversation_storage.errors import IntegrityError, UnknownCommit
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationStore,
)
from tests.integrated.conversation_storage.test_publications_mysql import (
    active_request,
)


@pytest.mark.parametrize("directory", [False, True])
async def test_fsync_failure_never_installs_sql_root(
    conversation_test_database,
    tmp_path,
    monkeypatch,
    directory,
):
    db = conversation_test_database
    request = await active_request(db)
    bundles = BundleStore(tmp_path)
    store = PublicationStore(db, bundles)
    await store.reserve(request, CommitPlan(recovery_block="root"))
    bundles.prepare_directory(request.operation_key)
    original = os.fsync
    calls = 0

    def fail(descriptor):
        nonlocal calls
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        calls += int(is_directory)
        if (directory and is_directory and calls == 3) or (
            not directory and not is_directory
        ):
            raise OSError(errno.EIO, "injected durability failure")
        return original(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail)
        with pytest.raises(OSError):
            await store.write_bundle(
                request.operation_key,
                [BundleBlock("root", "recovery", b"{}")],
            )
    session = await db.fetch_one(
        "SELECT recovery_publication_id,state_revision FROM swe_conversation_sessions WHERE id=%s",
        (request.session_id,),
    )
    assert session["recovery_publication_id"] is None
    assert session["state_revision"] == 0
    assert (await store.get(request.operation_key))["state"] == "prepared"
    if directory:
        # The exclusive final file survived. Reconcile only the original key.
        await store.confirm_bundle(request.operation_key)
        assert (await store.commit(request.operation_key))[
            "state"
        ] == "committed"


@pytest.mark.parametrize("fault", ["missing", "truncated"])
async def test_invalid_body_cannot_commit_metadata(
    conversation_test_database,
    tmp_path,
    fault,
):
    db = conversation_test_database
    request = await active_request(db)
    store = PublicationStore(db, BundleStore(tmp_path))
    await store.reserve(request, CommitPlan(recovery_block="root"))
    index = await store.write_bundle(
        request.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    path = tmp_path / index.relative_path
    if fault == "missing":
        path.unlink()
    else:
        path.write_bytes(b"truncated")
    with pytest.raises((IntegrityError, UnknownCommit)):
        await store.commit(request.operation_key)
    assert (await store.get(request.operation_key))["state"] == "prepared"
    session = await db.fetch_one(
        "SELECT recovery_publication_id,state_revision FROM swe_conversation_sessions WHERE id=%s",
        (request.session_id,),
    )
    assert session["recovery_publication_id"] is None
    assert session["state_revision"] == 0
