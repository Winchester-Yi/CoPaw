# -*- coding: utf-8 -*-
"""Measure offline migrations on anonymized historical copies only."""

import argparse
import asyncio
from copy import deepcopy
import gc
import inspect
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import tracemalloc

_V3_KEYS = (
    "session_generation",
    "turn_ledger_version",
    "ledger_nonce",
    "next_admission_order",
    "cold_turn_count",
    "ledger_latest",
    "revision",
)


def normalized_session(state):
    state = deepcopy(state)
    for key in _V3_KEYS:
        state.pop(key, None)
    for turn in state.setdefault("turn_states", {}).values():
        turn.pop("admission_order", None)
    state["schema_version"] = 2
    return state


def tree_size(root):
    files = [path for path in root.rglob("*") if path.is_file()]
    return {
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
    }


def rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


async def profile(operation, measurements, name):
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = operation()
        if inspect.isawaitable(result):
            result = await result
        duration = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    measurements[name] = {
        "seconds": round(duration, 6),
        "peak_new_python_bytes": peak,
        "process_peak_rss_bytes": rss_bytes(),
    }
    return result


def write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)


def read_events(path):
    return [
        json.loads(line)
        for line in path.read_bytes().splitlines()
        if line.strip()
    ]


async def session_case(root, measurements):
    from swe.app.runner.session import SafeJSONSession
    from swe.cli.memory_storage_cmd import _publish_json

    sessions = SafeJSONSession(str(root / "sessions"))
    expected = json.loads((root / "sessions/s.json").read_bytes())
    initial = tree_size(root / "sessions")
    preview = await profile(
        lambda: sessions.migrate_terminal_turns("s"),
        measurements,
        "preview",
    )
    assert json.loads((root / "sessions/s.json").read_bytes()) == expected
    await profile(
        lambda: sessions.migrate_terminal_turns("s", dry_run=False),
        measurements,
        "migration",
    )
    state = await sessions.get_session_state_dict("s")
    full = await sessions.materialize_turn_states("s", "", state)
    assert normalized_session(full) == normalized_session(expected)
    after_migration = tree_size(root / "sessions")
    hot = len(state.get("turn_states", {}))
    async with sessions.execution("s") as tx:
        turns = expected.setdefault("turn_states", {})
        if turns:
            turn_id = next(iter(turns))
            await tx.hydrate_turn(turn_id)
            turns[turn_id]["status"] = "stopped"
            tx.state["turn_states"][turn_id]["status"] = "stopped"
        probe = {
            "status": "completed",
            "message": {
                "id": "copy-rehearsal-new",
                "role": "user",
                "content": "synthetic probe",
            },
        }
        turns["copy-rehearsal-new"] = probe
        tx.state.setdefault("turn_states", {})["copy-rehearsal-new"] = probe
        await tx.commit_state(tx.state)
    await profile(
        lambda: sessions.migrate_terminal_turns("s"),
        measurements,
        "validation",
    )

    async def export():
        state = await sessions.export_legacy_state("s")
        _publish_json(root / "rollback/sessions/s.json", state)
        return state

    exported = await profile(export, measurements, "export")
    assert normalized_session(exported) == normalized_session(expected)
    write_json(root / "expected.json", normalized_session(expected))
    return {
        "initial_total": preview["total"],
        "exported_total": len(expected["turn_states"]),
        "hot_turns_after_migration": hot,
        "cold_turns_after_migration": preview["total"] - hot,
        "initial_storage": initial,
        "migrated_storage": after_migration,
        "rollback_storage": tree_size(root / "rollback"),
    }


async def journal_case(root, case, measurements):
    from swe.agents.memory.chat_checkpoint import CheckpointEvent
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(
        root / "dialog",
        cursor_secret=b"history-copy-rehearsal",
    )
    chat = case["chat_id"]
    source = store.path_for(chat)
    persisted_checkpoint = (source / "checkpoint.json").exists()
    expected = read_events(source / "events.jsonl")
    before = await store.read_checkpoint_state(chat)
    initial = tree_size(root / "dialog")
    await profile(
        lambda: store.migrate_checkpoint_v2(chat, dry_run=True),
        measurements,
        "preview",
    )
    await profile(
        lambda: store.migrate_checkpoint_v2(chat),
        measurements,
        "migration",
    )
    migrated = await store.read_checkpoint_state(chat)
    old_record, new_record = before.record.to_dict(), migrated.record.to_dict()
    if not persisted_checkpoint:
        for field in ("checkpoint_id", "updated_at"):
            old_record.pop(field)
            new_record.pop(field)
    assert old_record == new_record
    assert (before.current_epoch, before.events) == (
        migrated.current_epoch,
        migrated.events,
    )
    after_migration = tree_size(root / "dialog")
    sequence = (
        max(
            (
                e["sequence"]
                for e in expected
                if e["epoch"] == before.current_epoch
            ),
            default=0,
        )
        + 1
    )
    probe = CheckpointEvent.new(
        sequence=sequence,
        epoch=before.current_epoch,
        type="message_added",
        facts={"message_id": "copy-rehearsal-new"},
        source_refs=("message:copy-rehearsal-new",),
    )
    await store.append_checkpoint_event(chat, probe)
    expected.append(probe.to_dict())
    await profile(
        lambda: store.validate_checkpoint_v2(chat),
        measurements,
        "validation",
    )
    destination = root / "rollback/dialog" / chat
    await profile(
        lambda: store.export_checkpoint_legacy(chat, destination),
        measurements,
        "export",
    )
    assert read_events(destination / "events.jsonl") == expected
    assert (
        json.loads((destination / "checkpoint.json").read_bytes())
        == migrated.record.to_dict()
    )
    write_json(
        root / "expected.json",
        {"events": expected, "checkpoint": migrated.record.to_dict()},
    )
    return {
        "initial_total": len(expected) - 1,
        "exported_total": len(expected),
        "initial_storage": initial,
        "migrated_storage": after_migration,
        "rollback_storage": tree_size(root / "rollback"),
    }


