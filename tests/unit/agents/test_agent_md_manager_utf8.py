# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from swe.agents.memory import agent_md_manager
from swe.agents.memory.agent_md_manager import AgentMdManager
from swe.utils.fs_text import SanitizedFsText


def test_list_working_mds_renames_unsafe_filename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_file = tmp_path / "bad.md"
    raw_file.write_text("demo", encoding="utf-8")

    original = agent_md_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad.md":
            return SanitizedFsText(
                value="safe.md",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(agent_md_manager, "sanitize_fs_text", fake_sanitize)

    files = AgentMdManager(tmp_path).list_working_mds()

    assert not raw_file.exists()
    assert (tmp_path / "safe.md").exists()
    assert files[0]["filename"] == "safe.md"
    assert files[0]["path"].endswith("safe.md")


def test_list_working_mds_renames_conflicting_filename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "bad.md").write_text("demo", encoding="utf-8")
    (tmp_path / "safe.md").write_text("existing", encoding="utf-8")

    original = agent_md_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad.md":
            return SanitizedFsText(
                value="safe.md",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(agent_md_manager, "sanitize_fs_text", fake_sanitize)

    files = AgentMdManager(tmp_path).list_working_mds()

    filenames = sorted(file["filename"] for file in files)
    assert "safe.md" in filenames
    assert any(
        name.startswith("safe-") and name.endswith(".md") for name in filenames
    )
    assert not (tmp_path / "bad.md").exists()


def test_read_and_write_working_md_accept_sanitized_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_file = tmp_path / "bad.md"
    raw_file.write_text("old", encoding="utf-8")

    original = agent_md_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad.md":
            return SanitizedFsText(
                value="safe.md",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(agent_md_manager, "sanitize_fs_text", fake_sanitize)

    manager = AgentMdManager(tmp_path)
    assert manager.read_working_md("safe.md") == "old"

    manager.write_working_md("safe.md", "new")

    assert (tmp_path / "safe.md").read_text(encoding="utf-8") == "new"

    manager.append_working_md("bad.md", "\nmore")

    assert (tmp_path / "safe.md").read_text(encoding="utf-8") == "new\nmore"


def test_read_working_md_keeps_content_encoding_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_file = tmp_path / "bad.md"
    raw_file.write_bytes("你好".encode("gbk"))

    original = agent_md_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad.md":
            return SanitizedFsText(
                value="safe.md",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(agent_md_manager, "sanitize_fs_text", fake_sanitize)

    manager = AgentMdManager(tmp_path)

    assert manager.read_working_md("safe.md") == "你好"
