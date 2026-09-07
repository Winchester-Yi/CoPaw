# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest


def _write_workspace(workspace: Path, *, description: str = "cached") -> None:
    skill_dir = workspace / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: {description}\n---\nbody\n",
        encoding="utf-8",
    )
    (workspace / "skill.json").write_text(
        json.dumps(
            {
                "layout_version": 2,
                "skills": {"demo": {"enabled": True, "channels": ["all"]}},
            },
        ),
        encoding="utf-8",
    )


def test_resolve_effective_skills_reuses_unchanged_workspace_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from swe.agents import skills_manager

    _write_workspace(tmp_path)
    original = skills_manager.reconcile_workspace_manifest
    calls = 0

    def counted(workspace: Path):
        nonlocal calls
        calls += 1
        return original(workspace)

    monkeypatch.setattr(
        skills_manager,
        "reconcile_workspace_manifest",
        counted,
    )

    assert skills_manager.resolve_effective_skills(tmp_path, "console") == [
        "demo",
    ]
    assert skills_manager.resolve_effective_skills(tmp_path, "console") == [
        "demo",
    ]
    assert calls == 1


def test_snapshot_reuses_manifest_metadata_and_detects_skill_content_change(
    tmp_path: Path,
) -> None:
    from swe.agents import skills_manager
    from swe.agents.skill_runtime_snapshot import get_workspace_skill_snapshot

    _write_workspace(tmp_path, description="before")
    first = get_workspace_skill_snapshot(tmp_path)
    assert first.skills["demo"].metadata["description"] == "before"
    assert first.skills["demo"].content_signature

    (tmp_path / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: after\n---\nbody\n",
        encoding="utf-8",
    )
    second = get_workspace_skill_snapshot(tmp_path)
    assert second is not first
    assert (
        second.skills["demo"].content_signature
        != first.skills["demo"].content_signature
    )
    assert skills_manager.resolve_effective_skills(
        tmp_path,
        "console",
        _snapshot=second,
    ) == ["demo"]


def test_workspace_skill_coordinator_serializes_local_mutations(
    tmp_path: Path,
) -> None:
    from swe.agents.skill_runtime_snapshot import workspace_skill_coordinator

    entered: list[str] = []
    first_inside = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with workspace_skill_coordinator(tmp_path):
            entered.append("first")
            first_inside.set()
            release_first.wait(timeout=2)
            entered.append("first-done")

    def second() -> None:
        first_inside.wait(timeout=2)
        with workspace_skill_coordinator(tmp_path):
            entered.append("second")

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_inside.wait(timeout=2)
    time.sleep(0.02)
    assert entered == ["first"]
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert entered == ["first", "first-done", "second"]


def test_snapshot_build_failure_keeps_previous_cached_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from swe.agents import skills_manager
    from swe.agents import skill_runtime_snapshot as snapshots

    _write_workspace(tmp_path)
    first = snapshots.get_workspace_skill_snapshot(tmp_path)
    manifest_path = tmp_path / "skill.json"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    def fail_read(*_args, **_kwargs):
        raise RuntimeError("reconcile unavailable")

    monkeypatch.setattr(skills_manager, "read_skill_manifest", fail_read)
    with pytest.raises(RuntimeError, match="reconcile unavailable"):
        snapshots.get_workspace_skill_snapshot(tmp_path)

    assert snapshots._CACHE[tmp_path.resolve()] is first


def test_snapshot_validation_refreshes_stat_token_without_rehashing_forever(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from swe.agents import skills_manager
    from swe.agents import skill_runtime_snapshot as snapshots

    _write_workspace(tmp_path)
    first = snapshots.get_workspace_skill_snapshot(tmp_path)
    skill_md = tmp_path / "skills" / "demo" / "SKILL.md"
    stat = skill_md.stat()
    os.utime(
        skill_md,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
    )

    validated = snapshots._filter_changed_skills(first)
    assert (
        validated.skills["demo"].freshness_token
        != first.skills["demo"].freshness_token
    )

    monkeypatch.setattr(
        skills_manager,
        "_build_signature",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unchanged content should not be rehashed"),
        ),
    )
    assert snapshots._filter_changed_skills(validated) is validated


