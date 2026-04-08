# -*- coding: utf-8 -*-
# pylint: disable=protected-access
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from swe.agents import react_agent, skills_manager
from swe.agents.skills_manager import (
    SkillService,
    _read_skill_from_dir,
    get_pool_skill_manifest_path,
    get_skill_pool_dir,
    get_workspace_skill_manifest_path,
    get_workspace_skills_dir,
    reconcile_pool_manifest,
    reconcile_workspace_manifest,
)
from swe.utils.fs_text import SanitizedFsText


def _write_skill(skill_dir: Path, description: str = "demo skill") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


def test_reconcile_workspace_manifest_renames_unsafe_skill_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    skills_dir = get_workspace_skills_dir(workspace_dir)
    raw_skill_dir = skills_dir / "bad-skill"
    _write_skill(raw_skill_dir)

    manifest_path = get_workspace_skill_manifest_path(workspace_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "version": 0,
                "skills": {
                    "bad-skill": {
                        "enabled": True,
                        "channels": ["console"],
                        "source": "customized",
                        "config": {"x": 1},
                        "metadata": {"legacy": "keep"},
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    original = skills_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad-skill":
            return SanitizedFsText(
                value="safe-skill",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(skills_manager, "sanitize_fs_text", fake_sanitize)

    manifest = reconcile_workspace_manifest(workspace_dir)

    assert not raw_skill_dir.exists()
    assert (skills_dir / "safe-skill").exists()
    assert "safe-skill" in manifest["skills"]
    assert "bad-skill" not in manifest["skills"]
    assert manifest["skills"]["safe-skill"]["enabled"] is True
    assert manifest["skills"]["safe-skill"]["channels"] == ["console"]
    assert manifest["skills"]["safe-skill"]["source"] == "customized"
    assert manifest["skills"]["safe-skill"]["config"] == {"x": 1}
    assert manifest["skills"]["safe-skill"]["metadata"]["legacy"] == "keep"


def test_reconcile_workspace_manifest_renames_conflicting_skill_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    skills_dir = get_workspace_skills_dir(workspace_dir)
    _write_skill(skills_dir / "bad-skill")
    _write_skill(skills_dir / "safe-skill")

    original = skills_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad-skill":
            return SanitizedFsText(
                value="safe-skill",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(skills_manager, "sanitize_fs_text", fake_sanitize)

    manifest = reconcile_workspace_manifest(workspace_dir)

    renamed_names = [
        name for name in manifest["skills"] if name.startswith("safe-skill")
    ]
    assert "safe-skill" in renamed_names
    assert len(renamed_names) == 2
    assert any(name != "safe-skill" for name in renamed_names)
    assert not (skills_dir / "bad-skill").exists()


def test_reconcile_pool_manifest_renames_unsafe_skill_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pool_dir = get_skill_pool_dir(working_dir=tmp_path)
    raw_skill_dir = pool_dir / "bad-skill"
    _write_skill(raw_skill_dir)

    manifest_path = get_pool_skill_manifest_path(working_dir=tmp_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "version": 0,
                "skills": {
                    "bad-skill": {
                        "name": "bad-skill",
                        "source": "customized",
                        "config": {"x": 1},
                    },
                },
                "builtin_skill_names": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    original = skills_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad-skill":
            return SanitizedFsText(
                value="safe-skill",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(skills_manager, "sanitize_fs_text", fake_sanitize)

    manifest = reconcile_pool_manifest(working_dir=tmp_path)

    assert not raw_skill_dir.exists()
    assert (pool_dir / "safe-skill").exists()
    assert "safe-skill" in manifest["skills"]
    assert manifest["skills"]["safe-skill"]["config"] == {"x": 1}


def test_read_skill_from_dir_returns_sanitized_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_dir = tmp_path / "safe-skill"
    _write_skill(skill_dir)
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "bad-ref.md").write_text("demo", encoding="utf-8")
    nested_dir = skill_dir / "scripts" / "bad-dir"
    nested_dir.mkdir(parents=True)
    (nested_dir / "tool.sh").write_text("echo hi", encoding="utf-8")

    original = skills_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        mapping = {
            "bad-ref.md": "safe-ref.md",
            "bad-dir": "safe-dir",
        }
        if text in mapping:
            return SanitizedFsText(
                value=mapping[text],
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(skills_manager, "sanitize_fs_text", fake_sanitize)

    skill = _read_skill_from_dir(skill_dir, "customized")

    assert skill is not None
    assert "safe-ref.md" in skill.references
    assert "safe-dir" in skill.scripts


def test_load_skill_file_accepts_sanitized_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    skills_dir = get_workspace_skills_dir(workspace_dir)
    skill_dir = skills_dir / "safe-skill"
    _write_skill(skill_dir)
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "bad-ref.md").write_text("content", encoding="utf-8")

    original = skills_manager.sanitize_fs_text

    def fake_sanitize(text: str) -> SanitizedFsText:
        if text == "bad-ref.md":
            return SanitizedFsText(
                value="safe-ref.md",
                changed=True,
                strategy="replace",
            )
        return original(text)

    monkeypatch.setattr(skills_manager, "sanitize_fs_text", fake_sanitize)

    reconcile_workspace_manifest(workspace_dir)
    content = SkillService(workspace_dir).load_skill_file(
        skill_name="safe-skill",
        file_path="references/safe-ref.md",
        source="customized",
    )

    assert content == "content"


def test_sanitize_registered_skill_dirs_rewrites_prompt_only_path() -> None:
    raw_dir = b"/tmp/\xc4\xe3\xba\xc3".decode(
        "utf-8",
        errors="surrogateescape",
    )
    toolkit = SimpleNamespace(
        skills={
            "demo": {
                "name": "demo",
                "description": "demo skill",
                "dir": raw_dir,
            },
        },
    )

    react_agent.SWEAgent._sanitize_registered_skill_dirs(toolkit)

    assert toolkit.skills["demo"]["dir"] != raw_dir
    assert toolkit.skills["demo"]["dir"].startswith("/tmp/")
    toolkit.skills["demo"]["dir"].encode("utf-8")
