# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    UnknownCommit,
)
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.chat_mutations import ChatMetadataInstall
from swe.app.conversation_storage.message_installs import (
    MessageInstall,
    TurnInstall,
)
from swe.app.conversation_storage.publications import (
    PublicationRequest,
    PublicationStore,
    CommitPlan,
)
from swe.app.conversation_storage.retention import RetentionStore


class PublicationDatabase:
    """A stateful transaction double to inject lost responses after commit."""

    def __init__(self):
        self.rows = {}
        self.queries = []
        self.lose_commit = False
        self.fail_read = False
        self.fail_commit_install = False
        self.guard = None
        self.transaction_depth = 0
        self.session = {
            "id": 1,
            "tenant_key": b"t",
            "source_key": b"s",
            "agent_key": b"a",
            "generation": 1,
            "owner_key": b"owner",
            "lease_valid": 1,
            "active_turn_id": 3,
            "state_revision": 0,
            "history_high": 0,
            "context_epoch": 0,
        }
        self.chat = {
            "id": 2,
            "session_id": 1,
            "tenant_key": b"t",
            "source_key": b"s",
            "agent_key": b"a",
            "history_revision": 0,
            "epoch": 0,
            "history_high": 0,
            "deleted_at": None,
        }
        self.turn = {
            "id": 3,
            "session_id": 1,
            "chat_id": 2,
            "generation": 1,
            "state": "running",
        }
        self.messages = []

    @asynccontextmanager
    async def transaction(self):
        self.transaction_depth += 1
        before = deepcopy(
            (self.rows, self.session, self.chat, self.turn, self.messages),
        )
        try:
            yield self
        except BaseException:
            self.rows, self.session, self.chat, self.turn, self.messages = (
                before
            )
            raise
        finally:
            self.transaction_depth -= 1
        if self.lose_commit and self.transaction_depth == 0:
            self.lose_commit = False
            raise ConnectionError("commit response lost")

    async def fetch_one(self, query, params=None):
        self.queries.append(query)
        if self.fail_read:
            raise ConnectionError("primary unavailable")
        if "swe_conversation_retentions" in query:
            return deepcopy(self.guard)
        if "swe_conversation_sessions" in query:
            return deepcopy(self.session)
        if "swe_conversation_chats" in query:
            return deepcopy(self.chat)
        if "swe_conversation_turns" in query:
            return deepcopy(self.turn)
        if "operation_key" in query:
            return deepcopy(self.rows.get(params[0]))
        return next(
            (
                deepcopy(row)
                for row in self.rows.values()
                if row["id"] == params[0]
            ),
            None,
        )

    async def insert(self, query, params=None):
        self.queries.append(query)
        if "swe_conversation_retentions" in query:
            columns = query.split("(", 1)[1].split(")", 1)[0].split(",")
            self.guard = dict(
                zip((column.strip() for column in columns), params),
            )
            self.guard.update(id=1, state="active")
            return 1
        if "swe_conversation_publications" in query:
            columns = query.split("(", 1)[1].split(")", 1)[0].split(",")
            row = dict(zip((column.strip() for column in columns), params))
            key = row["operation_key"]
            if key not in self.rows:
                row.update(
                    id=len(self.rows) + 1,
                    gc_state="live",
                    byte_length=None,
                )
                self.rows[key] = row
            return self.rows[key]["id"]
        self.messages.append(params)
        return len(self.messages)

    async def fetch_all(self, query, params=None):
        self.queries.append(query)
        assert "backup_fence" in query
        return []

    async def execute(self, query, params=None):
        self.queries.append(query)
        if "swe_conversation_publications" in query:
            publication_id = params[-1]
            row = next(
                row
                for row in self.rows.values()
                if row["id"] == publication_id
            )
            if "state='committed'" in query:
                if self.fail_commit_install:
                    raise ConnectionError("publication install failed")
                row["state"] = "committed"
            elif "state='aborted'" in query:
                row["state"] = "aborted"
            else:
                row["byte_length"] = params[0]
                row["durable_at"] = datetime.now()
        elif "generation=generation+1" in query:
            self.session["generation"] += 1
            self.session["owner_key"] = None
            self.session["lease_valid"] = 0
        elif "recovery_publication_id=" in query:
            self.session.update(
                recovery_publication_id=params[0],
                recovery_offset=params[1],
                recovery_length=params[2],
            )
            self.session["state_revision"] += 1
        elif "state='settling'" in query:
            self.turn["state"] = "settling"
        return 1


def test_commit_plan_rejects_duplicate_version_field():
    with pytest.raises(IntegrityError, match="plan"):
        CommitPlan.parse(
            '{"version":99,"version":1,"recovery_block":null,"result_ids":[]}',
        )


