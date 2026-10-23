# -*- coding: utf-8 -*-
"""A Query keeps its starting observation across subsequent assembly phases."""

from types import SimpleNamespace

import pytest

from swe.agents import skill_runtime_snapshot as snapshots
from swe.agents import skills_manager
from swe.app.runner import query_runtime
from swe.agents.hook_runtime.models import HookSessionOverlay


@pytest.mark.asyncio
async def test_session_start_does_not_reprobe_or_replace_query_snapshot(
    tmp_path,
    monkeypatch,
):
    snapshot = snapshots.WorkspaceSkillSnapshot(
        tmp_path,
        1,
        snapshots.ManifestStat(0, 0, 0),
        {
            "demo": SimpleNamespace(
                directory=tmp_path,
                freshness_token="observed",
            ),
        },
    )
    inputs = SimpleNamespace(
        workspace_skill_snapshot=snapshot,
        tenant_hooks=None,
        agent_config=None,
        hook_overlay=HookSessionOverlay(),
        skip_history=False,
        env_context="",
        session_execution=None,
        selected_skill_directives=[],
        selected_context_directives=[],
    )

    async def emit(**kwargs):
        # Simulate a SessionStart hook spanning expiry and another refresh.
        snapshots.invalidate_workspace_skill_snapshot(tmp_path)
        return "", None

    def forbidden(*args, **kwargs):
        pytest.fail("Query performed another freshness admission")

    async def load_hooks(**kwargs):
        assert kwargs["inputs"].workspace_skill_snapshot is snapshot
        return inputs.hook_overlay

    monkeypatch.setattr(
        skills_manager,
        "get_skill_freshness_token",
        forbidden,
    )
    monkeypatch.setattr(snapshots, "_stat", forbidden)
    _, blocked = await query_runtime.complete_runtime_activation(
        request=object(),
        inputs=inputs,
        chat=None,
        turn_id="turn",
        mcp_clients=[],
        emit_session_start=emit,
        load_selected_hooks=load_hooks,
    )
    assert blocked is None
    assert inputs.workspace_skill_snapshot is snapshot
