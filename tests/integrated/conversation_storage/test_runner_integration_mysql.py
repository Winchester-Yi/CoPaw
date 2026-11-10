# -*- coding: utf-8 -*-
"""Observe durable settlement after the request-owned execution has closed."""

import asyncio
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.bundle import new_operation_key
from swe.app.conversation_storage.factory import WorkspaceConversationStorage
from swe.app.conversation_storage.runner_integration import IndexedQuery
from swe.app.conversation_storage.session_adapter import (
    DatabaseSessionExecution,
)
from tests.integrated.conversation_storage.test_execution_mysql import (
    admit,
    identity,
    seed,
)


async def test_confirmed_terminal_clears_unknown_without_replaying_closed_execution(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    storage = WorkspaceConversationStorage(
        db,
        identity().scope,
        tmp_path,
        cursor_secret=b"secret",
    )
    admission = await admit(storage.executions)
    await storage.executions.start(admission.authority)
    operation = new_operation_key()
    await storage.executions.finish(
        admission.authority,
        "completed",
        {},
        operation_key=operation,
    )
    execution = DatabaseSessionExecution(
        storage.session_for("console"),
        identity(),
    )
    execution.pending_operation = operation
    query = IndexedQuery(
        storage,
        SimpleNamespace(),
        SimpleNamespace(channel="console"),
        execution,
    )
    query.authority = admission.authority
    query.pending_operation = operation
    before = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_conversation_publications",
    )
    await query.finish("failed")
    assert query.terminal_status == "completed"
    assert query.pending_operation is execution.pending_operation is None
    assert not execution.is_active
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
        == before
    )


async def test_ended_request_settles_original_prepared_terminal(
    conversation_test_database,
    tmp_path,
):
    import pytest
    from swe.app.conversation_storage.errors import UnknownCommit

    db = conversation_test_database
    await seed(db)
    storage = WorkspaceConversationStorage(
        db,
        identity().scope,
        tmp_path,
        cursor_secret=b"secret",
    )
    admission = await admit(storage.executions)
    await storage.executions.start(admission.authority)
    execution = DatabaseSessionExecution(
        storage.session_for("console"),
        identity(),
    )
    await execution.__aenter__()
    query = IndexedQuery(
        storage,
        SimpleNamespace(),
        SimpleNamespace(channel="console"),
        execution,
    )
    query.authority = admission.authority
    query.question_key = "question"
    original = storage.publications.commit

    async def unavailable(operation):
        raise UnknownCommit(operation)

    storage.publications.commit = unavailable
    with pytest.raises(UnknownCommit):
        await query.finish("completed")
    operation = query.pending_operation
    storage.publications.commit = original
    await execution.__aexit__(None, None, None)
    query.request_ended = True
    await query.finish("failed")
    assert query.terminal_status == "completed"
    assert (await storage.publications.get(operation))["state"] == "committed"
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"] == 2
    assert (
        await db.fetch_one(
            "SELECT active_turn_id,owner_key FROM swe_conversation_sessions",
        )
    ) == {"active_turn_id": None, "owner_key": None}


async def query_fixture(db, tmp_path):
    from agentscope.message import Msg
    from unittest.mock import AsyncMock

    await seed(db)
    storage = WorkspaceConversationStorage(
        db,
        identity().scope,
        tmp_path,
        cursor_secret=b"secret",
    )
    adapter = storage.session_for("console")
    execution = DatabaseSessionExecution(adapter, identity())
    await execution.__aenter__()
    execution._state["future_module"] = {"nested": [1, 2]}
    execution._state["managed_cron_receipts"] = {"receipt": True}
    owner = SimpleNamespace(
        _get_or_create_chat=AsyncMock(return_value=SimpleNamespace(id="one")),
        _answer_turn_identity=lambda request: "identity",
        _answer_turn_runtimes={},
    )
    request = SimpleNamespace(
        channel="console",
        session_id="session",
        user_id="alice",
        channel_meta={},
    )
    query = IndexedQuery(storage, owner, request, execution)
    question = Msg("user", "hello", "user")
    question.id = "question"
    return storage, execution, owner, query, question