def request(**values):
    return PublicationRequest(
        operation_key=values.pop("operation_key", new_operation_key()),
        scope=WorkspaceScope("t", "s", "a"),
        kind="state",
        owner_key="owner",
        generation=1,
        session_id=1,
        chat_id=2,
        turn_id=3,
        base_state_revision=0,
        base_history_revision=0,
        epoch=0,
        **values,
    )


async def test_lost_reservation_commit_reuses_original_operation(tmp_path):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    db.lose_commit = True
    with pytest.raises(UnknownCommit) as failure:
        await service.reserve(operation, CommitPlan())
    assert failure.value.operation_key == operation.operation_key
    result = await service.reserve(operation, CommitPlan())
    assert result["id"] == 1
    assert len(db.rows) == 1
    assert result["boundary_key"] is None
    assert result["checkpoint_revision"] is None


async def test_durable_nas_precedes_visibility_and_lost_final_commit_is_idempotent(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    with pytest.raises(ConflictError, match="durable"):
        await service.commit(operation.operation_key)
    index = await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b'{"custom":true}')],
    )
    assert db.rows[operation.operation_key.encode()]["state"] == "prepared"
    db.lose_commit = True
    with pytest.raises(UnknownCommit):
        await service.commit(operation.operation_key)
    committed = await service.commit(operation.operation_key)
    assert committed["state"] == "committed"
    assert len(db.rows) == 1
    assert db.session["recovery_publication_id"] == committed["id"]
    assert db.session["recovery_offset"] == index.blocks["root"].offset
    assert db.session["recovery_length"] == index.blocks["root"].length
    assert db.session["state_revision"] == 1
    await service.commit(operation.operation_key)
    assert db.session["state_revision"] == 1


@pytest.mark.parametrize("reconcile", [False, True])
async def test_backup_fence_blocks_before_session_locks(
    tmp_path,
    monkeypatch,
    reconcile,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan())
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("body", "message", b"{}")],
    )
    db.queries.clear()

    async def fenced(_):
        assert not any(
            "swe_conversation_sessions" in query for query in db.queries
        )
        raise ConflictError("backup fence")

    monkeypatch.setattr(RetentionStore, "assert_writes_allowed", fenced)
    with pytest.raises(ConflictError, match="backup fence"):
        if reconcile:
            await service.reconcile(
                operation.operation_key,
                owner_stopped=True,
                expected_generation=1,
            )
        else:
            await service.commit(operation.operation_key)
    assert (await service.get(operation.operation_key))["state"] == "prepared"


@pytest.mark.parametrize("phase", ["reserve", "durable"])
async def test_backup_fence_blocks_prepared_and_durable_metadata(
    tmp_path,
    monkeypatch,
    phase,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    if phase == "durable":
        await service.reserve(operation, CommitPlan())

    async def fenced(_):
        raise ConflictError("backup fence")

    monkeypatch.setattr(RetentionStore, "assert_writes_allowed", fenced)
    with pytest.raises(ConflictError, match="backup fence"):
        if phase == "reserve":
            await service.reserve(operation, CommitPlan())
        else:
            await service.write_bundle(
                operation.operation_key,
                [BundleBlock("body", "message", b"{}")],
            )


async def test_same_operation_cannot_change_plan_or_payload_identity(tmp_path):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan())
    with pytest.raises(ConflictError):
        await service.reserve(operation, CommitPlan(recovery_block="root"))


async def test_old_generation_cannot_commit_or_remove_its_bundle(tmp_path):
    db = PublicationDatabase()
    store = BundleStore(tmp_path)
    service = PublicationStore(db, store)
    operation = request()
    await service.reserve(operation, CommitPlan())
    index = await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    db.session["generation"] += 1
    with pytest.raises(ConflictError, match="authority"):
        await service.commit(operation.operation_key)
    assert (tmp_path / index.relative_path).exists()
    assert db.rows[operation.operation_key.encode()]["state"] == "prepared"


