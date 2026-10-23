# -*- coding: utf-8 -*-
"""Offline storage commands never migrate implicitly or overwrite exports."""

import json

from click.testing import CliRunner

from swe.cli.main import cli


def test_session_preview_apply_validate_and_export(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source = sessions / "s.json"
    turns = {
        f"t{i}": {"status": "completed", "message": {"id": f"t{i}"}}
        for i in range(80)
    }
    source.write_text(json.dumps({"turn_states": turns}))
    runner = CliRunner()
    base = [
        "memory-storage",
        "session",
        "--sessions-dir",
        str(sessions),
        "--session-id",
        "s",
    ]
    preview = runner.invoke(cli, base + ["preview"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["total"] == 80
    dry = runner.invoke(cli, base + ["migrate"])
    assert dry.exit_code == 0, dry.output
    assert "schema_version" not in json.loads(source.read_text())
    applied = runner.invoke(cli, base + ["migrate", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert json.loads(source.read_text())["schema_version"] == 3
    checked = runner.invoke(cli, base + ["validate"])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["total"] == 80
    output = tmp_path / "legacy.json"
    exported = runner.invoke(
        cli,
        base + ["export", "--destination", str(output)],
    )
    assert exported.exit_code == 0, exported.output
    assert len(json.loads(output.read_text())["turn_states"]) == 80
    before = output.read_bytes()
    repeated = runner.invoke(
        cli,
        base + ["export", "--destination", str(output)],
    )
    assert repeated.exit_code != 0
    assert output.read_bytes() == before


def test_session_missing_root_cannot_create_session(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "memory-storage",
            "session",
            "--sessions-dir",
            str(tmp_path),
            "--session-id",
            "missing",
            "migrate",
            "--apply",
        ],
    )
    assert result.exit_code != 0
    assert not (tmp_path / "missing.json").exists()


def test_journal_commands_default_to_preview(tmp_path):
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    dialog = tmp_path / "dialog"
    store = ConversationArchiveStore(dialog, cursor_secret=b"test")
    store.path_for("11111111-1111-1111-1111-111111111111").mkdir(parents=True)
    base = [
        "memory-storage",
        "journal",
        "--dialog-root",
        str(dialog),
        "--chat-id",
        "11111111-1111-1111-1111-111111111111",
    ]
    runner = CliRunner()
    dry = runner.invoke(cli, base + ["migrate"])
    assert dry.exit_code == 0, dry.output
    assert not (
        store.path_for("11111111-1111-1111-1111-111111111111")
        / "state-v2.json"
    ).exists()
    applied = runner.invoke(cli, base + ["migrate", "--apply"])
    assert applied.exit_code == 0, applied.output
    valid = runner.invoke(cli, base + ["validate"])
    assert valid.exit_code == 0, valid.output
    output = tmp_path / "legacy"
    exported = runner.invoke(
        cli,
        base + ["export", "--destination", str(output)],
    )
    assert exported.exit_code == 0, exported.output
    assert output.is_dir()


def test_journal_legacy_export_rejected_without_quarantine(
    tmp_path,
    monkeypatch,
):
    from swe.agents.memory.chat_checkpoint import CheckpointEvent
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    monkeypatch.delenv("SWE_CHECKPOINT_V2_TENANTS", raising=False)
    dialog = tmp_path / "dialog"
    store = ConversationArchiveStore(dialog, cursor_secret=b"test")
    chat_id = "11111111-1111-1111-1111-111111111111"
    store._append_checkpoint_event(
        chat_id,
        CheckpointEvent.new(
            sequence=1,
            epoch=1,
            type="message_added",
            facts={"message_id": "first", "role": "user"},
            source_refs=("message:first",),
        ),
    )
    destination = tmp_path / "legacy"
    result = CliRunner().invoke(
        cli,
        [
            "memory-storage",
            "journal",
            "--dialog-root",
            str(dialog),
            "--chat-id",
            chat_id,
            "export",
            "--destination",
            str(destination),
        ],
    )

    assert result.exit_code != 0
    assert "version 2" in result.output
    assert not (
        store.path_for(chat_id) / "journal-v2-quarantine.json"
    ).exists()
    assert not destination.exists()


def test_corrupt_session_is_not_replaced_by_migration(tmp_path):
    source = tmp_path / "broken.json"
    source.write_text('{"turn_states":')
    before = source.read_bytes()
    result = CliRunner().invoke(
        cli,
        [
            "memory-storage",
            "session",
            "--sessions-dir",
            str(tmp_path),
            "--session-id",
            "broken",
            "migrate",
            "--apply",
        ],
    )
    assert result.exit_code != 0
    assert source.read_bytes() == before
