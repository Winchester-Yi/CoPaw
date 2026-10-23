# -*- coding: utf-8 -*-
"""Historical-copy measurement uses new directories and exact expectations."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts/rehearse_memory_history.py"
)


@pytest.mark.parametrize("with_turns", [True, False])
def test_session_worker_preserves_history_and_exports_new_writes(
    tmp_path,
    with_turns,
):
    case = {"id": "session-01", "kind": "session", "root": str(tmp_path)}
    (tmp_path / "case.json").write_text(json.dumps(case))
    source = tmp_path / "sessions/s.json"
    source.parent.mkdir()
    initial = {"agent": {"memory": {"content": []}}}
    if with_turns:
        initial["turn_states"] = {
            "t0": {
                "status": "completed",
                "message": {"id": "t0", "content": "masked"},
            },
        }
    source.write_text(json.dumps(initial))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--worker", str(tmp_path / "case.json")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["initial_total"] == int(with_turns)
    assert report["exported_total"] == 1 + int(with_turns)
    assert report["measurements"]["migration"]["peak_new_python_bytes"] > 0
    exported = json.loads((tmp_path / "rollback/sessions/s.json").read_bytes())
    if with_turns:
        assert exported["turn_states"]["t0"]["status"] == "stopped"
    assert "copy-rehearsal-new" in exported["turn_states"]


def test_normalized_session_removes_only_storage_metadata():
    spec = importlib.util.spec_from_file_location("history_rehearsal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {
        "schema_version": 3,
        "revision": 4,
        "ledger_nonce": "x",
        "agent": {"memory": {"content": ["masked"]}},
        "turn_states": {
            "t": {"status": "completed", "admission_order": 1},
        },
    }
    assert module.normalized_session(state) == {
        "schema_version": 2,
        "agent": {"memory": {"content": ["masked"]}},
        "turn_states": {"t": {"status": "completed"}},
    }
    assert state["revision"] == 4


def test_events_only_journal_materializes_default_checkpoint(
    tmp_path,
    monkeypatch,
):
    from swe.agents.memory.chat_checkpoint import CheckpointEvent

    chat_id = "16374f34-c02e-4bb9-87bf-eb5e64348cef"
    secret_directory = tmp_path / "outside-the-copy"
    monkeypatch.setenv("SWE_SECRET_DIR", str(secret_directory))
    monkeypatch.delenv("SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET", raising=False)
    case = {
        "id": "journal-01",
        "kind": "journal",
        "root": str(tmp_path),
        "chat_id": chat_id,
    }
    (tmp_path / "case.json").write_text(json.dumps(case))
    source = tmp_path / "dialog" / chat_id / "events.jsonl"
    source.parent.mkdir(parents=True)
    event = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="message_added",
        facts={"message_id": "masked"},
        source_refs=("message:masked",),
    )
    source.write_text(json.dumps(event.to_dict()) + "\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--worker", str(tmp_path / "case.json")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert not secret_directory.exists()
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["initial_total"] == 1
    assert report["exported_total"] == 2