def test_snapshot_warn_mode_keeps_skill_when_scan_reports_blocking_finding(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Warn mode must not turn a scanner finding into snapshot exclusion."""
    from swe.agents import skill_runtime_snapshot as snapshots
    from swe.agents import skills_manager
    from swe.security import skill_scanner

    _write_workspace(tmp_path)
    manifest = json.loads(
        (tmp_path / "skill.json").read_text(encoding="utf-8"),
    )
    monkeypatch.setattr(
        skills_manager,
        "read_skill_manifest",
        lambda *_args, **_kwargs: manifest,
    )
    monkeypatch.setenv("SWE_SKILL_SCAN_MODE", "warn")

    def raise_scan(*_args, **_kwargs):
        raise skill_scanner.SkillScanError(
            skill_scanner.ScanResult(
                skill_name="demo",
                skill_directory=str(tmp_path / "skills" / "demo"),
            ),
        )

    monkeypatch.setattr(skill_scanner, "scan_skill_directory", raise_scan)

    snapshot = snapshots.get_workspace_skill_snapshot(tmp_path)

    assert "demo" in snapshot.skills


def test_mutate_json_coordinator_uses_manifest_kind_not_parent_name(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A workspace directory named ``skill_pool`` is still a workspace."""
    from swe.agents import skill_runtime_snapshot as snapshots
    from swe.agents import skills_manager

    workspace = tmp_path / "skill_pool"
    workspace.mkdir()
    manifest_path = workspace / "skill.json"
    entered: list[Path] = []

    class _Coordinator:
        def __enter__(self):
            entered.append(workspace)

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        snapshots,
        "workspace_skill_coordinator",
        lambda path: _Coordinator(),
    )

    skills_manager._mutate_json(
        manifest_path,
        {"schema_version": "workspace-skill-manifest.v1", "skills": {}},
        lambda payload: payload,
    )

    assert entered == [workspace]


def test_reconcile_moves_registered_skill_to_enabled_root(
    tmp_path: Path,
) -> None:
    """A registered skill is moved when its manifest enablement changes."""
    from swe.agents import skills_manager

    workspace = tmp_path / "workspace"
    disabled = workspace / ".disabled_skills" / "demo"
    disabled.mkdir(parents=True)
    (disabled / "SKILL.md").write_text(
        "---\nname: demo\ndescription: moved\n---\nbody\n",
        encoding="utf-8",
    )
    (workspace / "skill.json").write_text(
        json.dumps(
            {
                "layout_version": 2,
                "skills": {"demo": {"enabled": True, "channels": ["all"]}},
            },
        ),
        encoding="utf-8",
    )

    skills_manager.reconcile_workspace_manifest(workspace)

    assert (workspace / "skills" / "demo" / "SKILL.md").exists()
    assert not disabled.exists()


@pytest.mark.asyncio
async def test_corrupt_manifest_and_missing_skill_fail_closed_for_snapshot(
    tmp_path: Path,
) -> None:
    """Unreadable or missing workspace state must not load a skill."""
    from swe.agents.skill_runtime_snapshot import (
        get_workspace_skill_snapshot_async,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "skill.json").write_text("{not-json", encoding="utf-8")

    first = await get_workspace_skill_snapshot_async(workspace)
    assert first.skills == {}

    (workspace / "skill.json").write_text(
        json.dumps(
            {
                "layout_version": 2,
                "skills": {"missing": {"enabled": True, "channels": ["all"]}},
            },
        ),
        encoding="utf-8",
    )
    second = await get_workspace_skill_snapshot_async(workspace)
    assert second.skills == {}


def test_workspace_snapshots_are_isolated_between_tenants(
    tmp_path: Path,
) -> None:
    """A skill snapshot never crosses workspace/tenant boundaries."""
    from swe.agents.skill_runtime_snapshot import get_workspace_skill_snapshot

    tenant_a = tmp_path / "tenant-a"
    tenant_b = tmp_path / "tenant-b"
    _write_workspace(tenant_a, description="tenant-a")
    _write_workspace(tenant_b, description="tenant-b")

    snapshot_a = get_workspace_skill_snapshot(tenant_a)
    snapshot_b = get_workspace_skill_snapshot(tenant_b)

    assert snapshot_a.workspace_dir == tenant_a.resolve()
    assert snapshot_b.workspace_dir == tenant_b.resolve()
    assert snapshot_a.skills["demo"].metadata["description"] == "tenant-a"
    assert snapshot_b.skills["demo"].metadata["description"] == "tenant-b"