async def test_reconcile_missing_reservation_installs_permanent_aborted_key(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    result = await service.abort(operation, owner_stopped=True)
    assert result["state"] == "aborted"
    with pytest.raises(ConflictError, match="aborted"):
        await service.reserve(operation, CommitPlan())
    assert db.session["generation"] == 2


async def test_abort_requires_observed_owner_stop_and_primary_availability(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    with pytest.raises(ConflictError, match="owner"):
        await service.abort(operation, owner_stopped=False)
    db.fail_read = True
    with pytest.raises(UnknownCommit):
        await service.abort(operation, owner_stopped=True)
    assert not db.rows


async def test_commit_locks_guard_session_chat_turn_publication_in_order(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan())
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    db.queries.clear()
    await service.commit(operation.operation_key)
    locks = [query for query in db.queries if "FOR UPDATE" in query]
    assert [query.split("FROM ")[1].split()[0] for query in locks] == [
        "swe_conversation_retentions",
        "swe_conversation_retentions",
        "swe_conversation_retentions",
        "swe_conversation_sessions",
        "swe_conversation_chats",
        "swe_conversation_turns",
        "swe_conversation_publications",
    ]


async def test_existing_bundle_needs_explicit_durability_confirmation(
    tmp_path,
):
    db = PublicationDatabase()
    store = BundleStore(tmp_path)
    service = PublicationStore(db, store)
    operation = request()
    await service.reserve(operation, CommitPlan())
    original = store.publish(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"original")],
        metadata={"operation_key": operation.operation_key},
    )
    # This represents a published file whose response/directory fsync was lost.
    confirmed = await service.confirm_bundle(operation.operation_key)
    assert confirmed == original
    assert (
        db.rows[operation.operation_key.encode()]["byte_length"]
        == original.byte_length
    )
    assert (await service.commit(operation.operation_key))[
        "state"
    ] == "committed"


