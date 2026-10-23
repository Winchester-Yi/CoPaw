# -*- coding: utf-8 -*-
"""A fresh writer recovers the journal's precise durable publication point."""

import json
import os
import subprocess
import sys

import pytest

from swe.agents.memory.chat_checkpoint import CheckpointEvent
from swe.agents.memory.conversation_archive import ConversationArchiveStore

CHAT = "16374f34-c02e-4bb9-87bf-eb5e64348cef"


@pytest.mark.parametrize("stage", ["unpublished", "published"])
def test_journal_abrupt_exit_recovers_and_retry_does_not_duplicate(
    tmp_path,
    stage,
):
    store = ConversationArchiveStore(tmp_path / "dialog")
    store.migrate_checkpoint_v2(CHAT)
    event = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="message_added",
        facts={"message_id": "crash-event", "role": "user"},
        source_refs=("message:crash-event",),
    )
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event.to_dict()), encoding="utf-8")
    script = r"""
import json, os, sys
from pathlib import Path
from swe.agents.memory import checkpoint_storage_v2 as storage
from swe.agents.memory.chat_checkpoint import CheckpointEvent
from swe.agents.memory.conversation_archive import ConversationArchiveStore
root, chat, event_path, stage = sys.argv[1:]
original = storage.atomic_bytes
def publish(path, payload):
    if path.name == 'state-v2.json' and stage == 'unpublished':
        os._exit(91)
    original(path, payload)
    if path.name == 'state-v2.json':
        os._exit(91)
storage.atomic_bytes = publish
event = CheckpointEvent.from_dict(json.loads(Path(event_path).read_bytes()))
ConversationArchiveStore(root)._append_checkpoint_event(chat, event)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "dialog"),
            CHAT,
            str(event_path),
            stage,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 91, result.stderr
    reopened = ConversationArchiveStore(tmp_path / "dialog")
    visible = reopened._read_checkpoint_state(CHAT).events
    assert len(visible) == (1 if stage == "published" else 0)
    reopened._append_checkpoint_event(CHAT, event)
    reopened._append_checkpoint_event(CHAT, event)
    assert [
        item.to_dict() for item in reopened._read_checkpoint_state(CHAT).events
    ] == [
        event.to_dict(),
    ]
    assert not (
        reopened.path_for(CHAT) / "journal-v2-quarantine.json"
    ).exists()
    evidence = {
        "stage": stage,
        "visible_before_retry": len(visible),
        "after_retry": 1,
        "event_id": event.id,
        "exit_code": result.returncode,
        "filesystem_device": os.stat(tmp_path).st_dev,
    }
    (tmp_path / "recovery-evidence.json").write_text(
        json.dumps(evidence),
        encoding="utf-8",
    )
