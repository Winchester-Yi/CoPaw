import json
from types import SimpleNamespace

import httpx
import pytest
from agentscope.tool import Toolkit, ToolResponse

from swe.jev.config import JevConfig
from swe.jev.skills import JevToolkit, prepare_skill_selection


def agent():
    toolkit = JevToolkit()
    toolkit.skills = {
        name: {"name": name, "description": name, "dir": "/tenant/" + name}
        for name in ["auth", "billing", "explicit"]
    }
    return SimpleNamespace(
        toolkit=toolkit,
        _agent_config=SimpleNamespace(
            jev=JevConfig(
                endpoint="https://jev.test/decide",
                timeout_seconds=1,
                skill_selection={"enabled": True, "threshold": 0.8},
            ),
            tools=None,
        ),
        _request_context={"current_user_text": "fix login"},
    )


def respond(monkeypatch, scores):
    original = httpx.AsyncClient

    def handler(request):
        questions = json.loads(request.content)["questions"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    key: {"type": "noul", "noul": scores[key]}
                    for key in questions
                }
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )


async def test_prompt_filters_without_removing_runtime_skills(monkeypatch):
    value = agent()
    original_skills = dict(value.toolkit.skills)
    respond(monkeypatch, {"auth": 0.95, "billing": 0.1, "explicit": 0.1})
    await prepare_skill_selection(value, pinned={"explicit", "foreign"})
    prompt = value.toolkit.get_agent_skill_prompt()
    assert "/tenant/auth" in prompt
    assert "/tenant/explicit" in prompt
    assert "/tenant/billing" not in prompt
    assert value.toolkit.skills == original_skills
    assert "foreign" not in prompt


async def test_failed_selection_keeps_full_prompt(monkeypatch):
    value = agent()
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            **kwargs,
        ),
    )
    await prepare_skill_selection(value)
    assert "/tenant/billing" in value.toolkit.get_agent_skill_prompt()


async def test_discovery_keeps_hidden_skills_accessible(monkeypatch):
    value = agent()

    async def read_file(file_path: str) -> ToolResponse:
        """Read a file."""
        return ToolResponse(content=[])

    value.toolkit.register_tool_function(read_file)
    respond(monkeypatch, {"auth": 0.95, "billing": 0.1, "explicit": 0.1})
    await prepare_skill_selection(value)
    result = await value.toolkit.call_tool_function(
        {
            "id": "lookup",
            "name": "search_skills",
            "input": {"query": "billing"},
        }
    )
    chunks = [chunk async for chunk in result]
    text = chunks[-1].content[0]["text"]
    assert "/tenant/billing/SKILL.md" in text
    assert "/tenant/auth/SKILL.md" not in text
    assert "/tenant/billing" not in value.toolkit.get_agent_skill_prompt()


async def test_no_discovery_tool_when_read_file_disabled(monkeypatch):
    value = agent()
    respond(monkeypatch, {"auth": 0.95, "billing": 0.1, "explicit": 0.1})
    await prepare_skill_selection(value)
    assert "search_skills" not in value.toolkit.tools


def test_unselected_toolkit_matches_framework_prompt():
    value = agent()
    assert (
        value.toolkit.get_agent_skill_prompt()
        == Toolkit.get_agent_skill_prompt(value.toolkit)
    )


async def test_disabled_legacy_agent_does_not_require_new_runtime_fields():
    await prepare_skill_selection(SimpleNamespace())


def test_swe_agent_installs_catalog_projection(monkeypatch):
    from swe.agents.react_agent import SWEAgent
    from swe.config.config import AgentProfileConfig

    value = SWEAgent.__new__(SWEAgent)
    value._request_context = {}
    value._agent_config = AgentProfileConfig(
        id="test",
        name="Test",
        jev=agent()._agent_config.jev,
    )
    value._task_tracker = None
    value._workspace_dir = None
    for method in (
        "_register_enabled_tools",
        "_register_background_task_tools",
        "_register_background_subagent_tools",
    ):
        monkeypatch.setattr(value, method, lambda *a, **kw: None)
    toolkit = value._create_toolkit()
    assert isinstance(toolkit, JevToolkit)