async def test_cannot_abort_with_a_forged_scope_even_if_owner_is_stopped(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    object.__setattr__(operation, "scope", WorkspaceScope("other", "s", "a"))
    with pytest.raises(IntegrityError, match="scope"):
        await service.abort(operation, owner_stopped=True)
    assert not db.rows


async def test_recovery_publication_without_turn_cannot_bypass_active_authority(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    object.__setattr__(operation, "turn_id", None)
    object.__setattr__(operation, "generation", 0)
    object.__setattr__(operation, "base_state_revision", None)
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    with pytest.raises(ConflictError, match="authority"):
        await service.commit(operation.operation_key)
    assert db.rows[operation.operation_key.encode()]["state"] == "prepared"


async def test_cancellation_waits_for_nas_writer_before_reporting_unknown(
    tmp_path,
):
    db = PublicationDatabase()
    bundles = BundleStore(tmp_path)
    service = PublicationStore(db, bundles)
    operation = request()
    await service.reserve(operation, CommitPlan())
    entered, release, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    publish = bundles.publish

    def blocked_publish(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return publish(*args, **kwargs)
        finally:
            finished.set()

    bundles.publish = blocked_publish
    task = asyncio.create_task(
        service.write_bundle(
            operation.operation_key,
            [BundleBlock("root", "recovery", b"{}")],
        ),
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    was_pending = not task.done()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(UnknownCommit) as error:
        await task
    assert was_pending
    assert finished.is_set()
    assert error.value.operation_key == operation.operation_key
    assert (tmp_path / bundles.path_for(operation.operation_key)).exists()
    assert not db.rows[operation.operation_key.encode()].get("durable_at")
    await service.confirm_bundle(operation.operation_key)


async def test_failed_install_rolls_back_root_and_publication(tmp_path):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    db.fail_commit_install = True
    with pytest.raises(UnknownCommit):
        await service.commit(operation.operation_key)
    assert db.session["state_revision"] == 0
    assert "recovery_publication_id" not in db.session
    assert db.rows[operation.operation_key.encode()]["state"] == "prepared"


@pytest.mark.parametrize("missing", ["session", "chat", "turn"])
async def test_abort_rejects_missing_associations_before_fencing(
    tmp_path,
    missing,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    setattr(db, missing, None)
    with pytest.raises(IntegrityError, match="association"):
        await service.abort(request(), owner_stopped=True)
    assert not db.rows


async def test_explicit_reconciliation_fences_execution_and_installs_original_root(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    with pytest.raises(ConflictError, match="stopped"):
        await service.reconcile(
            operation.operation_key,
            owner_stopped=False,
            expected_generation=1,
        )
    row = await service.reconcile(
        operation.operation_key,
        owner_stopped=True,
        expected_generation=1,
    )
    assert row["state"] == "committed"
    assert db.session["generation"] == 2
    assert db.session["owner_key"] is None
    assert db.turn["state"] == "settling"
    assert db.session["state_revision"] == 1
    await service.reconcile(
        operation.operation_key,
        owner_stopped=True,
        expected_generation=1,
    )
    assert db.session["generation"] == 2
    assert db.session["state_revision"] == 1


async def test_reconciliation_rejects_generation_or_state_progress(tmp_path):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    with pytest.raises(ConflictError, match="generation"):
        await service.reconcile(
            operation.operation_key,
            owner_stopped=True,
            expected_generation=2,
        )
    db.session["state_revision"] = 1
    with pytest.raises(ConflictError, match="revision"):
        await service.reconcile(
            operation.operation_key,
            owner_stopped=True,
            expected_generation=1,
        )
    assert db.session["generation"] == 1


async def test_only_controlled_reconciliation_can_install_fenced_generation(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    db.session.update(generation=2, owner_key=None, lease_valid=0)
    with pytest.raises(ConflictError, match="authority"):
        await service.commit(operation.operation_key)
    result = await service.reconcile(
        operation.operation_key,
        owner_stopped=True,
        expected_generation=2,
    )
    assert result["state"] == "committed"
    assert db.session["generation"] == 3
    assert db.session["owner_key"] is None
    assert db.turn["state"] == "settling"


@pytest.mark.parametrize("generation", [1, 2])
async def test_reconciliation_never_fences_another_owner(tmp_path, generation):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    db.session.update(generation=generation, owner_key=b"different-owner")
    with pytest.raises(ConflictError, match="owner"):
        await service.reconcile(
            operation.operation_key,
            owner_stopped=True,
            expected_generation=generation,
        )
    assert db.session["owner_key"] == b"different-owner"


def metadata_plan():
    return CommitPlan(
        chat_metadata=ChatMetadataInstall(
            external_chat_key="chat",
            user_id="alice",
            channel="console",
            title="title",
            metadata_text="{}",
            created_at="2026-10-30T00:00:00Z",
            updated_at="2026-10-30T00:00:00Z",
            expected_metadata_revision=0,
        ),
    )


async def test_metadata_reconciliation_confirms_original_operation_and_is_idempotent(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = replace(request(), kind="chat_metadata", turn_id=None)
    await service.reserve(operation, metadata_plan())
    committed = {
        **await service.get(operation.operation_key),
        "state": "committed",
    }
    service.confirm_bundle = AsyncMock()
    service.commit = AsyncMock(return_value=committed)
    result = await service.reconcile_metadata(
        operation.operation_key,
        owner_stopped=True,
    )
    assert result is committed
    service.confirm_bundle.assert_awaited_once_with(operation.operation_key)
    service.commit.assert_awaited_once_with(operation.operation_key)
    with pytest.raises(ConflictError, match="owner"):
        await service.reconcile_metadata(
            operation.operation_key,
            owner_stopped=False,
        )
    db.rows[operation.operation_key.encode()]["state"] = "committed"
    assert (
        await service.reconcile_metadata(
            operation.operation_key,
            owner_stopped=True,
        )
    )["state"] == "committed"
    assert service.confirm_bundle.await_count == 1


async def test_metadata_reconciliation_conflict_aborts_only_original_operation(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = replace(request(), kind="chat_metadata", turn_id=None)
    await service.reserve(operation, metadata_plan())
    service.confirm_bundle = AsyncMock()
    service.commit = AsyncMock(side_effect=ConflictError("metadata conflict"))
    service.abort = AsyncMock(return_value={"state": "aborted"})
    with pytest.raises(ConflictError):
        await service.reconcile_metadata(
            operation.operation_key,
            owner_stopped=True,
        )
    service.abort.assert_awaited_once()
    assert (
        service.abort.await_args.args[0].operation_key
        == operation.operation_key
    )
    assert db.session["generation"] == 1 and db.session["active_turn_id"] == 3


async def test_metadata_reconciliation_missing_body_keeps_original_prepared(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = replace(request(), kind="chat_metadata", turn_id=None)
    await service.reserve(operation, metadata_plan())
    with pytest.raises(FileNotFoundError):
        await service.reconcile_metadata(
            operation.operation_key,
            owner_stopped=True,
        )
    assert len(db.rows) == 1
    assert (await service.get(operation.operation_key))["state"] == "prepared"


async def test_fenced_terminal_reconciliation_installs_original_message_and_root(
    tmp_path,
):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    plan = CommitPlan(
        recovery_block="root",
        messages=(
            MessageInstall(
                "answer",
                "memory",
                "assistant",
                source_msg_key="answer",
                turn_id=3,
            ),
        ),
        turn_install=TurnInstall("terminal", history_status="completed"),
    )
    await service.reserve(operation, plan)
    await service.write_bundle(
        operation.operation_key,
        [
            BundleBlock("root", "recovery", b"{}"),
            BundleBlock(
                "answer",
                "message",
                b'{"id":"answer","role":"assistant","content":"done"}',
            ),
        ],
    )
    result = await service.reconcile(
        operation.operation_key,
        owner_stopped=True,
        expected_generation=1,
    )
    assert result["state"] == "committed"
    assert len(db.messages) == 1
    assert db.session["recovery_publication_id"] == result["id"]
    assert db.session["state_revision"] == 1


async def test_commit_preserves_imported_committed_at(tmp_path):
    db = PublicationDatabase()
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"{}")],
    )
    db.rows[operation.operation_key.encode()]["committed_at"] = "imported"
    await service.commit(operation.operation_key)
    assert (
        db.rows[operation.operation_key.encode()]["committed_at"] == "imported"
    )
