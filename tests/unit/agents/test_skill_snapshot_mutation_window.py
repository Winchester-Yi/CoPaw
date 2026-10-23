# -*- coding: utf-8 -*-
"""Ordinary management operations honor the freshness observation window."""

import json

from swe.agents import skill_runtime_snapshot as snapshots
from swe.agents import skills_manager


def test_ordinary_reconcile_does_not_invalidate_observation_window(
    tmp_path,
    monkeypatch,
):
    now = [0.0]
    monkeypatch.setattr(snapshots.time, "monotonic", lambda: now[0])
    directory = tmp_path / "skills" / "demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\nbody\n",
    )
    (tmp_path / "skill.json").write_text(
        json.dumps(
            {
                "layout_version": 2,
                "skills": {"demo": {"enabled": True}},
            },
        ),
    )
    first = snapshots.get_workspace_skill_snapshot(tmp_path)
    skills_manager.reconcile_workspace_manifest(tmp_path)
    now[0] = 599
    assert snapshots.get_workspace_skill_snapshot(tmp_path) is first
