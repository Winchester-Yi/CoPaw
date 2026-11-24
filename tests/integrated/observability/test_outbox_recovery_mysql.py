# -*- coding: utf-8 -*-
"""Real MySQL lease and worker replay contract."""

import os
from uuid import uuid4

import pytest

from swe.database import DatabaseConnection
from swe.observability.outbox_recovery import (
    OutboxPage,
    OutboxRecoveryStore,
    OutboxRecoveryWorker,
)
from tests.integrated.observability.test_mysql_pipeline import _test_db_config

pytestmark = pytest.mark.skipif(
    not os.getenv("SWE_AGENTOBS_TEST_DB_HOST"),
    reason="requires disposable MySQL *_test schema",
)


@pytest.mark.asyncio
async def test_worker_crash_lease_takeover_and_inbox_ack():
    db = DatabaseConnection(_test_db_config())
    await db.connect()
    try:
        store = OutboxRecoveryStore(db, uuid4().bytes + uuid4().bytes)
        owner_a, owner_b = str(uuid4()), str(uuid4())
        assert await store.claim(owner_a, 30) is None
        assert await store.claim(owner_b, 30) is False
        await db.execute(
            "UPDATE swe_agentobs_outbox_recovery SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE scope_key=%s",
            (store.scope_key,),
        )
        assert await store.claim(owner_b, 30) is None
        assert not await store.renew(owner_a, 30)
        assert not await store.finish(owner_a, "stale")
        assert await store.finish(owner_b, None)

        event = {"event_id": str(uuid4())}

        class Scanner:
            async def scan_page(self, cursor, limit):
                assert cursor is None and limit >= 1
                return OutboxPage([event], None)

            async def verified_event(self, record):
                return record

        class Emitter:
            def __init__(self):
                self.events = []

            def emit(self, item):
                self.events.append(item)
                return True

        emitter = Emitter()
        worker = OutboxRecoveryWorker(store, Scanner(), emitter)
        # A process dies after claiming its lease and before advancing its cursor.
        assert await store.claim(owner_a, 30) is None
        assert await worker.run_once() == 0
        await db.execute(
            "UPDATE swe_agentobs_outbox_recovery SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE scope_key=%s",
            (store.scope_key,),
        )
        assert await worker.run_once() == 1
        assert await worker.run_once() == 1
        assert len(emitter.events) == 2
        await db.execute(
            "INSERT INTO swe_agentobs_inbox (event_id,processing_state) VALUES (%s,2)",
            (event["event_id"],),
        )
        assert not await store.acknowledged(event["event_id"])
        assert await worker.run_once() == 1
        await db.execute(
            "INSERT INTO swe_agentobs_inbox (event_id,processing_state) VALUES (%s,1)",
            (event["event_id"],),
        )
        assert await store.acknowledged(event["event_id"])
        assert await worker.run_once() == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rebind_rejects_changed_public_metadata_then_accepts_matching_copy(
    tmp_path,
    monkeypatch,
):
    # Both rebind outcomes share the same database and stored evidence.
    # pylint: disable=too-many-statements
    from io import StringIO
    from copy import deepcopy
    from types import SimpleNamespace
    from agentscope.message import Msg
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.runner.manager import ChatManager
    from swe.app.runner.models import ChatSpec
    from swe.app.runner.repo.json_repo import JsonChatRepository
    from swe.app.runner.session import SafeJSONSession
    from swe.observability import Scope, bind_context
    from swe.observability.emitter import JsonlEmitter
    from swe.observability.instrumentation import (
        Observability,
        bind_tool_span,
        mark_pending_message_evidence,
    )
    from swe.observability.outbox_recovery import (
        clear_registered_scopes,
        enable_registered_scopes,
        rebind_offline,
        scope_digest,
    )
    from swe.observability.outbox_scanner import WorkspaceOutboxScanner

    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
    monkeypatch.setenv("SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET", "test")
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="swe")
    chat_id = str(uuid4())
    roots = [tmp_path / "old", tmp_path / "new"]
    workspaces = []
    for root in roots:
        root.mkdir()
        manager = ChatManager(repo=JsonChatRepository(root / "chats.json"))
        await manager.create_chat(
            ChatSpec(
                id=chat_id,
                session_id="session",
                user_id="user",
                meta={"source_id": "source", "agent_id": "agent"},
            ),
        )
        workspaces.append(
            SimpleNamespace(
                workspace_dir=root,
                tenant_id="tenant",
                source_id="source",
                agent_id="agent",
                chat_manager=manager,
                runner=SimpleNamespace(
                    session=SafeJSONSession(str(root / "sessions")),
                ),
            ),
        )
    db = DatabaseConnection(_test_db_config())
    await db.connect()
    clear_registered_scopes()
    enable_registered_scopes()
    try:
        store = OutboxRecoveryStore(db, scope_digest(scope))
        await store.register_scope(scope, str(roots[0].resolve()), "legacy")
        sink = StringIO()
        emitter = JsonlEmitter(stream=sink)
        obs = Observability(emitter)
        run = obs.admit_run(
            scope=Scope("swe", "source", "tenant", "agent"),
            business_ref={"chat_id": chat_id, "session_id": "session"},
            enabled=True,
        )
        attempt = obs.start_attempt(run).with_updates(
            workspace_binding=(str(roots[0].resolve()), "legacy"),
        )
        message = Msg(
            "system",
            [
                {
                    "type": "tool_result",
                    "id": "call",
                    "name": "tool",
                    "output": [{"type": "text", "text": "private"}],
                },
            ],
            "system",
            metadata={"public_label": "original"},
        )
        with bind_context(attempt):
            span = obs.span("Tool", "execute", name="tool")
            with bind_tool_span(span):
                mark_pending_message_evidence(message)
        emitter.close(timeout=2)
        event_id = message.metadata["agent_observability_outbox"]["event"][
            "event_id"
        ]

        async def save(workspace, msg):
            await workspace.runner.session.save_merged_state(
                "session",
                user_id="user",
                state={
                    "agent": {"memory": {"content": [[msg.to_dict(), []]]}},
                },
            )

        await save(workspaces[0], message)
        changed = Msg.from_dict(deepcopy(message.to_dict()))
        changed.metadata["public_label"] = "changed"
        await save(workspaces[1], changed)
        scanners = [
            WorkspaceOutboxScanner(workspace, scope)
            for workspace in workspaces
        ]
        await db.execute(
            "INSERT INTO swe_agentobs_inbox (event_id,processing_state) VALUES (%s,1)",
            (event_id,),
        )

        async def rebind():
            return await rebind_offline(
                store,
                scope,
                *scanners,
                old_root=str(roots[0].resolve()),
                old_authority="legacy",
                new_root=str(roots[1].resolve()),
                new_authority="legacy",
                writers_stopped=True,
            )

        with pytest.raises(ValueError, match="differs"):
            await rebind()
        row = await db.fetch_one(
            "SELECT workspace_root FROM swe_agentobs_outbox_scopes WHERE scope_key=%s",
            (store.scope_key,),
        )
        assert bytes(row["workspace_root"]) == str(roots[0].resolve()).encode()
        await save(workspaces[1], message)
        await rebind()
        row = await db.fetch_one(
            "SELECT workspace_root FROM swe_agentobs_outbox_scopes WHERE scope_key=%s",
            (store.scope_key,),
        )
        assert bytes(row["workspace_root"]) == str(roots[1].resolve()).encode()
    finally:
        clear_registered_scopes()
        await db.execute(
            "DELETE FROM swe_agentobs_outbox_recovery WHERE scope_key=%s",
            (scope_digest(scope),),
        )
        await db.execute(
            "DELETE FROM swe_agentobs_outbox_scopes WHERE scope_key=%s",
            (scope_digest(scope),),
        )
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_os_kill_runner_at_session_commit_and_recover(
    tmp_path,
    monkeypatch,
    phase,
):
    # One test must observe the child process before and after its termination.
    # pylint: disable=too-many-statements
    import asyncio
    import json
    import signal
    import sys
    from io import StringIO
    from pathlib import Path
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability import outbox_runtime
    from swe.observability.emitter import JsonlEmitter
    from swe.observability.outbox_recovery import scope_digest
    from swe.observability.projector import InboxProjector
    from swe.observability.store import ObservabilityStore

    scope = WorkspaceScope(
        "default",
        "runner-integration",
        "critical-agent",
        service_unit_id="swe",
    )
    marker = tmp_path / "commit-barrier.json"
    env = os.environ.copy()
    env.update(SWE_TEST_CRASH_MARKER=str(marker), SWE_TEST_CRASH_PHASE=phase)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "tests/integrated/observability/test_real_runner_evidence_mysql.py::test_runner_tool_answer_and_question_evidence_mysql[None]",
        "-q",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(600):
            if marker.exists():
                break
            assert (
                process.returncode is None
            ), f"child exited before barrier: {process.returncode}"
            await asyncio.sleep(0.1)
        assert marker.exists(), "child did not reach commit barrier"
        barrier = json.loads(marker.read_text())
        assert barrier["phase"] == phase and barrier["refs"]
        process.kill()
        assert await process.wait() == -signal.SIGKILL
        monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "swe")
        monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
        monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
        monkeypatch.setattr(
            outbox_runtime,
            "get_tenant_storage_config_path",
            lambda _: Path(barrier["config"]),
        )
        db = DatabaseConnection(_test_db_config())
        await db.connect()
        try:
            store = OutboxRecoveryStore(db, scope_digest(scope))
            sink = StringIO()
            emitter = JsonlEmitter(stream=sink)
            worker = await outbox_runtime.recovery_worker_for_scope(
                scope,
                metadata_db=None,
                observability_db=db,
                emitter=emitter,
                expected_root=barrier["workspace"],
                expected_authority="legacy",
            )
            workspace = worker.scanner.workspace
            chat = await workspace.chat_manager.get_chat(barrier["chat_id"])
            assert chat is not None
            state = await workspace.runner.session.get_persisted_session_state_dict(
                chat.session_id,
                chat.user_id,
            )
            entries = (
                state.get("agent", {}).get("memory", {}).get("content", [])
            )
            committed_refs = {
                entry[0]
                .get("metadata", {})
                .get("agent_observability_evidence_ref")
                for entry in entries
            }
            from swe.agents.memory.conversation_archive import (
                ConversationArchiveStore,
            )

            archive = ConversationArchiveStore(
                Path(barrier["workspace"]) / "dialog",
            )
            for ref, message_id in barrier["message_ids"].items():
                archived = await archive.read_evidence_occurrence(
                    chat.id,
                    ref,
                    message_id,
                    max_bytes=8 * 1024 * 1024,
                )
                assert archived["status"] == "unavailable" and archived.get(
                    "snapshot",
                )
            if phase == "before":
                assert set(barrier["refs"]).isdisjoint(committed_refs)
            else:
                assert set(barrier["refs"]) <= committed_refs
            claim_calls, scan_chats = [], []
            original_claim = worker.store.claim
            recovery_manager = worker.scanner._recovery_workspace.chat_manager
            original_get_chat = recovery_manager.get_chat

            async def tracked_claim(*args, **kwargs):
                result = await original_claim(*args, **kwargs)
                claim_calls.append(result)
                return result

            async def tracked_get_chat(chat_id):
                scan_chats.append(chat_id)
                return await original_get_chat(chat_id)

            worker.store.claim = tracked_claim
            recovery_manager.get_chat = tracked_get_chat
            await worker.run_once()
            assert len(claim_calls) == 1 and claim_calls[0] is not False
            assert barrier["chat_id"] in scan_chats
            assert emitter.close(timeout=2) == 0
            recovered = [
                json.loads(line) for line in sink.getvalue().splitlines()
            ]
            if phase == "before":
                assert not recovered
            else:
                assert {e["reference_id"] for e in recovered} == set(
                    barrier["refs"],
                )
                from tests.integrated.observability.test_real_runner_evidence_mysql import (
                    run_full_recovery_cycle,
                )

                unacked = StringIO()
                unacked_emitter = JsonlEmitter(stream=unacked)
                worker = await outbox_runtime.recovery_worker_for_scope(
                    scope,
                    metadata_db=None,
                    observability_db=db,
                    emitter=unacked_emitter,
                    expected_root=barrier["workspace"],
                    expected_authority="legacy",
                )
                await run_full_recovery_cycle(worker, set(barrier["refs"]))
                assert unacked_emitter.close(timeout=2) == 0
                assert {
                    event["reference_id"]
                    for event in map(
                        json.loads,
                        unacked.getvalue().splitlines(),
                    )
                } == set(barrier["refs"])
                async with db.transaction() as sql:
                    for event in recovered:
                        await sql.insert(
                            "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                            (json.dumps(event),),
                        )
                projector = InboxProjector(
                    ObservabilityStore(db),
                    poll_seconds=0.01,
                )
                while await projector.project_batch():
                    pass
                for event in recovered:
                    assert await store.acknowledged(event["event_id"])
                replay = StringIO()
                replay_emitter = JsonlEmitter(stream=replay)
                worker = await outbox_runtime.recovery_worker_for_scope(
                    scope,
                    metadata_db=None,
                    observability_db=db,
                    emitter=replay_emitter,
                    expected_root=barrier["workspace"],
                    expected_authority="legacy",
                )
                await run_full_recovery_cycle(worker, set(barrier["refs"]))
                assert replay_emitter.close(timeout=2) == 0
                assert not replay.getvalue()
        finally:
            await db.close()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        cleanup = DatabaseConnection(_test_db_config())
        await cleanup.connect()
        try:
            await cleanup.execute(
                "DELETE FROM swe_agentobs_outbox_recovery WHERE scope_key=%s",
                (scope_digest(scope),),
            )
            await cleanup.execute(
                "DELETE FROM swe_agentobs_outbox_scopes WHERE scope_key=%s",
                (scope_digest(scope),),
            )
        finally:
            await cleanup.close()
