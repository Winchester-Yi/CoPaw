# -*- coding: utf-8 -*-
"""Cold turn storage preserves committed snapshots and admission semantics."""

import json
from types import SimpleNamespace

import pytest

from swe.app.runner.session import SafeJSONSession
from swe.app.runner.session_lifecycle import admit_user_turn


def turns(count):
    return {
        f"t{i}": {
            "status": "completed" if i % 2 else "failed",
            "message": {"id": f"t{i}", "role": "user", "content": "x" * 100},
            "created_at": str(i),
            "chat_id": "chat-a",
        }
        for i in range(count)
    }


@pytest.mark.asyncio
async def test_migration_keeps_hot_window_and_cold_statuses(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    original = {"turn_states": turns(80), "agent": {"memory": {}}}
    await session.save_merged_state("s", state=original)
    preview = await session.migrate_terminal_turns("s", dry_run=True)
    assert preview["total"] == 80
    assert (await session.get_session_state_dict("s")).get(
        "schema_version",
    ) != 3
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    assert state["schema_version"] == 3
    assert len(state["turn_states"]) <= 64
    cold = await session.get_turn_state("s", "", state, "t0")
    assert cold["status"] == "failed"
    full = await session.materialize_turn_states("s", "", state)
    assert list(full["turn_states"]) == list(original["turn_states"])
    assert (
        full["turn_states"]["t0"]["message"]
        == original["turn_states"]["t0"]["message"]
    )


@pytest.mark.asyncio
async def test_cold_read_and_retry_preserve_original_dict_comparison(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    original = turns(80)
    original["t0"]["message"]["numeric"] = 1
    await session.save_merged_state("s", state={"turn_states": original})
    await session.migrate_terminal_turns("s", dry_run=False)
    message = dict(original["t0"]["message"], numeric=True)
    async with session.execution("s") as tx:
        msg = SimpleNamespace(id="t0", role="user", to_dict=lambda: message)
        await admit_user_turn(
            tx,
            msgs=[msg],
            request=SimpleNamespace(channel_meta={}),
        )
        assert tx.state["turn_states"]["t0"]["status"] == "failed"
        assert not tx.has_uncommitted_state


@pytest.mark.asyncio
async def test_export_includes_updates_and_does_not_revive_deleted_session(
    tmp_path,
):
    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    async with session.execution("s") as tx:
        await tx.hydrate_turn("t0")
        tx.state["turn_states"]["t0"]["status"] = "stopped"
        await tx.commit_state(tx.state)
    exported = await session.export_legacy_state("s")
    assert exported["turn_states"]["t0"]["status"] == "stopped"
    assert len(exported["turn_states"]) == 80
    assert exported["schema_version"] == 2
    path = tmp_path / "s.json"
    generation = json.loads(path.read_text())["session_generation"]
    path.unlink()
    async with session.execution("s") as tx:
        assert not tx.state
        await tx.commit_state({"agent": {}})
    state = json.loads(path.read_text())
    assert state.get("session_generation") != generation
    assert not state.get("turn_states")


@pytest.mark.asyncio
async def test_short_mutation_hydrates_cold_turn_and_advances_revision(
    tmp_path,
):
    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    before = await session.get_session_state_dict("s")

    def mutate(state):
        state["turn_states"]["t0"]["status"] = "stopped"
        return state

    await session.mutate_session_state("s", mutate, hydrate_turn_ids=("t0",))
    after = await session.get_session_state_dict("s")
    assert after["revision"] == before["revision"] + 1
    turn = await session.get_turn_state("s", "", after, "t0")
    assert turn["status"] == "stopped"
    assert turn["message"]["id"] == "t0"


@pytest.mark.asyncio
async def test_failed_root_publication_recovers_before_next_revision(
    tmp_path,
    monkeypatch,
):
    from swe.app.runner import session as module

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    before = await session.get_session_state_dict("s")
    writer = module._write_json_state_sync
    async with session.execution("s") as tx:
        tx.state["turn_states"].update(turns(81))
        monkeypatch.setattr(
            module,
            "_write_json_state_sync",
            lambda *a: (_ for _ in ()).throw(OSError("publish failed")),
        )
        with pytest.raises(OSError, match="publish failed"):
            await tx.commit_state(tx.state)
    monkeypatch.setattr(module, "_write_json_state_sync", writer)
    assert (await session.get_session_state_dict("s"))["revision"] == before[
        "revision"
    ]
    async with session.execution("s") as tx:
        await tx.commit_state(tx.state)
    after = await session.get_session_state_dict("s")
    assert (await session.get_turn_state("s", "", after, "t0"))[
        "status"
    ] == "failed"


@pytest.mark.asyncio
async def test_history_and_sharing_use_cold_authoritative_status(
    tmp_path,
    monkeypatch,
):
    from swe.app.runner import api
    from swe.app.runner.models import ChatSpec
    from swe.app.chat_sharing.router import _turn_statuses

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    chat = ChatSpec(
        id="chat-a",
        name="test",
        session_id="s",
        user_id="",
        channel="console",
    )
    workspace = SimpleNamespace(workspace_dir=None)
    history = await api._build_chat_history(
        chat,
        session=session,
        workspace=workspace,
    )
    assert len(history.messages) == 80
    full = await session.materialize_turn_states(
        "s",
        "",
        await session.get_session_state_dict("s"),
        chat.id,
    )
    assert _turn_statuses(history.messages, full)["t0"] == "failed"


@pytest.mark.asyncio
async def test_delete_cold_turn_is_revisioned_and_does_not_return(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    old = await session.get_session_state_dict("s")
    await session.delete_turn_state("s", "t0")
    new = await session.get_session_state_dict("s")
    assert await session.get_turn_state("s", "", new, "t0") is None
    assert (await session.get_turn_state("s", "", old, "t0"))[
        "status"
    ] == "failed"
    assert "t0" not in (await session.export_legacy_state("s"))["turn_states"]


@pytest.mark.asyncio
async def test_latest_rechecks_chat_after_same_id_moves_between_chats(
    tmp_path,
):
    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    async with session.execution("s") as tx:
        await tx.hydrate_turn("t15")
        tx.state["turn_states"]["t15"].update(
            chat_id="chat-b",
            status="admitted",
        )
        await tx.commit_state(tx.state)
    state = await session.get_session_state_dict("s")
    assert (await session.get_latest_turn_state("s", "", state, "chat-b"))[
        0
    ] == "t15"
    assert (await session.get_latest_turn_state("s", "", state, "chat-a"))[
        0
    ] == "t79"


@pytest.mark.asyncio
async def test_unsettled_records_are_never_archived(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    source = turns(80)
    source["t0"]["status"] = "admitted"
    source["t1"]["status"] = "unknown"
    await session.save_merged_state("s", state={"turn_states": source})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    assert state["turn_states"]["t0"]["status"] == "admitted"
    assert state["turn_states"]["t1"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_new_session_format_is_opt_in_and_existing_format_sticks(
    tmp_path,
    monkeypatch,
):
    from swe.config.context import current_tenant_id

    session = SafeJSONSession(str(tmp_path))
    token = current_tenant_id.set("ledger-tenant")
    try:
        monkeypatch.setenv("SWE_SESSION_V3_TENANTS", "ledger-tenant")
        async with session.execution("s") as tx:
            await tx.commit_state({"turn_states": {}})
        assert (await session.get_session_state_dict("s"))[
            "schema_version"
        ] == 3
        monkeypatch.delenv("SWE_SESSION_V3_TENANTS")
        async with session.execution("s") as tx:
            await tx.commit_state(tx.state)
        assert (await session.get_session_state_dict("s"))[
            "schema_version"
        ] == 3
    finally:
        current_tenant_id.reset(token)


@pytest.mark.asyncio
async def test_cleanup_failure_after_publish_cannot_delete_committed_turns(
    tmp_path,
    monkeypatch,
):
    from swe.app.runner.turn_ledger import TurnLedger

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    finish = TurnLedger.finish
    async with session.execution("s") as tx:
        tx.state["turn_states"].update(turns(81))
        # Added records must be persisted once before they are eligible.
        await tx.commit_state(tx.state)
        monkeypatch.setattr(
            TurnLedger,
            "finish",
            lambda *a: (_ for _ in ()).throw(OSError("cleanup failed")),
        )
        await tx.commit_state(tx.state)
        pending = (
            TurnLedger(tmp_path / "s.json").directory(tx.state)
            / "pending.json"
        )
        assert pending.exists()
        monkeypatch.setattr(TurnLedger, "finish", finish)
        await tx.commit_state(tx.state)
        assert not pending.exists()
    final = await session.get_session_state_dict("s")
    assert (
        len(
            (await session.materialize_turn_states("s", "", final))[
                "turn_states"
            ],
        )
        == 81
    )


@pytest.mark.asyncio
async def test_missing_cold_payload_fails_instead_of_silently_losing_history(
    tmp_path,
):
    from swe.app.runner.turn_ledger import TurnLedger

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(65)})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    ledger = TurnLedger(tmp_path / "s.json")
    for path in ledger._turn_directory(state, "t0").glob("*.json"):
        path.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        await session.get_turn_state("s", "", state, "t0")
    with pytest.raises((ValueError, FileNotFoundError)):
        await session.materialize_turn_states("s", "", state)


@pytest.mark.asyncio
async def test_latest_skips_cold_payloads_when_hot_turn_is_newer(
    tmp_path,
    monkeypatch,
):
    from swe.app.runner.turn_ledger import TurnLedger

    session = SafeJSONSession(str(tmp_path))
    source = turns(130)
    source["t129"]["status"] = "unknown"
    await session.save_merged_state("s", state={"turn_states": source})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    monkeypatch.setattr(
        TurnLedger,
        "_read_turn",
        lambda *a: pytest.fail("unnecessary cold payload read"),
    )
    assert (await session.get_latest_turn_state("s", "", state, "chat-a"))[
        0
    ] == "t128"


@pytest.mark.asyncio
async def test_corrupt_root_cannot_be_overwritten_by_short_save(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    path = tmp_path / "s.json"
    path.write_text('{"schema_version": 3,')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        await session.save_merged_state("s", state={"agent": {}})
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_short_replacement_preserves_cold_metadata(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    await session.save_merged_state("s", state={"agent": {}})
    state = await session.get_session_state_dict("s")
    assert state["cold_turn_count"] == 16
    assert (
        len(
            (await session.materialize_turn_states("s", "", state))[
                "turn_states"
            ],
        )
        == 16
    )
    assert "cold_turn_count" not in await session.export_legacy_state("s")


@pytest.mark.asyncio
async def test_future_schema_refuses_execution_and_save(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    path = tmp_path / "s.json"
    path.write_text('{"schema_version": 4, "revision": 5}')
    with pytest.raises(ValueError, match="schema"):
        async with session.execution("s"):
            pass
    with pytest.raises(ValueError, match="schema"):
        await session.save_merged_state("s", state={})


@pytest.mark.asyncio
async def test_missing_reference_and_latest_index_fail_closed(tmp_path):
    from swe.app.runner.turn_ledger import TurnLedger

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(65)})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    ledger = TurnLedger(tmp_path / "s.json")
    refs = list(ledger._reference_directory(state, "t0").glob("*.json"))
    for path in refs:
        path.unlink()
    with pytest.raises(ValueError, match="reference"):
        await session.get_turn_state("s", "", state, "t0")
    for path in ledger.directory(state).glob("latest/*/*.json"):
        path.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        await session.get_latest_turn_state("s", "", state, "chat-a")


@pytest.mark.asyncio
async def test_utf8_byte_budget_and_modified_terminal_protection(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    source = turns(8)
    for turn in source.values():
        turn["message"]["content"] = "中" * 20000
    await session.save_merged_state("s", state={"turn_states": source})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    assert len(state["turn_states"]) < 8
    async with session.execution("s") as tx:
        await tx.hydrate_turn("t0")
        tx.state["turn_states"]["t0"]["message"]["content"] = "更新" * 150000
        await tx.commit_state(tx.state)
        assert "t0" in tx.state["turn_states"]
    assert (
        await session.get_turn_state(
            "s",
            "",
            await session.get_session_state_dict("s"),
            "t0",
        )
    )["message"]["content"] == "更新" * 150000


@pytest.mark.asyncio
async def test_preview_does_not_recover_pending_intent(tmp_path):
    from swe.app.runner.turn_ledger import TurnLedger, terminal_fingerprints

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    ledger = TurnLedger(tmp_path / "s.json")
    ledger.prepare(
        state,
        terminal_fingerprints(state),
        state["revision"] + 1,
        deleted=("t0",),
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = await session.migrate_terminal_turns("s", dry_run=True)
    assert preview["pending"] is True
    assert before == {
        p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


@pytest.mark.asyncio
async def test_damaged_pending_cannot_delete_committed_files(tmp_path):
    from swe.app.runner.turn_ledger import TurnLedger, atomic_json

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(65)})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    ledger = TurnLedger(tmp_path / "s.json")
    root = ledger.directory(state)
    atomic_json(
        root / "pending.json",
        {
            "generation": state["session_generation"],
            "base_revision": state["revision"],
            "target_revision": state["revision"] + 1,
            "nonce": "00000000-0000-0000-0000-000000000000",
            "files": ["metadata.json"],
        },
    )
    before = (root / "metadata.json").read_bytes()
    with pytest.raises(ValueError, match="intent"):
        ledger.recover(state)
    assert (root / "metadata.json").read_bytes() == before


@pytest.mark.asyncio
async def test_cancelled_recovery_holds_lock_until_worker_finishes(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import threading
    from swe.app.runner.turn_ledger import TurnLedger

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(65)})
    await session.migrate_terminal_turns("s", dry_run=False)
    started, release = threading.Event(), threading.Event()
    recover = TurnLedger.recover
    calls = 0

    def blocked_recover(ledger, state):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(5)
        recover(ledger, state)

    monkeypatch.setattr(TurnLedger, "recover", blocked_recover)
    acquired = asyncio.Event()

    async def enter(mark=False):
        async with session.execution("s"):
            if mark:
                acquired.set()

    first = asyncio.create_task(enter())
    assert await asyncio.to_thread(started.wait, 5)
    first.cancel()
    second = asyncio.create_task(enter(True))
    await asyncio.sleep(0.02)
    assert not first.done()
    assert not acquired.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert acquired.is_set()


@pytest.mark.asyncio
async def test_v3_replacement_dicts_preserve_committed_identity(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    before = await session.get_session_state_dict("s")
    async with session.execution("s") as tx:
        await tx.commit_state({"agent": {}})
    await session.mutate_session_state(
        "s",
        lambda _: {"agent": {"memory": {}}},
    )
    final = await session.get_session_state_dict("s")
    assert final["schema_version"] == 3
    assert final["session_generation"] == before["session_generation"]
    assert final["revision"] == before["revision"] + 2
    assert (await session.get_turn_state("s", "", final, "t0"))[
        "status"
    ] == "failed"


@pytest.mark.asyncio
async def test_corrupt_tombstone_flag_is_not_accepted(tmp_path):
    from swe.app.runner.turn_ledger import TurnLedger

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(65)})
    await session.migrate_terminal_turns("s", dry_run=False)
    state = await session.get_session_state_dict("s")
    ledger = TurnLedger(tmp_path / "s.json")
    path = next(ledger._turn_directory(state, "t0").glob("*.json"))
    payload = json.loads(path.read_bytes())
    payload["deleted"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="checksum"):
        await session.get_turn_state("s", "", state, "t0")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["intent", "files", "published"])
async def test_process_exit_recovers_exact_commit_boundary(tmp_path, stage):
    import subprocess
    import sys

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(65)})
    await session.migrate_terminal_turns("s", dry_run=False)
    script = r"""
import json, os, sys
from pathlib import Path
from swe.app.runner import session as sessions
from swe.app.runner import turn_ledger as ledgers
path, stage = sys.argv[1:]
state = json.loads(Path(path).read_bytes())
state['_deleted_turn_ids'] = ['t0']
if stage == 'intent':
    original = ledgers.atomic_json
    def write(path, value):
        original(path, value)
        if path.name == 'pending.json':
            os._exit(91)
    ledgers.atomic_json = write
elif stage == 'files':
    sessions._write_json_state_sync = lambda *a: os._exit(91)
else:
    ledgers.TurnLedger.finish = lambda *a: os._exit(91)
sessions._commit_ledger_sync(path, state, ledgers.terminal_fingerprints(state), state['revision'] + 1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "s.json"), stage],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 91, result.stderr
    state = await session.get_session_state_dict("s")
    visible = await session.get_turn_state("s", "", state, "t0")
    assert (visible is None) == (stage == "published")
    async with session.execution("s") as tx:
        await tx.commit_state(tx.state)
    state = await session.get_session_state_dict("s")
    assert ((await session.get_turn_state("s", "", state, "t0")) is None) == (
        stage == "published"
    )
    assert len(
        (await session.materialize_turn_states("s", "", state))["turn_states"],
    ) == (64 if stage == "published" else 65)


@pytest.mark.asyncio
async def test_two_processes_serialize_ledger_session_writes(tmp_path):
    from contextlib import ExitStack
    import asyncio
    import subprocess
    import sys

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state(
        "s",
        state={"turn_states": turns(65), "count": 0},
    )
    await session.migrate_terminal_turns("s", dry_run=False)
    before = await session.get_session_state_dict("s")
    script = r"""
import asyncio, sys
from swe.app.runner.session import SafeJSONSession
async def main():
    session = SafeJSONSession(sys.argv[1])
    for _ in range(8):
        async with session.execution('s') as tx:
            tx.state['count'] += 1
            await tx.commit_state(tx.state)
asyncio.run(main())
"""
    with ExitStack() as stack:
        processes = [
            stack.enter_context(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(tmp_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                _, error = await asyncio.to_thread(
                    process.communicate,
                    timeout=30,
                )
                assert process.returncode == 0, error
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    final = await session.get_session_state_dict("s")
    assert final["count"] == 16
    assert final["revision"] == before["revision"] + 16
    assert (
        len(
            (await session.materialize_turn_states("s", "", final))[
                "turn_states"
            ],
        )
        == 65
    )