async def legacy_case(root, case):
    expected = json.loads((root / "expected.json").read_bytes())
    if case["kind"] == "session":
        from swe.app.runner.session import SafeJSONSession

        session = SafeJSONSession(str(root / "rollback/sessions"))
        state = await session.get_session_state_dict("s")
        assert normalized_session(state) == expected
        async with session.execution("s") as tx:
            probe = {
                "status": "completed",
                "message": {
                    "id": "legacy-writer-probe",
                    "content": "probe",
                    "role": "user",
                },
            }
            tx.state["turn_states"]["legacy-writer-probe"] = probe
            await tx.commit_state(tx.state)
        expected["turn_states"]["legacy-writer-probe"] = probe
        assert (
            normalized_session(await session.get_session_state_dict("s"))
            == expected
        )
        return {
            "passed": True,
            "total_after_legacy_write": len(expected["turn_states"]),
            "module": inspect.getfile(SafeJSONSession),
        }
    from swe.agents.memory.chat_checkpoint import CheckpointEvent
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(
        root / "rollback/dialog",
        cursor_secret=b"history-copy-rehearsal",
    )
    chat = case["chat_id"]
    state = await store.read_checkpoint_state(chat)
    assert state.record.to_dict() == expected["checkpoint"]
    path = store.path_for(chat) / "events.jsonl"
    assert read_events(path) == expected["events"]
    event = CheckpointEvent.new(
        sequence=state.events[-1].sequence + 1,
        epoch=state.current_epoch,
        type="message_added",
        facts={"message_id": "legacy-writer-probe"},
        source_refs=("message:legacy-writer-probe",),
    )
    await store.append_checkpoint_event(chat, event)
    assert read_events(path) == expected["events"] + [event.to_dict()]
    return {
        "passed": True,
        "total_after_legacy_write": len(expected["events"]) + 1,
        "module": inspect.getfile(ConversationArchiveStore),
    }


async def worker(case_path, legacy):
    case = json.loads(case_path.read_bytes())
    root = case_path.parent
    assert root.resolve() == Path(case["root"]).resolve()
    if legacy:
        return await legacy_case(root, case)
    measurements = {}
    baseline = rss_bytes()
    if case["kind"] == "session":
        result = await session_case(root, measurements)
    else:
        result = await journal_case(root, case, measurements)
    return {
        "id": case["id"],
        "kind": case["kind"],
        "passed": True,
        "measurements": measurements,
        "baseline_rss_bytes": baseline,
        "case_peak_rss_bytes": rss_bytes(),
        **result,
    }


def subprocess_worker(script, case_path, source_checkout, legacy=False):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = (
        str(source_checkout / "src")
        + os.pathsep
        + str(source_checkout / "tests/fakes")
    )
    for name in ("SWE_SESSION_V3_TENANTS", "SWE_CHECKPOINT_V2_TENANTS"):
        environment.pop(name, None)
    arguments = [sys.executable, str(script), "--worker", str(case_path)]
    if legacy:
        arguments.append("--legacy")
    result = subprocess.run(
        arguments,
        env=environment,
        cwd=source_checkout,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    (
        case_path.parent / ("legacy.log" if legacy else "migration.log")
    ).write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(
            f"case {case_path.parent.name} failed; inspect its local log",
        )
    return json.loads(result.stdout.splitlines()[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--old-checkout", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--legacy",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(asyncio.run(worker(args.worker, args.legacy))))
        return
    if not all((args.source_root, args.output_root, args.old_checkout)):
        parser.error(
            "--source-root, --output-root and --old-checkout are required",
        )
    from memory_history_fixture import prepare_cases, verify_sources

    script = Path(__file__).resolve()
    current = script.parent.parent
    old_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=args.old_checkout,
        text=True,
    ).strip()
    if not old_revision.startswith("ddb683634"):
        parser.error("old checkout must be the pre-phase2 ddb683634 revision")
    prepared = prepare_cases(args.source_root, args.output_root)
    results = []
    for case in prepared["cases"]:
        case_path = Path(case["root"]) / "case.json"
        write_json(case_path, case)
        result = subprocess_worker(script, case_path, current)
        legacy = subprocess_worker(
            script,
            case_path,
            args.old_checkout,
            legacy=True,
        )
        legacy.pop("module", None)
        results.append(
            {
                **result,
                "legacy": legacy,
                "source_bytes": case["source_bytes"],
                "fixture_bytes": case["fixture_bytes"],
            },
        )
        print(f"{case['id']}: migration/export/old-writer passed", flush=True)
    unchanged = verify_sources(prepared["source_fingerprints"])
    summary = {
        "scope": "anonymized-historical-copies-local-filesystem",
        "passed": unchanged and bool(results),
        "selected_original_files_unchanged": unchanged,
        "old_revision": old_revision,
        "inventory": prepared["inventory"],
        "cases": results,
        "memory_semantics": "tracemalloc=new allocations per operation; RSS=per-case cumulative OS high-water including imports, not isolated per-operation resident memory",
    }
    write_json(args.output_root / "summary.json", summary)
    if not summary["passed"]:
        raise RuntimeError("source changed or no cases were exercised")
    print(
        json.dumps(
            {
                "passed": True,
                "cases": len(results),
                "summary": str(args.output_root / "summary.json"),
            },
        ),
    )


if __name__ == "__main__":
    main()
