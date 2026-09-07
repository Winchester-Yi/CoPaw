# -*- coding: utf-8 -*-
"""SubAgent run monitor API tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from swe.app.middleware.tenant_workspace import TenantWorkspaceContext
from swe.app.runner.api import get_workspace
from swe.app.runner.models import ChatSpec
from swe.app.routers.subagents import router
from swe.app.subagents import (
    AgentResult,
    AgentRegistry,
    BackgroundSubAgentNotManageable,
    DefinitionMatchMetadata,
    DelegationSpec,
    PerRunSubAgentRunStore,
    PermissionPolicy,
    SubAgentStartRequest,
    builtin_definition_provider,
)
from swe.app.subagents.models import AgentError, BudgetConfig, Metrics
from swe.app.subagents.monitor import SubAgentMonitorService
from swe.app.subagents.supervisor import BackgroundSubAgentScope
from swe.config.config import AgentProfileConfig


class _ChatManager:
    def __init__(self) -> None:
        self.chats = {
            "chat-1": ChatSpec(
                id="chat-1",
                session_id="session-1",
                user_id="user-1",
                channel="console",
                name="Chat 1",
            ),
            "chat-2": ChatSpec(
                id="chat-2",
                session_id="session-2",
                user_id="user-1",
                channel="console",
                name="Chat 2",
            ),
        }

    async def get_chat(self, chat_id: str) -> ChatSpec | None:
        return self.chats.get(chat_id)


class _Supervisor:
    def __init__(self, store: PerRunSubAgentRunStore) -> None:
        self.store = store
        self.cancel_calls: list[str] = []
        self.wait_calls = 0

    async def get(self, _scope, run_id: str):
        return await self.store.get(run_id)

    async def cancel(self, _scope, run_id: str):
        self.cancel_calls.append(run_id)
        return await self.store.cancel(run_id)

    async def wait(self, _scope, *, timeout_ms: int = 0):
        self.wait_calls += 1
        return None

    def is_manageable(self, _scope, run_id: str) -> bool:
        return run_id == "subagent-running"


@pytest.mark.asyncio
async def test_monitor_records_scan_runs_filesystem_io_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls = []

    async def run_worker(function, *args, **kwargs):
        calls.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "swe.app.subagents.monitor.run_runtime_state_work",
        run_worker,
        raising=False,
    )
    service = SubAgentMonitorService(
        supervisor=SimpleNamespace(),
        scope=BackgroundSubAgentScope(
            tenant_id="tenant-1",
            agent_id="agent-1",
            run_store_dir=tmp_path,
        ),
    )

    assert (
        await service._records_for_session("session-1") == []
    )  # noqa: SLF001
    assert len(calls) == 1


def _client(
    tmp_path,
    *,
    request_workspace: object | None = None,
) -> tuple[TestClient, PerRunSubAgentRunStore, _Supervisor]:
    app = FastAPI()
    app.include_router(router)
    store = PerRunSubAgentRunStore(tmp_path / "subagent_runs")
    supervisor = _Supervisor(store)
    workspace = SimpleNamespace(
        agent_id="agent-1",
        tenant_id="tenant-1",
        workspace_dir=tmp_path,
        chat_manager=_ChatManager(),
        config=AgentProfileConfig(
            id="agent-1",
            name="Agent",
            workspace_dir=str(tmp_path),
        ),
        subagent_supervisor=supervisor,
        subagent_run_store_dir=tmp_path / "subagent_runs",
    )
    app.state.workspace = workspace
    app.dependency_overrides[get_workspace] = lambda: workspace
    if request_workspace is not None:

        @app.middleware("http")
        async def inject_workspace_context(request, call_next):
            request.state.workspace = request_workspace
            return await call_next(request)

    return TestClient(app), store, supervisor


async def _create_run(
    store: PerRunSubAgentRunStore,
    *,
    run_id: str,
    session_id: str,
    status: str,
    objective: str = "Inspect repository",
    nickname: str | None = None,
    definition_match: DefinitionMatchMetadata | None = None,
):
    definition = AgentRegistry([builtin_definition_provider()]).resolve(
        "plan-researcher",
    )
    record = await store.create(
        DelegationSpec(
            parent_thread_id=session_id,
            name="plan-researcher",
            objective=objective,
            budget={"timeout_ms": 120_000},
        ),
        definition,
        PermissionPolicy.readonly(),
        effective_budget=BudgetConfig(max_turns=4, timeout_ms=120_000),
        start_request=SubAgentStartRequest.model_validate(
            {
                "name": "plan-researcher",
                "instruction": "Research this run.",
                "objective": objective,
            },
        ),
        definition_match=definition_match,
        nickname=nickname,
    )
    path = store._path(record.run_id)  # pylint: disable=protected-access
    record = record.model_copy(update={"run_id": run_id})
    path.unlink()
    store._write(record)  # pylint: disable=protected-access
    if status == "running":
        return await store.mark_running(run_id, worker_pid=123)
    if status == "completed":
        return await store.finish(
            run_id,
            AgentResult(
                task_id="task-1",
                agent_run_id=run_id,
                agent_name="plan-researcher",
                status="completed",
                summary="完成" * 120,
                metrics=Metrics(turns_used=3, elapsed_ms=10_000),
            ),
        )
    if status == "failed":
        return await store.fail(
            run_id,
            "失败" * 120,
            result=AgentResult(
                task_id="task-1",
                agent_run_id=run_id,
                agent_name="plan-researcher",
                status="failed",
                summary="failed",
                metrics=Metrics(elapsed_ms=10_000),
                errors=[
                    AgentError(
                        code="runtime_error",
                        message="失败" * 120,
                        recoverable=False,
                    ),
                ],
            ),
        )
    return record


def test_monitor_snapshot_ignores_lightweight_request_workspace(
    tmp_path,
) -> None:
    client, store, _supervisor = _client(
        tmp_path,
        request_workspace=TenantWorkspaceContext("tenant-1", tmp_path),
    )

    import asyncio

    asyncio.run(
        _create_run(
            store,
            run_id="subagent-running",
            session_id="session-1",
            status="running",
        ),
    )

    response = client.get("/subagents/runs", params={"chat_id": "chat-1"})

    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()["runs"]] == [
        "subagent-running",
    ]


def test_snapshot_returns_slim_current_chat_runs(tmp_path) -> None:
    client, store, supervisor = _client(tmp_path)
    started_at = datetime.now(timezone.utc) - timedelta(seconds=30)

    async def prepare() -> None:
        running = await _create_run(
            store,
            run_id="subagent-running",
            session_id="session-1",
            status="running",
        )
        store._write(  # pylint: disable=protected-access
            running.model_copy(update={"started_at": started_at}),
        )
        await _create_run(
            store,
            run_id="subagent-completed",
            session_id="session-1",
            status="completed",
        )
        await _create_run(
            store,
            run_id="subagent-other-chat",
            session_id="session-2",
            status="running",
        )

    import asyncio

    asyncio.run(prepare())

    response = client.get("/subagents/runs", params={"chat_id": "chat-1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["chat_id"] == "chat-1"
    assert payload["session_id"] == "session-1"
    assert [item["run_id"] for item in payload["runs"]] == [
        "subagent-running",
        "subagent-completed",
    ]
    running = payload["runs"][0]
    assert running["status"] == "running"
    assert running["stoppable"] is True
    assert running["budget_consumption"]["timeout_ms"] == 120_000
    assert running["budget_consumption"]["turns_used"] == 0
    assert running["budget_consumption"]["max_turns"] == 4
    assert running["budget_consumption"]["ratio"] > 0
    assert "effective_policy" not in running
    assert "delegation_spec" not in running
    assert "stderr_tail" not in running
    completed = payload["runs"][1]
    assert completed["summary_preview"] == "完成" * 80
    assert completed["stoppable"] is False
    assert completed["budget_consumption"]["turns_used"] == 3
    assert completed["budget_consumption"]["max_turns"] == 4
    assert supervisor.wait_calls == 1


def test_monitor_snapshot_includes_nickname_and_definition_match(
    tmp_path,
) -> None:
    client, store, _supervisor = _client(tmp_path)

    async def prepare() -> None:
        await _create_run(
            store,
            run_id="subagent-running",
            session_id="session-1",
            status="running",
            nickname="研究员",
            definition_match=DefinitionMatchMetadata(
                matched=False,
            ),
        )

    import asyncio

    asyncio.run(prepare())

    response = client.get("/subagents/runs", params={"chat_id": "chat-1"})

    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["nickname"] == "研究员"
    assert run["definition_match"]["matched"] is False


def test_cancel_running_run_marks_cancelled(tmp_path) -> None:
    client, store, supervisor = _client(tmp_path)

    async def prepare() -> None:
        await _create_run(
            store,
            run_id="subagent-running",
            session_id="session-1",
            status="running",
        )

    import asyncio

    asyncio.run(prepare())

    response = client.post(
        "/subagents/runs/subagent-running/cancel",
        json={"chat_id": "chat-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["status"] == "cancelled"
    assert payload["run"]["stoppable"] is False
    assert supervisor.cancel_calls == ["subagent-running"]


@pytest.mark.parametrize("status", ["pending", "completed", "failed"])
def test_cancel_non_running_run_is_not_stoppable(tmp_path, status) -> None:
    client, store, supervisor = _client(tmp_path)

    async def prepare() -> None:
        await _create_run(
            store,
            run_id="subagent-target",
            session_id="session-1",
            status=status,
        )

    import asyncio

    asyncio.run(prepare())

    response = client.post(
        "/subagents/runs/subagent-target/cancel",
        json={"chat_id": "chat-1"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "subagent run is not running"
    assert supervisor.cancel_calls == []


def test_cancel_run_from_another_chat_returns_not_found(tmp_path) -> None:
    client, store, supervisor = _client(tmp_path)

    async def prepare() -> None:
        await _create_run(
            store,
            run_id="subagent-other-chat",
            session_id="session-2",
            status="running",
        )

    import asyncio

    asyncio.run(prepare())

    response = client.post(
        "/subagents/runs/subagent-other-chat/cancel",
        json={"chat_id": "chat-1"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "subagent run not found"
    assert supervisor.cancel_calls == []


def test_cancel_not_manageable_run_reports_conflict(tmp_path) -> None:
    client, store, supervisor = _client(tmp_path)

    async def prepare() -> None:
        await _create_run(
            store,
            run_id="subagent-unmanageable",
            session_id="session-1",
            status="running",
        )

    import asyncio

    asyncio.run(prepare())

    async def not_manageable(_scope, run_id):
        return BackgroundSubAgentNotManageable(run_id=run_id)

    supervisor.cancel = not_manageable

    response = client.post(
        "/subagents/runs/subagent-unmanageable/cancel",
        json={"chat_id": "chat-1"},
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "background_subagent_run_not_manageable"
    )


def test_cancel_race_to_terminal_run_is_not_stoppable(tmp_path) -> None:
    client, store, supervisor = _client(tmp_path)

    async def prepare() -> None:
        await _create_run(
            store,
            run_id="subagent-race",
            session_id="session-1",
            status="running",
        )

    import asyncio

    asyncio.run(prepare())

    async def completed_before_cancel(_scope, run_id):
        return await store.finish(
            run_id,
            AgentResult(
                task_id="task-1",
                agent_run_id=run_id,
                agent_name="plan-researcher",
                status="completed",
                summary="finished",
                metrics=Metrics(elapsed_ms=10_000),
            ),
        )

    supervisor.cancel = completed_before_cancel

    response = client.post(
        "/subagents/runs/subagent-race/cancel",
        json={"chat_id": "chat-1"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "subagent run is not running"