@pytest.mark.parametrize(
    "lost",
    ["reservation", "reservation_absent", "admission", "admission_prepared"],
)
async def test_unknown_admission_retains_registry_and_settles_without_model_work(
    conversation_test_database,
    tmp_path,
    lost,
):
    from swe.app.conversation_storage.errors import UnknownCommit

    db = conversation_test_database
    storage, execution, owner, query, question = await query_fixture(
        db,
        tmp_path,
    )
    service = storage.executions
    name = (
        "reserve_admission"
        if lost.startswith("reservation")
        else "publish_admission"
    )
    original = getattr(service, name)

    async def response_lost(*args, **kwargs):
        if lost == "reservation_absent":
            raise UnknownCommit(kwargs["operation_key"])
        if lost == "admission_prepared":
            commit = storage.publications.commit

            async def unavailable(operation):
                raise UnknownCommit(operation)

            storage.publications.commit = unavailable
            try:
                await original(*args, **kwargs)
            finally:
                storage.publications.commit = commit
        result = await original(*args, **kwargs)
        operation = (
            result.publication["operation_key"].decode()
            if lost == "reservation"
            else args[0].publication["operation_key"].decode()
        )
        raise UnknownCommit(operation)

    setattr(service, name, response_lost)
    with pytest.raises(UnknownCommit):
        async with query.admit([question]):
            pytest.fail(
                "model work must not start before admission confirmation",
            )
    setattr(service, name, original)
    assert owner._answer_turn_runtimes["identity"] == (None, execution)
    assert execution.indexed_query is query and query.request_ended
    await execution.__aexit__(None, None, None)
    await query.finish("failed")
    turn = await db.fetch_one(
        "SELECT state,history_status FROM swe_conversation_turns",
    )
    if lost.startswith("reservation"):
        assert (
            query.terminal_status == "rejected" and turn["state"] == "rejected"
        )
    else:
        assert query.terminal_status == "interrupted"
        assert turn == {
            "state": "interrupted",
            "history_status": "interrupted",
        }
        state = await storage.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        assert state["future_module"] == {"nested": [1, 2]}
        assert state["managed_cron_receipts"] == {"receipt": True}
    assert (
        await db.fetch_one(
            "SELECT active_turn_id,owner_key FROM swe_conversation_sessions",
        )
    ) == {"active_turn_id": None, "owner_key": None}
    duplicate = await service.reserve_admission(
        identity(),
        "one",
        "question",
        operation_key=new_operation_key(),
        runtime_turn_key="retry",
    )
    assert not duplicate.created


