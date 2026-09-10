# -*- coding: utf-8 -*-
"""Critical-path checks for scheduled run execution boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentscope_runtime.engine.schemas.agent_schemas import (
    ContentType,
    RunStatus,
    TextContent,
)

from swe.app.crons.executor import CronExecutor
from swe.app.runner.query_contracts import QueryPersistenceResult
from swe.config.context import (
    get_current_effective_tenant_id,
    get_current_source_id,
    get_current_workspace_dir,
)
from swe.config.llm_workload import LLM_WORKLOAD_CRON, get_current_llm_workload

from tests.integrated.critical_paths.conftest import (
    build_agent_job,
    build_text_job,
)


class RecordingRunner:
    def __init__(self, *, text: str = "agent done") -> None:
        self.text = text
        self.requests: list[dict] = []
        self.context: dict[str, object] = {}
        self.session = _RecordingSession()

    async def stream_query(self, req):
        self.requests.append(dict(req))
        self.context = {
            "workspace_dir": get_current_workspace_dir(),
            "source_id": get_current_source_id(),
            "effective_tenant_id": get_current_effective_tenant_id(),
            "llm_workload": get_current_llm_workload(),
        }
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            content=[TextContent(type=ContentType.TEXT, text=self.text)],
        )
        yield SimpleNamespace(
            object="response",
            status=RunStatus.Completed,
        )

    def get_query_persistence_result(self, **_kwargs):
        return QueryPersistenceResult(
            session_id="session-critical",
            user_id="user-critical",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        )


class _RecordingSession:
    def __init__(self) -> None:
        self.state: dict[str, object] = {}

    async def mutate_session_state(self, *, mutator, **_kwargs):
        self.state = mutator(self.state)


@pytest.mark.asyncio
async def test_text_scheduled_run_delivers_without_mcp_availability(
    tmp_path,
    recording_channel_manager,
) -> None:
    executor = CronExecutor(
        runner=RecordingRunner(),
        channel_manager=recording_channel_manager,
    )
    job = build_text_job(workspace_dir=tmp_path, text="heartbeat ok")

    result = await executor.execute(
        job,
        dispatch_meta={
            "scheduled_fire_at": "2026-09-08T02:30:00Z",
        },
    )

    assert result.output_preview == "heartbeat ok"
    assert recording_channel_manager.texts == []
    assert executor._runner.session.state["task_messages"][0]["content"] == [
        {"type": "text", "text": "heartbeat ok"},
    ]
    assert get_current_workspace_dir() is None
    assert get_current_source_id() is None


@pytest.mark.asyncio
async def test_agent_scheduled_run_builds_cron_request_and_sends_events(
    tmp_path,
    recording_channel_manager,
) -> None:
    runner = RecordingRunner(text="agent final")
    executor = CronExecutor(
        runner=runner,
        channel_manager=recording_channel_manager,
    )
    job = build_agent_job(workspace_dir=tmp_path)

    result = await executor.execute(
        job,
        dispatch_meta={
            "scheduled_fire_at": "2026-09-08T02:30:00Z",
        },
    )

    assert runner.context["workspace_dir"] == tmp_path
    assert runner.context["source_id"] == "source-critical"
    assert runner.context["effective_tenant_id"] is not None
    assert runner.context["llm_workload"] == LLM_WORKLOAD_CRON
    assert runner.requests[0]["skip_history"] is True
    assert runner.requests[0]["source_id"] == "source-critical"
    assert runner.requests[0]["session_id"] == "session-critical"
    assert recording_channel_manager.events[0]["event"].status == (
        RunStatus.Completed
    )
    assert result.output_preview == "agent final"
