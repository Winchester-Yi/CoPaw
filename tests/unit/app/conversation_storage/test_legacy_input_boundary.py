# -*- coding: utf-8 -*-
"""Legacy extraction enumerates only the selected session/archive inputs."""

import json
import os

import pytest

from swe.app.conversation_storage.migration import legacy
from swe.app.runner.session import SafeJSONSession

CHAT = "16374f34-c02e-4bb9-87bf-eb5e64348cef"


def _unrelated_symlink(root):
    sibling = root / "unrelated"
    sibling.mkdir()
    (sibling / "forbidden-link").symlink_to(root, target_is_directory=True)


def test_working_copy_requires_explicit_entries_and_ignores_other_trees(
    tmp_path,
):
    (tmp_path / "chosen").mkdir()
    (tmp_path / "chosen/data.json").write_text("{}")
    _unrelated_symlink(tmp_path)
    with legacy.working_copy(tmp_path, relative_roots=("chosen",)) as copied:
        assert (copied / "chosen/data.json").read_text() == "{}"
        assert not (copied / "unrelated").exists()


@pytest.mark.parametrize("schema", [1, 2])
def test_old_session_ignores_unselected_siblings(tmp_path, schema):
    path = tmp_path / "selected.json"
    state = {"schema_version": schema, "agent": {"memory": {"content": []}}}
    path.write_text(json.dumps(state))
    _unrelated_symlink(tmp_path)
    assert legacy.read_session(path)["root"] == state


async def _ledger_session(root):
    session = SafeJSONSession(str(root))
    await session.save_merged_state(
        "selected",
        state={
            "turn_states": {
                "question": {
                    "status": "completed",
                    "message": {"id": "question"},
                },
            },
        },
    )
    await session.migrate_terminal_turns("selected", dry_run=False)
    return root / "selected.json"


async def test_v3_session_copies_only_own_generation(tmp_path, monkeypatch):
    path = await _ledger_session(tmp_path)
    state = json.loads(path.read_text())
    generation = state["session_generation"]
    unrelated = tmp_path / ".turn-ledger/unrelated-generation"
    unrelated.mkdir()
    (unrelated / "forbidden-link").symlink_to(
        tmp_path,
        target_is_directory=True,
    )
    _unrelated_symlink(tmp_path)
    monkeypatch.setattr(
        legacy,
        "safe_files",
        lambda *_args: pytest.fail("must not scan the whole session parent"),
    )
    result = legacy.read_session(path)
    assert result["format"] == "session-v3"
    assert "question" in result["state"]["turn_states"]
    assert all(
        name == path.name
        or name.startswith(".turn-ledger/" + generation + "/")
        for name in result["files"]
    )


async def test_v3_session_rejects_symlink_inside_selected_generation(tmp_path):
    path = await _ledger_session(tmp_path)
    generation = json.loads(path.read_text())["session_generation"]
    (tmp_path / ".turn-ledger" / generation / "forbidden-link").symlink_to(
        path,
    )
    with pytest.raises(ValueError, match="symlink"):
        legacy.read_session(path)


async def test_archive_copies_only_selected_chat_and_deleted_marker(
    tmp_path,
    monkeypatch,
):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    store = ConversationArchiveStore(tmp_path, cursor_secret=b"test")
    await store.commit(CHAT, [Msg("user", "selected body", "user")])
    _unrelated_symlink(tmp_path)
    (tmp_path / ".deleted").mkdir()
    (tmp_path / ".deleted/other-chat").symlink_to(
        tmp_path,
        target_is_directory=True,
    )
    monkeypatch.setattr(
        legacy,
        "safe_files",
        lambda *_args: pytest.fail("must not scan the whole dialog parent"),
    )
    result = legacy.read_archive(tmp_path, CHAT)
    assert result["batches"][0]["messages"][0]["content"] == "selected body"
    assert not result["deleted"]


def test_archive_rejects_selected_deleted_symlink(tmp_path):
    (tmp_path / ".deleted").mkdir()
    (tmp_path / ".deleted" / CHAT).symlink_to(
        tmp_path,
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="symlink"):
        legacy.read_archive(tmp_path, CHAT)


def test_session_rejects_selected_file_symlink(tmp_path):
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"schema_version": 2}))
    link = tmp_path / "selected.json"
    link.symlink_to(original)
    with pytest.raises(ValueError, match="symlink"):
        legacy.read_session(link)


def test_archive_missing_selected_inputs_remains_absent(tmp_path):
    _unrelated_symlink(tmp_path)
    assert legacy.read_archive(tmp_path, CHAT)["format"] == "absent"


def test_working_copy_rejects_selected_special_file_but_ignores_other_fifo(
    tmp_path,
):
    (tmp_path / "selected.json").write_text("{}")
    os.mkfifo(tmp_path / "unrelated-fifo")
    with legacy.working_copy(
        tmp_path,
        relative_roots=("selected.json",),
    ) as copied:
        assert (copied / "selected.json").read_text() == "{}"
    with pytest.raises(ValueError, match="unsupported|regular|special"):
        with legacy.working_copy(tmp_path, relative_roots=("unrelated-fifo",)):
            pytest.fail("special selected input must be rejected")
