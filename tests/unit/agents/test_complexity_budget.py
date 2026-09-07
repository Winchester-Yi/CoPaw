# -*- coding: utf-8 -*-
"""Complexity budgets for high-risk agent control paths."""

from __future__ import annotations

import ast
import inspect
import textwrap

from swe.agents.skills_manager import (
    SkillPoolService,
    _require_workspace_layout_v2,
)
from swe.agents.tool_guard_mixin import ToolGuardMixin
from swe.app.runner.runner import AgentRunner, _extract_assistant_response


def _cyclomatic_complexity(func) -> int:
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    decision_nodes = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.ExceptHandler,
        ast.BoolOp,
        ast.IfExp,
        ast.comprehension,
    )
    return 1 + sum(isinstance(node, decision_nodes) for node in ast.walk(tree))


def test_targeted_agent_paths_stay_within_complexity_budget() -> None:
    targets = (
        _require_workspace_layout_v2,
        SkillPoolService.download_to_workspace,
        ToolGuardMixin._acting_impl,
        ToolGuardMixin._selected_expert_follow_up,
        _extract_assistant_response,
        AgentRunner.query_handler,
    )

    assert all(_cyclomatic_complexity(target) <= 15 for target in targets)