async def test_unknown_semantic_boundary_uses_full_confirmed_root_and_interruption(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.errors import UnknownCommit

    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    adapter = storage.session_for("console")
    original = adapter.publications.commit
    calls = []
    async with query.admit([question]):
        calls.append("tool-finished")
        state = dict(execution.state)
        state["tool_results"] = {"result": "done"}

        async def response_lost(operation):
            raise UnknownCommit(operation)

        adapter.publications.commit = response_lost
        with pytest.raises(UnknownCommit):
            await execution.commit_state(state)
        adapter.publications.commit = original
    operation = execution.pending_operation
    await execution.__aexit__(None, None, None)
    await query.finish("failed")
    assert query.terminal_status == "interrupted"
    assert calls == ["tool-finished"]
    assert (await storage.publications.get(operation))["state"] == "committed"
    loaded = await adapter.get_session_state_dict("session", "alice")
    assert loaded["future_module"] == {"nested": [1, 2]}
    assert loaded["managed_cron_receipts"] == {"receipt": True}
    assert loaded["tool_results"] == {"result": "done"}
    assert loaded["turn_states"]["question"]["status"] == "interrupted"


@pytest.mark.parametrize("kind", ["terminal", "state"])
async def test_absent_original_operation_uses_retained_request_for_tombstone(
    conversation_test_database,
    tmp_path,
    kind,
):
    from swe.app.conversation_storage.errors import UnknownCommit

    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    adapter = storage.session_for("console")
    publications = (
        storage.publications if kind == "terminal" else adapter.publications
    )
    original = publications.reserve

    async def unavailable(request, plan):
        raise UnknownCommit(request.operation_key, request)

    async with query.admit([question]):
        publications.reserve = unavailable
        with pytest.raises(UnknownCommit):
            if kind == "terminal":
                await query.finish("completed")
            else:
                await execution.commit_state(dict(execution.state))
        publications.reserve = original
    operation = query.pending_operation or execution.pending_operation
    assert await storage.publications.get(operation) is None
    await execution.__aexit__(None, None, None)
    await query.finish("failed")
    assert query.terminal_status == "interrupted"
    assert (await storage.publications.get(operation))["state"] == "aborted"
    assert (
        await conversation_test_database.fetch_one(
            "SELECT active_turn_id,owner_key FROM swe_conversation_sessions",
        )
    ) == {"active_turn_id": None, "owner_key": None}
    loaded = await adapter.get_session_state_dict("session", "alice")
    assert loaded["future_module"] == {"nested": [1, 2]}


async def test_stop_wins_over_ended_unknown_completed_publication(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.errors import UnknownCommit

    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    original = storage.publications.commit
    async with query.admit([question]):

        async def unavailable(operation):
            raise UnknownCommit(operation)

        storage.publications.commit = unavailable
        with pytest.raises(UnknownCommit):
            await query.finish("completed")
        storage.publications.commit = original
        await storage.executions.request_stop(identity(), "question")
    operation = query.pending_operation
    await execution.__aexit__(None, None, None)
    await query.finish("failed")
    assert query.terminal_status == "stopped"
    publication = await storage.publications.get(operation)
    assert publication["state"] == "aborted"
    assert (
        storage.publications.bundles.inspect(
            storage.publications.bundles.validate_publication_locator(
                publication,
            ),
        ).byte_length
        > 0
    )


async def test_ended_request_cannot_release_a_changed_generation_owner(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.errors import (
        ConflictError,
        UnknownCommit,
    )

    db = conversation_test_database
    storage, execution, _, query, question = await query_fixture(db, tmp_path)
    original = storage.publications.commit
    async with query.admit([question]):

        async def unavailable(operation):
            raise UnknownCommit(operation)

        storage.publications.commit = unavailable
        with pytest.raises(UnknownCommit):
            await query.finish("completed")
        storage.publications.commit = original
    operation = query.pending_operation
    await execution.__aexit__(None, None, None)
    await db.execute(
        "UPDATE swe_conversation_sessions SET generation=generation+1,owner_key=%s",
        (b"other-owner",),
    )
    with pytest.raises(ConflictError, match="another owner"):
        await query.finish("failed")
    session = await db.fetch_one(
        "SELECT active_turn_id,owner_key FROM swe_conversation_sessions",
    )
    assert session == {
        "active_turn_id": query.authority.turn_id,
        "owner_key": b"other-owner",
    }
    assert query.terminal_status is None
    assert (await storage.publications.get(operation))["state"] == "aborted"


async def _leave_restart_phase(storage, execution, query, question, phase):
    from swe.app.conversation_storage.errors import UnknownCommit

    if phase in ("reserved", "restored_reserved"):
        original = storage.executions.publish_admission

        async def no_body(*args, **kwargs):
            raise UnknownCommit(args[0].publication["operation_key"].decode())

        storage.executions.publish_admission = no_body
        with pytest.raises(UnknownCommit):
            async with query.admit([question]):
                pytest.fail("reserved Turn cannot run")
        storage.executions.publish_admission = original
    else:
        original_admission = storage.executions.publish_admission
        if phase == "admission":

            async def admission_prepared(*args, **kwargs):
                original_commit = storage.publications.commit

                async def unavailable_admission(operation):
                    raise UnknownCommit(operation)

                storage.publications.commit = unavailable_admission
                try:
                    return await original_admission(*args, **kwargs)
                finally:
                    storage.publications.commit = original_commit

            storage.executions.publish_admission = admission_prepared
        try:
            context = query.admit([question])
            await context.__aenter__()
        except UnknownCommit:
            assert phase == "admission"
        storage.executions.publish_admission = original_admission
        if phase != "admission":
            if phase in ("state", "terminal"):
                original = storage.publications.commit

                async def unavailable(operation):
                    raise UnknownCommit(operation)

                if phase == "state":
                    adapter = storage.session_for("console")
                    adapter.publications.commit = unavailable
                    state = dict(execution.state)
                    state["tool_results"] = {"done": True}
                    with pytest.raises(UnknownCommit):
                        await execution.commit_state(state)
                    adapter.publications.commit = original
                else:
                    storage.publications.commit = unavailable
                    with pytest.raises(UnknownCommit):
                        await query.finish("completed")
                    storage.publications.commit = original
            elif phase == "stopping":
                await storage.executions.request_stop(identity(), "question")
            await context.__aexit__(None, None, None)


@pytest.mark.parametrize(
    "phase",
    [
        "reserved",
        "restored_reserved",
        "admission",
        "running",
        "state",
        "terminal",
        "stopping",
    ],
)
async def test_new_process_recovers_persisted_turn_without_original_runtime(
    conversation_test_database,
    tmp_path,
    phase,
):
    from swe.app.conversation_storage.errors import UnknownCommit
    from swe.app.conversation_storage.recovery import (
        EndedOwnerRecovery,
        OwnerExitProof,
    )

    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    await _leave_restart_phase(storage, execution, query, question, phase)
    authority = query.authority
    if phase == "restored_reserved":
        await conversation_test_database.execute(
            "UPDATE swe_conversation_sessions SET generation=generation+1,owner_key=NULL,lease_until=NULL",
        )
    proof = OwnerExitProof(
        authority.owner_key,
        authority.generation,
        "kubernetes/pod-uid-exited-and-volume-drained",
        "test-operator",
        True,
        True,
        service_unit_id="schema-test-service",
    )
    await execution.__aexit__(None, None, None)
    del execution, query
    fresh = WorkspaceConversationStorage(
        conversation_test_database,
        identity().scope,
        tmp_path,
        cursor_secret=b"secret",
    )
    recovery = EndedOwnerRecovery(
        fresh,
        verify_stopped=lambda value: value == proof,
    )
    report = await recovery.recover(identity(), authority.turn_id, proof)
    expected = {
        "reserved": "rejected",
        "restored_reserved": "rejected",
        "terminal": "succeeded",
        "stopping": "stopped",
    }.get(phase, "interrupted")
    assert report["state"] == expected
    assert (
        await conversation_test_database.fetch_one(
            "SELECT active_turn_id,owner_key FROM swe_conversation_sessions",
        )
    ) == {"active_turn_id": None, "owner_key": None}
    if phase not in ("reserved", "restored_reserved"):
        state = await fresh.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        assert state["future_module"] == {"nested": [1, 2]}
        assert state["managed_cron_receipts"] == {"receipt": True}
        if phase == "state":
            assert state["tool_results"] == {"done": True}
    count = (
        await conversation_test_database.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"]
    assert (await recovery.recover(identity(), authority.turn_id, proof))[
        "state"
    ] == expected
    assert (
        await conversation_test_database.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"] == count


async def test_restart_recovery_rejects_wrong_incarnation_and_preserves_unreadable_body(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.errors import (
        ConflictError,
        IntegrityError,
        UnknownCommit,
    )
    from swe.app.conversation_storage.recovery import (
        EndedOwnerRecovery,
        OwnerExitProof,
    )

    db = conversation_test_database
    storage, execution, _, query, question = await query_fixture(db, tmp_path)
    original = storage.publications.commit
    async with query.admit([question]):

        async def unavailable(operation):
            raise UnknownCommit(operation)

        storage.publications.commit = unavailable
        with pytest.raises(UnknownCommit):
            await query.finish("completed")
        storage.publications.commit = original
    operation = query.pending_operation
    authority = query.authority
    await execution.__aexit__(None, None, None)
    service = EndedOwnerRecovery(storage, verify_stopped=lambda value: True)
    wrong = OwnerExitProof(
        "different-owner",
        authority.generation,
        "audit/exited",
        "ops",
        True,
        True,
        service_unit_id="schema-test-service",
    )
    before = await db.fetch_one(
        "SELECT generation,owner_key,active_turn_id FROM swe_conversation_sessions",
    )
    with pytest.raises(ConflictError, match="incarnation"):
        await service.recover(identity(), authority.turn_id, wrong)
    assert (
        await db.fetch_one(
            "SELECT generation,owner_key,active_turn_id FROM swe_conversation_sessions",
        )
        == before
    )
    proof = OwnerExitProof(
        authority.owner_key,
        authority.generation,
        "audit/exited",
        "ops",
        True,
        True,
        service_unit_id="schema-test-service",
    )
    publication = await storage.publications.get(operation)
    path = storage.bundles.root / storage.bundles.validate_publication_locator(
        publication,
    )
    path.write_bytes(b"truncated")
    with pytest.raises(IntegrityError, match="truncated|length"):
        await service.recover(identity(), authority.turn_id, proof)
    assert (
        await db.fetch_one(
            "SELECT active_turn_id FROM swe_conversation_sessions",
        )
    )["active_turn_id"] == authority.turn_id
    assert (await storage.publications.get(operation))["state"] == "prepared"


async def test_recovery_cli_uses_existing_roots_and_records_audited_result(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    import json
    from swe.app.chat_sharing.store import ChatShareStore
    from swe.app.conversation_storage.recovery_cli import RecoveryJob
    from tests.integrated.conversation_storage.conftest import (
        SQL_ROOT,
        apply_sql_script,
    )

    db = conversation_test_database
    storage, execution, _, query, question = await query_fixture(db, tmp_path)
    async with query.admit([question]):
        pass
    authority = query.authority
    await execution.__aexit__(None, None, None)
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    evidence = tmp_path / "recovery-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "version": 2,
                "service_unit_id": "schema-test-service",
                "schema": db.config.database,
                "workspace_root": str(tmp_path),
                "nas_root": str(tmp_path),
                "scope": {
                    "service_unit_id": "schema-test-service",
                    "tenant_id": "tenant",
                    "source_id": "source",
                    "agent_id": "agent",
                },
                "session": {
                    "user_id": "alice",
                    "channel": "console",
                    "session_id": "session",
                },
                "turn_id": authority.turn_id,
                "owner": {
                    "owner_key": authority.owner_key,
                    "generation": authority.generation,
                    "operator": "reviewed-ops",
                    "evidence_ref": "audit/exact-pod-uid-io-drained",
                    "owner_stopped": True,
                    "io_drained": True,
                },
            },
        ),
    )
    receipt = tmp_path / "recovery.jsonl"
    result = await RecoveryJob(evidence).apply(authority.owner_key, receipt)
    assert result["state"] == "interrupted"
    records = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert [row["phase"] for row in records] == ["started", "completed"]
    assert records[0]["evidence"]["owner"]["owner_key"] == authority.owner_key
    assert (
        records[1]["result"]["evidence_ref"]
        == "audit/exact-pod-uid-io-drained"
    )
    assert (
        await db.fetch_one(
            "SELECT active_turn_id FROM swe_conversation_sessions",
        )
    )["active_turn_id"] is None


async def test_restore_stop_survives_late_prepared_completed_result(
    conversation_test_database,
    tmp_path,
):
    from swe.app.chat_sharing.store import ChatShareStore
    from swe.app.conversation_storage.backup import (
        ConversationBackup,
        RestoreEvidence,
        restore_backup,
    )
    from swe.app.conversation_storage.bundle import BundleBlock
    from swe.app.conversation_storage.message_installs import TurnInstall
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationRequest,
    )
    from swe.app.conversation_storage.recovery import (
        EndedOwnerRecovery,
        OwnerExitProof,
    )
    from tests.integrated.conversation_storage.conftest import (
        SQL_ROOT,
        apply_sql_script,
    )
    from tests.integrated.conversation_storage.test_rollback import empty_clone
    import json

    db = conversation_test_database
    source = tmp_path / "source"
    source.mkdir()
    storage, execution, _, query, question = await query_fixture(db, source)
    # Formal new Chat provisioning starts at epoch 1; seed() is a bare SQL fixture.
    await db.execute("UPDATE swe_conversation_chats SET epoch=1")
    async with query.admit([question]):
        await storage.executions.request_stop(identity(), "question")
    authority = query.authority
    await execution.__aexit__(None, None, None)
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    backup = tmp_path / "backup"
    await ConversationBackup(db, storage.bundles, identity().scope).capture(
        backup,
    )
    async with empty_clone(db) as clone:
        restored_root = tmp_path / "restored"
        bundles = storage.bundles.__class__(restored_root)
        report = await restore_backup(
            clone,
            bundles,
            backup,
            evidence=RestoreEvidence("source-exited", "isolated-target"),
        )
        assert report["needs_settlement"] and not report["restore_ready"]
        assert (
            await clone.fetch_one("SELECT state FROM swe_conversation_turns")
        )["state"] == "stopping"
        restored = WorkspaceConversationStorage(
            clone,
            identity().scope,
            restored_root,
            cursor_secret=b"secret",
        )
        session = await restored.session_for("console")._row(identity())
        chat = await clone.fetch_one(
            "SELECT * FROM swe_conversation_chats WHERE id=%s",
            (authority.chat_id,),
        )
        operation = new_operation_key()
        request = PublicationRequest(
            operation,
            identity().scope,
            "terminal",
            authority.owner_key,
            authority.generation,
            session_id=authority.session_id,
            chat_id=authority.chat_id,
            turn_id=authority.turn_id,
            base_state_revision=session["state_revision"],
            base_history_revision=chat["history_revision"],
            epoch=chat["epoch"],
        )
        await restored.publications.reserve(
            request,
            CommitPlan(
                recovery_block="recovery",
                turn_install=TurnInstall(
                    "terminal",
                    history_status="completed",
                ),
            ),
        )
        state = await restored.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        state["revision"] += 1
        state["turn_states"]["question"]["status"] = "completed"
        await restored.publications.write_bundle(
            operation,
            [BundleBlock("recovery", "recovery", json.dumps(state).encode())],
        )
        proof = OwnerExitProof(
            authority.owner_key,
            authority.generation,
            "audit/stopped-uid-drained",
            "operator",
            True,
            True,
            service_unit_id="schema-test-service",
        )
        result = await EndedOwnerRecovery(
            restored,
            verify_stopped=lambda value: value == proof,
        ).recover(identity(), authority.turn_id, proof)
        assert result["state"] == "stopped"
        assert (await restored.publications.get(operation))[
            "state"
        ] == "aborted"
        loaded = await restored.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        assert loaded["turn_states"]["question"]["status"] == "stopped"
        assert loaded["future_module"] == {"nested": [1, 2]}


async def test_terminal_owner_release_does_not_cancel_publication_cleanup(
    conversation_test_database,
    tmp_path,
):
    from unittest.mock import Mock

    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    original = storage.publications._committed_projection
    parent = SimpleNamespace(cancel=Mock())

    async def renew_during_cleanup(operation, projection, references):
        if operation == query.terminal_operation:
            # The SQL terminal is committed, but the publication caller still
            # owns cache refresh and reader release outside the transaction.
            # Keep a real SQL budget while exercising the post-commit race.
            await asyncio.wait_for(query._renew(parent), 30)
        return await original(operation, projection, references)

    storage.publications._committed_projection = renew_during_cleanup
    try:
        async with query.admit([question]):
            await query.finish("completed")
        parent.cancel.assert_not_called()
        assert query.renew_error is None
        assert query.terminal_status == "completed"
        assert (await storage.publications.get(query.terminal_operation))[
            "state"
        ] == "committed"
    finally:
        storage.publications._committed_projection = original
        await execution.__aexit__(None, None, None)
