# -*- coding: utf-8 -*-
"""Publication faults on the current adapter in generated disposable schemas."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import ConflictError, UnknownCommit
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationRequest,
    PublicationStore,
)
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_row,
    insert_session,
)


class DatabaseProxy:
    def __init__(self, database):
        self.database = database

    def __getattr__(self, name):
        return getattr(self.database, name)


class LostCommitDatabase(DatabaseProxy):
    """Lose a transport response only after the real adapter committed."""

    lose_next_commit = False
    transaction_depth = 0

    @asynccontextmanager
    async def transaction(self):
        self.transaction_depth += 1
        try:
            async with self.database.transaction() as session:
                yield session
        finally:
            self.transaction_depth -= 1
        if self.lose_next_commit and self.transaction_depth == 0:
            self.lose_next_commit = False
            raise ConnectionError(
                "injected response loss after confirmed server commit",
            )


class InstallFailureDatabase(DatabaseProxy):
    async def execute(self, query, params=None):
        if "SET state='committed'" in query:
            raise ConnectionError("injected publication installation failure")
        return await self.database.execute(query, params)


class PausedInsertDatabase(DatabaseProxy):
    def __init__(self, database):
        super().__init__(database)
        self.inserted = asyncio.Event()
        self.release = asyncio.Event()

    async def insert(self, query, params=None):
        result = await self.database.insert(query, params)
        self.inserted.set()
        await self.release.wait()
        return result


class PausedExecuteDatabase(DatabaseProxy):
    def __init__(self, database, marker):
        super().__init__(database)
        self.marker = marker
        self.executed = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, query, params=None):
        result = await self.database.execute(query, params)
        if self.marker in query:
            self.executed.set()
            await self.release.wait()
        return result


async def active_request(db):
    session = await insert_session(db, generation=1, owner_key=b"owner")
    chat = await insert_chat(db, session)
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "chat_id": chat,
            "session_id": session,
            "question_msg_key": b"question",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 1,
            "state": "running",
        },
    )
    await db.execute(
        "UPDATE swe_conversation_sessions SET active_turn_id=%s,lease_until=UTC_TIMESTAMP(6)+INTERVAL 1 HOUR WHERE id=%s",
        (turn, session),
    )
    return PublicationRequest(
        new_operation_key(),
        WorkspaceScope("tenant", "source", "agent"),
        "state",
        "owner",
        1,
        session_id=session,
        chat_id=chat,
        turn_id=turn,
        base_state_revision=0,
        base_history_revision=0,
        epoch=0,
    )


async def prepared_root(db, root):
    operation = await active_request(db)
    store = PublicationStore(db, BundleStore(root))
    await store.reserve(operation, CommitPlan(recovery_block="root"))
    index = await store.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b'{"unknown_module":true}')],
    )
    return operation, index


@pytest.mark.parametrize("phase", ["reserve", "commit"])
async def test_lost_mysql_commit_response_keeps_one_original_operation(
    conversation_test_database,
    tmp_path,
    phase,
):
    db = conversation_test_database
    operation = await active_request(db)
    fault = LostCommitDatabase(db)
    store = PublicationStore(fault, BundleStore(tmp_path))
    if phase == "reserve":
        fault.lose_next_commit = True
        with pytest.raises(UnknownCommit):
            await store.reserve(operation, CommitPlan(recovery_block="root"))
    reserved = await store.reserve(
        operation,
        CommitPlan(recovery_block="root"),
    )
    index = await store.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    if phase == "commit":
        fault.lose_next_commit = True
        with pytest.raises(UnknownCommit):
            await store.commit(operation.operation_key)
    committed = await store.commit(operation.operation_key)
    assert committed["id"] == reserved["id"]
    assert committed["state"] == "committed"
    await store.commit(operation.operation_key)
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (operation.session_id,),
    )
    assert session["state_revision"] == 1
    assert session["recovery_publication_id"] == reserved["id"]
    assert (session["recovery_offset"], session["recovery_length"]) == (
        index.blocks["root"].offset,
        2,
    )
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS count FROM swe_conversation_publications",
        )
    )["count"] == 1


async def test_mysql_failed_install_rolls_back_root_and_publication(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    operation, _ = await prepared_root(db, tmp_path)
    faulty = PublicationStore(
        InstallFailureDatabase(db),
        BundleStore(tmp_path),
    )
    with pytest.raises(UnknownCommit):
        await faulty.commit(operation.operation_key)
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (operation.session_id,),
    )
    assert session["state_revision"] == 0
    assert session["recovery_publication_id"] is None
    good = PublicationStore(db, BundleStore(tmp_path))
    assert (await good.get(operation.operation_key))["state"] == "prepared"
    assert (await good.commit(operation.operation_key))["state"] == "committed"


async def test_mysql_missing_reservation_waits_for_original_insert_arbitration(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    operation = PublicationRequest(
        new_operation_key(),
        WorkspaceScope("tenant", "source", "agent"),
        "state",
        "stopped-owner",
        0,
    )
    original = PausedInsertDatabase(db)
    writer = PublicationStore(original, BundleStore(tmp_path))
    reconciler = PublicationStore(db, BundleStore(tmp_path))
    reservation = asyncio.create_task(writer.reserve(operation, CommitPlan()))
    rejection = None
    try:
        await asyncio.wait_for(original.inserted.wait(), 5)
        # The original INSERT is still uncommitted, so a normal read sees no row.
        assert await reconciler.get(operation.operation_key) is None
        rejection = asyncio.create_task(
            reconciler.abort(operation, owner_stopped=True),
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(rejection), 0.1)
    finally:
        original.release.set()
    try:
        first, aborted = await asyncio.gather(reservation, rejection)
        assert first["id"] == aborted["id"]
        assert aborted["state"] == "aborted"
        assert (
            await db.fetch_one(
                "SELECT COUNT(*) AS count FROM swe_conversation_publications",
            )
        )["count"] == 1
        with pytest.raises(ConflictError, match="aborted"):
            await writer.reserve(operation, CommitPlan())
    finally:
        if rejection is not None and not rejection.done():
            rejection.cancel()


@pytest.mark.parametrize("winner", ["writer", "abort"])
async def test_mysql_writer_abort_serialize_on_same_lock_order(
    conversation_test_database,
    tmp_path,
    winner,
):
    db = conversation_test_database
    operation, _ = await prepared_root(db, tmp_path)
    marker = (
        "SET state='committed'"
        if winner == "writer"
        else "generation=generation+1"
    )
    paused = PausedExecuteDatabase(db, marker)
    leader = PublicationStore(paused, BundleStore(tmp_path))
    follower = PublicationStore(db, BundleStore(tmp_path))
    first = asyncio.create_task(
        (
            leader.commit(operation.operation_key)
            if winner == "writer"
            else leader.abort(operation, owner_stopped=True)
        ),
    )
    second = None
    try:
        await asyncio.wait_for(paused.executed.wait(), 5)
        second = asyncio.create_task(
            (
                follower.abort(operation, owner_stopped=True)
                if winner == "writer"
                else follower.commit(operation.operation_key)
            ),
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.1)
    finally:
        paused.release.set()
    await first
    if winner == "writer":
        assert (await second)["state"] == "committed"
    else:
        with pytest.raises(ConflictError, match="aborted"):
            await second
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (operation.session_id,),
    )
    assert session["state_revision"] == (1 if winner == "writer" else 0)
    assert session["generation"] == (1 if winner == "writer" else 2)
