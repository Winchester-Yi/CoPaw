# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_select_runtime_context_logs_skill_selection_counts(
    monkeypatch,
    caplog,
    tmp_path: Path,
) -> None:
    from swe.app.runner import query_runtime
    from swe.app.runner.skill_selection import SkillUseDirective

    selected = SkillUseDirective(
        name="selected",
        description="selected",
        path=tmp_path / "selected" / "SKILL.md",
    )
    reference = SkillUseDirective(
        name="reference",
        description="reference",
        path=tmp_path / "reference" / "SKILL.md",
    )

    async def build_references(**_kwargs):
        return [reference]

    monkeypatch.setattr(
        "swe.app.runner.context_references.build_context_reference_directives",
        build_references,
    )
    monkeypatch.setattr(
        "swe.app.runner.skill_selection.build_skill_use_directives",
        lambda **_kwargs: [selected],
    )

    inputs = SimpleNamespace(
        agent_config=object(),
        channel="console",
        workspace_skill_snapshot=SimpleNamespace(generation=7),
        selected_skill_directives=[],
        selected_context_directives=[],
    )
    request = object()

    runtime_logger = logging.getLogger(query_runtime.__name__)
    runtime_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=query_runtime.__name__):
            await query_runtime.select_runtime_context_directives(
                inputs,
                request,
                workspace_dir=tmp_path,
                chat=None,
                request_scenario_snapshot=lambda _request: None,
                with_scenario_mcp=lambda config, *_args, **_kwargs: config,
                request_context_references=lambda _request: [],
                request_selected_skill_names=lambda _request: ["selected"],
            )
    finally:
        runtime_logger.removeHandler(caplog.handler)

    assert any(
        "selected_skill_count=1" in record.getMessage()
        and "reference_skill_count=1" in record.getMessage()
        and "runtime_skill_snapshot_generation=7" in record.getMessage()
        for record in caplog.records
    )
