# -*- coding: utf-8 -*-
"""Real Agent steps await complete SQL/NAS recovery publications."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.model import ChatResponse
from agentscope.pipeline import stream_printing_messages
from agentscope.tool import Toolkit, ToolResponse

from swe.app.conversation_storage.errors import UnknownCommit
from swe.app.conversation_storage.session_adapter import SemanticBatch
from tests.integrated.conversation_storage.test_runner_integration_mysql import (
    query_fixture,
)
from tests.unit.app.conversation_storage.test_semantic_runtime import (
    ApprovalAgent,
    ScriptedModel,
    text_in,
)


async def run_live(query, question, agent):
    await query.storage.db.execute("UPDATE swe_conversation_chats SET epoch=1")
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="one"))
    async with query.admit([question]):
        await query.bind_runtime(runtime)
        return [
            frame
            async for frame in query.stream(
                stream_printing_messages([agent], agent(question)),
            )
        ]


def agent_for(script, toolkit=None, agent_type=ReActAgent):
    agent = agent_type(
        "assistant",
        "Answer clearly",
        ScriptedModel(script),
        OpenAIChatFormatter(),
        toolkit=toolkit or Toolkit(),
        memory=InMemoryMemory(),
        parallel_tool_calls=False,
    )
    agent.set_console_output_enabled(False)
    return agent


async def test_tool_result_is_readable_from_mysql_nas_before_next_model(
    conversation_test_database,
    tmp_path,
):
    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    effects = []

    async def read_root():
        root = await storage.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        assert root["future_module"] == {"nested": [1, 2]}
        assert root["managed_cron_receipts"] == {"receipt": True}
        return root

    async def record() -> ToolResponse:
        assert "call-one" in text_in(await read_root())
        effects.append("tool")
        return ToolResponse(content=[{"type": "text", "text": "tool-fact"}])

    async def script(turn):
        if turn == 1:
            yield ChatResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "call-one",
                        "name": "record",
                        "input": {},
                    },
                ],
            )
        else:
            assert "tool-fact" in text_in(await read_root())
            effects.append("second model")
            yield ChatResponse(content=[{"type": "text", "text": "done"}])

    toolkit = Toolkit()
    toolkit.register_tool_function(record)
    try:
        frames = await run_live(query, question, agent_for(script, toolkit))
        assert effects == ["tool", "second model"]
        assert frames[-1][1] is True
        assert query.terminal_status == "completed"
        assert "tool-fact" in text_in(await read_root())
    finally:
        await execution.__aexit__(None, None, None)


@pytest.mark.parametrize("bound", ["bytes", "events", "time"])
async def test_bounded_stream_root_is_readable_before_message_enters_memory(
    conversation_test_database,
    tmp_path,
    bound,
):
    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    query.semantic_batch = SemanticBatch(
        3 if bound == "bytes" else 100000,
        2 if bound == "events" else 1000,
        0.01 if bound == "time" else 100,
    )
    published = asyncio.Event()
    commit_state = execution.commit_state

    async def observe_commit(root):
        result = await commit_state(root)
        if "中" in text_in(root):
            published.set()
        return result

    execution.commit_state = observe_commit

    async def script(turn):
        yield ChatResponse(content=[{"type": "text", "text": "中"}])
        if bound == "time":
            await asyncio.wait_for(published.wait(), 30)
        elif bound == "events":
            yield ChatResponse(content=[{"type": "text", "text": "中间"}])
        root = await storage.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        assert "中" in text_in(root)
        assert not any(
            msg.role == "assistant" for msg, _ in agent.memory.content
        )
        yield ChatResponse(content=[{"type": "text", "text": "中文完整"}])

    agent = agent_for(script)
    try:
        await run_live(query, question, agent)
        assert query.terminal_status == "completed"
    finally:
        await execution.__aexit__(None, None, None)


async def test_approval_session_boundary_is_durable_before_next_step(
    conversation_test_database,
    tmp_path,
):
    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )

    async def script(turn):
        if turn == 1:
            yield ChatResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "approval-call",
                        "name": "restricted",
                        "input": {},
                    },
                ],
            )
        else:
            root = await storage.session_for("console").get_session_state_dict(
                "session",
                "alice",
            )
            assert "approval_required" in text_in(root)
            assert "tool_guard_denied" in text_in(root)
            yield ChatResponse(content=[{"type": "text", "text": "waiting"}])

    agent = agent_for(script, agent_type=ApprovalAgent)
    agent._request_context = {"session_id": "session", "channel": "console"}
    agent._tool_guard_approval_service = SimpleNamespace(
        create_pending=AsyncMock(
            return_value=SimpleNamespace(request_id="pending"),
        ),
        cancel_stale_pending_for_tool_call=AsyncMock(),
    )
    agent._notify_approval_pending = AsyncMock()
    try:
        await run_live(query, question, agent)
        agent._tool_guard_approval_service.create_pending.assert_awaited_once()
    finally:
        await execution.__aexit__(None, None, None)


async def test_unknown_semantic_publication_stops_and_reconciles_original_key(
    conversation_test_database,
    tmp_path,
):
    storage, execution, _, query, question = await query_fixture(
        conversation_test_database,
        tmp_path,
    )
    effects = []
    original = execution.adapter.publications.commit
    pending = []

    async def lose_response(operation):
        pending.append(operation)
        raise UnknownCommit(operation)

    async def record() -> ToolResponse:
        effects.append("tool")
        execution.adapter.publications.commit = lose_response
        return ToolResponse(
            content=[{"type": "text", "text": "durable result"}],
        )

    async def script(turn):
        effects.append("model")
        yield ChatResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "call-one",
                    "name": "record",
                    "input": {},
                },
            ],
        )

    toolkit = Toolkit()
    toolkit.register_tool_function(record)
    try:
        with pytest.raises(UnknownCommit):
            await run_live(query, question, agent_for(script, toolkit))
        assert effects == ["model", "tool"]
        assert query.pending_operation == pending[0]
        assert (await storage.publications.get(pending[0]))[
            "state"
        ] == "prepared"
        execution.adapter.publications.commit = original
        await execution.__aexit__(None, None, None)
        await query.finish("failed")
        assert (await storage.publications.get(pending[0]))[
            "state"
        ] == "committed"
        assert query.terminal_status == "interrupted"
        assert effects == ["model", "tool"]
        root = await storage.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        assert "durable result" in text_in(root)
        assert root["future_module"] == {"nested": [1, 2]}
    finally:
        execution.adapter.publications.commit = original
        await execution.__aexit__(None, None, None)
