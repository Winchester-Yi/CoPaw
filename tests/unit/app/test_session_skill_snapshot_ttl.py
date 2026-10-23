# -*- coding: utf-8 -*-
"""Session freshness consumes only the Query's observed workspace state."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.agents.skill_runtime_snapshot import (
    ManifestStat,
    WorkspaceSkillSnapshot,
)
from swe.app.runner.runner import AgentRunner
from swe.app.runner.session_lifecycle import refresh_session_skill_freshness


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "partial", "failure"])
async def test_session_refresh_uses_observed_tokens_and_preserves_unconfirmed(
    monkeypatch,
    tmp_path,
    outcome,
):
    stored = {
        name: {
            "skill_name": name,
            "resolved_skill_dir": str(tmp_path / name),
            "freshness_token": "old",
            "confirmed_at": 1.0,
        }
        for name in ("a", "c", "deleted")
    }
    skill = SimpleNamespace(
        directory=tmp_path / "a",
        freshness_token="observed",
    )
    snapshot = WorkspaceSkillSnapshot(
        tmp_path,
        1,
        ManifestStat(0, 0, 0),
        {} if outcome == "failure" else {"a": skill},
        manifest_available=outcome != "failure",
        unconfirmed_skills=(
            frozenset({"c"}) if outcome == "partial" else frozenset()
        ),
    )
    owner = SimpleNamespace(
        workspace_dir=tmp_path,
        session=SimpleNamespace(
            get_session_skill_snapshot=AsyncMock(return_value=stored),
        ),
        _supports_session_skill_freshness_refresh=lambda **kwargs: True,
        _normalize_session_skill_snapshot=AgentRunner._normalize_session_skill_snapshot,
        _refresh_session_skill_snapshot_entries=AgentRunner._refresh_session_skill_snapshot_entries,
        _skill_freshness_notice_text=AgentRunner._skill_freshness_notice_text,
    )
    runtime = SimpleNamespace(
        session_id="s",
        user_id="u",
        session_execution=None,
        agent=SimpleNamespace(
            _workspace_skill_snapshot=snapshot,
            get_effective_skills=lambda: [] if outcome == "failure" else ["a"],
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Session freshness must not probe disk")

    monkeypatch.setattr(
        "swe.agents.skills_manager.resolve_effective_skill_dir",
        forbidden,
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        forbidden,
    )
    with monkeypatch.context() as disk_guard:
        disk_guard.setattr(Path, "exists", forbidden)
        result = await refresh_session_skill_freshness(
            owner,
            runtime=runtime,
            refresh_result_type=SimpleNamespace,
        )
    if outcome == "failure":
        assert result.refreshed_snapshot == stored
        assert result.notice_text is None
    else:
        assert result.refreshed_snapshot["a"]["freshness_token"] == "observed"
        assert "deleted" not in result.refreshed_snapshot
        assert "deleted: no longer effective" in result.notice_text
        if outcome == "partial":
            assert result.refreshed_snapshot["c"] == stored["c"]
            assert "c:" not in result.notice_text
        else:
            assert "c" not in result.refreshed_snapshot
