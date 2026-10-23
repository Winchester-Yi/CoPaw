# -*- coding: utf-8 -*-
import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException

from swe.app.routers.tools import list_tools
from swe.app.source_tools.router import (
    SourceToolManualTestRequest,
    create_draft,
    effective_source_tools,
    list_drafts,
    manual_test_draft,
)
from swe.app.source_tools.validation import MAX_SOURCE_TOOL_BYTES
from swe.app.source_tools.service import SourceToolService
from swe.app.source_tools.store import SourceToolStore


def _request(service: SourceToolService, role: str = "manager"):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(source_tool_service=service),
        ),
        state=SimpleNamespace(source_id="source-a", user="manager-a"),
        headers={"X-User-Role": role},
    )


def _script() -> bytes:
    return b"""
TOOL_NAME = "source_echo"
TOOL_DESCRIPTION = "Echo source input."
TOOL_JSON_SCHEMA = {"type": "object", "properties": {}}
REQUIRED_ENV = []

async def execute(arguments, context):
    return {"ok": True}
"""


@pytest.mark.asyncio
async def test_effective_metadata_is_available_without_manager_role(
    tmp_path: Path,
):
    service = SourceToolService(
        SourceToolStore(tmp_path),
        safety_scan=lambda _content, _name: True,
    )
    draft = service.create_draft("source-a", _script(), actor="manager-a")
    service.publish("source-a", draft.name, actor="manager-a")

    result = await effective_source_tools(_request(service, role="user"))

    assert result[0].name == "source_echo"
    assert result[0].origin == "source"


@pytest.mark.asyncio
async def test_draft_listing_requires_source_manager(tmp_path: Path):
    service = SourceToolService(
        SourceToolStore(tmp_path),
        safety_scan=lambda _content, _name: True,
    )

    with pytest.raises(HTTPException) as exc_info:
        await list_drafts(_request(service, role="user"))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_manual_draft_test_requires_confirmation(tmp_path: Path):
    service = SourceToolService(
        SourceToolStore(tmp_path),
        safety_scan=lambda _content, _name: True,
    )
    service.create_draft("source-a", _script(), actor="manager-a")

    with pytest.raises(HTTPException) as exc_info:
        await manual_test_draft(
            "source_echo",
            SourceToolManualTestRequest(confirmed=False),
            _request(service),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_manual_draft_test_rejects_arguments_that_violate_tool_schema(
    tmp_path: Path,
):
    service = SourceToolService(
        SourceToolStore(tmp_path),
        safety_scan=lambda _content, _name: True,
    )
    service.create_draft(
        "source-a",
        _script().replace(
            b'"properties": {}',
            b'"properties": {"invoice_id": {"type": "string"}}, "required": ["invoice_id"]',
        ),
        actor="manager-a",
    )

    with pytest.raises(HTTPException) as exc_info:
        await manual_test_draft(
            "source_echo",
            SourceToolManualTestRequest(confirmed=True, arguments={}),
            _request(service),
        )

    assert exc_info.value.status_code == 400
    assert "schema" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_manual_draft_test_uses_guarded_agent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = SourceToolService(
        SourceToolStore(tmp_path),
        safety_scan=lambda _content, _name: True,
    )
    service.create_draft("source-a", _script(), actor="manager-a")
    calls = []
    from swe.agents import model_factory, prompt, skill_runtime_snapshot

    model_snapshot = object()
    skill_snapshot = object()
    (tmp_path / "AGENTS.md").write_text("manual test prompt")
    read_threads = []
    original_read = prompt.read_text_file_with_encoding_fallback

    def read_prompt(path):
        read_threads.append(threading.get_ident())
        return original_read(path)

    monkeypatch.setattr(
        prompt,
        "read_text_file_with_encoding_fallback",
        read_prompt,
    )

    async def prepare_model(**kwargs):
        assert kwargs == {"agent_id": "agent-a", "tenant_id": "tenant-a"}
        return model_snapshot

    async def prepare_skills(workspace_dir):
        assert workspace_dir == tmp_path
        return skill_snapshot

    monkeypatch.setattr(
        model_factory,
        "prepare_model_config_async",
        prepare_model,
    )
    monkeypatch.setattr(
        skill_runtime_snapshot,
        "get_workspace_skill_snapshot_async",
        prepare_skills,
    )

    class FakeConfig:
        def __init__(self):
            self.tools = SimpleNamespace(builtin_tools={})
            self.system_prompt_files = ["AGENTS.md"]
            self.heartbeat = None

        def model_copy(self, *, deep: bool):
            assert deep is True
            return self

    class FakeAgent:
        def __init__(self, **kwargs):
            assert kwargs["prepared_model_config"] is model_snapshot
            assert kwargs["workspace_skill_snapshot"] is skill_snapshot
            assert prompt._prepared_prompt.get().key == (
                tmp_path,
                ("AGENTS.md",),
                "agent-a",
                False,
            )
            calls.append(kwargs)
            self.memory = SimpleNamespace(
                content=[
                    (
                        SimpleNamespace(content=[{"output": {"ok": True}}]),
                        {},
                    ),
                ],
            )

        async def _acting_impl(self, tool_call):
            calls.append(tool_call)

    workspace = SimpleNamespace(
        agent_id="agent-a",
        tenant_id="tenant-a",
        workspace_dir=tmp_path,
        _task_tracker=None,
    )

    async def fake_agent_and_config(_request):
        return workspace, FakeConfig()

    agent_context = importlib.import_module("swe.app.agent_context")
    monkeypatch.setattr(
        agent_context,
        "get_agent_and_config_for_request",
        fake_agent_and_config,
    )
    fake_react_agent = ModuleType("swe.agents.react_agent")
    fake_react_agent.SWEAgent = FakeAgent
    monkeypatch.setitem(
        sys.modules,
        "swe.agents.react_agent",
        fake_react_agent,
    )

    response = await manual_test_draft(
        "source_echo",
        SourceToolManualTestRequest(
            confirmed=True,
            arguments={"key": "value"},
        ),
        _request(service),
    )

    assert response.output == {"ok": True}
    assert calls[0]["source_tool_versions"][0].version == 0
    assert calls[1]["input"] == {"key": "value"}
    assert service.audit("source-a")[-1].event == "manual_test_completed"
    assert read_threads and threading.get_ident() not in read_threads
    assert prompt._prepared_prompt.get() is None


@pytest.mark.asyncio
async def test_upload_reads_only_the_bounded_validation_window(tmp_path: Path):
    service = SourceToolService(
        SourceToolStore(tmp_path),
        safety_scan=lambda _content, _name: True,
    )
    calls = []

    class Upload:
        async def read(self, size=-1):
            calls.append(size)
            return _script()

    await create_draft(_request(service), Upload())

    assert calls == [MAX_SOURCE_TOOL_BYTES + 1]


@pytest.mark.asyncio
async def test_inactive_source_only_tool_is_not_listed_as_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = SourceToolService(
        SourceToolStore(tmp_path),
        safety_scan=lambda _content, _name: True,
    )
    agent_config = SimpleNamespace(
        tools=SimpleNamespace(
            builtin_tools={
                "source_echo": SimpleNamespace(
                    name="source_echo",
                    enabled=False,
                    description="old source tool",
                    async_execution=False,
                ),
            },
        ),
    )

    async def fake_agent_and_config(_request):
        return SimpleNamespace(), agent_config

    agent_context = importlib.import_module("swe.app.agent_context")
    monkeypatch.setattr(
        agent_context,
        "get_agent_and_config_for_request",
        fake_agent_and_config,
    )

    result = await list_tools(_request(service))

    assert result == []
