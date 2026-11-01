# -*- coding: utf-8 -*-
"""Runtime durability must precede the next model/tool step, not queued frames."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.pipeline import stream_printing_messages
from agentscope.tool import Toolkit, ToolResponse

from swe.app.conversation_storage.errors import StorageError, UnknownCommit
from swe.app.conversation_storage.runner_integration import IndexedQuery
from swe.app.conversation_storage.session_adapter import SemanticBatch
from swe.agents.tool_guard_mixin import ToolGuardMixin
from swe.security.tool_guard.models import ToolGuardResult
from tests.unit.app.conversation_storage.test_runner_integration import (
    setup_runtime,
)


def text_in(root):
    return str(root.get("agent", {}).get("memory", {}).get("content", []))


class ScriptedModel(ChatModelBase):
    def __init__(self, script):
        super().__init__("durability-test", stream=True)
        self.script = script
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.script(self.calls)


def runtime_fixture(
    script,
    *,
    batch=None,
    tool=None,
    fail=None,
    agent_type=ReActAgent,
):
    storage, owner, execution, request, question, events = setup_runtime()
    durable = []

    async def save_state(identity, state):
        state = deepcopy(state)
        if fail is not None:
            fail(state)
        state["revision"] += 1
        durable.append(state)
        return state

    execution.adapter._save_state = save_state
    toolkit = Toolkit()
    if tool is not None:
        toolkit.register_tool_function(tool)
    model = ScriptedModel(script)
    agent = agent_type(
        "assistant",
        "Answer clearly",
        model,
        OpenAIChatFormatter(),
        toolkit=toolkit,
        memory=InMemoryMemory(),
        parallel_tool_calls=False,
    )
    agent.set_console_output_enabled(False)
    storage.archive = SimpleNamespace(bind_memory=Mock())
    query = IndexedQuery(storage, owner, request, execution)
    query.semantic_batch = batch or SemanticBatch(100000, 1000, 100)
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat"))
    return query, runtime, question, durable, model


async def run(query, runtime, question):
    async with query.admit([question]):
        await query.bind_runtime(runtime)
        return [
            frame
            async for frame in query.stream(
                stream_printing_messages(
                    [runtime.agent],
                    runtime.agent(question),
                ),
            )
        ]


async def test_real_agent_publishes_tool_call_and_result_before_advancing():
    durable = None
    calls = []

    async def record() -> ToolResponse:
        assert durable and "call-one" in text_in(durable[-1])
        assert durable[-1]["unknown"] == {"keep": True}
        calls.append("tool")
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
            assert "tool-fact" in text_in(durable[-1])
            calls.append("second-model")
            yield ChatResponse(content=[{"type": "text", "text": "done"}])

    query, runtime, question, durable, _ = runtime_fixture(script, tool=record)
    await run(query, runtime, question)
    assert calls == ["tool", "second-model"]


async def test_archive_snapshot_keeps_printed_occurrence_not_yet_in_memory():
    async def script(turn):
        yield ChatResponse(content=[{"type": "text", "text": "unused"}])

    query, runtime, question, _, _ = runtime_fixture(script)
    async with query.admit([question]):
        await query.bind_runtime(runtime)
        publisher = query.semantic_publisher
        message = Msg("assistant", "partial peer output", "assistant")
        await publisher.pre_print(
            runtime.agent,
            {"msg": message, "last": False},
        )
        provider = query.storage.archive.bind_memory.call_args.args[3]
        root = provider()
        if asyncio.iscoroutine(root):
            root = await root
        assert "partial peer output" in text_in(root)
        await publisher.close()


@pytest.mark.parametrize("bound", ["bytes", "events", "time"])
async def test_real_stream_text_is_durable_before_model_message_enters_memory(
    bound,
):
    durable = None
    runtime = None

    async def script(turn):
        yield ChatResponse(content=[{"type": "text", "text": "中"}])
        if bound == "time":
            await asyncio.sleep(0.04)
        elif bound == "events":
            yield ChatResponse(content=[{"type": "text", "text": "中间"}])
        assert not any(
            msg.role == "assistant" for msg, _ in runtime.agent.memory.content
        )
        assert durable and "中" in text_in(durable[-1])
        assert durable[-1]["unknown"] == {"keep": True}
        yield ChatResponse(content=[{"type": "text", "text": "中文完整"}])

    batch = SemanticBatch(
        3 if bound == "bytes" else 100000,
        2 if bound == "events" else 1000,
        0.01 if bound == "time" else 100,
    )
    query, runtime, question, durable, _ = runtime_fixture(script, batch=batch)
    await run(query, runtime, question)
    final = runtime.agent.memory.state_dict()["content"]
    assistant = [item for item in final if item[0]["role"] == "assistant"]
    assert len(assistant) == 1
    assert "中文完整" in str(assistant)


async def test_unknown_tool_result_publication_never_advances_to_next_model():
    calls = []

    async def record() -> ToolResponse:
        calls.append("tool")
        return ToolResponse(content=[{"type": "text", "text": "tool-fact"}])

    async def script(turn):
        calls.append("model")
        yield ChatResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "one",
                    "name": "record",
                    "input": {},
                },
            ],
        )

    def fail(state):
        if "tool-fact" in text_in(state):
            raise UnknownCommit("f" * 64)

    query, runtime, question, _, model = runtime_fixture(
        script,
        tool=record,
        fail=fail,
    )
    with pytest.raises(UnknownCommit):
        await run(query, runtime, question)
    assert calls == ["model", "tool"]
    assert model.calls == 1
    assert query.execution.pending_operation == "f" * 64
    assert query.terminal_status is None


class ApprovalAgent(ToolGuardMixin, ReActAgent):
    async def _acting_impl(self, tool_call):
        return await self._acting_with_approval(
            tool_call,
            tool_call["name"],
            ToolGuardResult(tool_name=tool_call["name"], params={}),
        )


async def test_real_approval_boundary_retains_pending_card_before_next_model():
    durable = None

    async def script(turn):
        if turn == 1:
            yield ChatResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "approve-one",
                        "name": "restricted",
                        "input": {},
                    },
                ],
            )
        else:
            assert "approval_required" in text_in(durable[-1])
            assert "pending" in text_in(durable[-1])
            assert "tool_guard_denied" in text_in(durable[-1])
            yield ChatResponse(content=[{"type": "text", "text": "waiting"}])

    query, runtime, question, durable, _ = runtime_fixture(
        script,
        agent_type=ApprovalAgent,
    )
    runtime.agent._request_context = {"session_id": "s", "channel": "console"}
    runtime.agent._tool_guard_lock = asyncio.Lock()
    runtime.agent._tool_guard_approval_service = SimpleNamespace(
        create_pending=AsyncMock(
            return_value=SimpleNamespace(request_id="approval-one"),
        ),
        cancel_stale_pending_for_tool_call=AsyncMock(),
    )
    runtime.agent._notify_approval_pending = AsyncMock()
    await run(query, runtime, question)
    runtime.agent._tool_guard_approval_service.create_pending.assert_awaited_once()
    assert query.terminal_status == "completed"


async def test_timed_unknown_publication_stops_stalled_model_without_terminal():
    effects = []

    async def script(turn):
        yield ChatResponse(content=[{"type": "text", "text": "partial"}])
        await asyncio.sleep(0.2)
        effects.append("advanced")
        yield ChatResponse(content=[{"type": "text", "text": "complete"}])

    def fail(state):
        if "partial" in text_in(state):
            raise UnknownCommit("e" * 64)

    query, runtime, question, _, model = runtime_fixture(
        script,
        batch=SemanticBatch(100000, 1000, 0.01),
        fail=fail,
    )
    with pytest.raises(UnknownCommit) as error:
        await run(query, runtime, question)
    assert error.value.operation_key == "e" * 64
    assert effects == []
    assert model.calls == 1
    assert query.terminal_status is None
    query.storage.executions.finish.assert_not_awaited()


async def test_terminal_keeps_committed_partial_stream_when_model_fails():
    async def script(turn):
        yield ChatResponse(
            content=[{"type": "text", "text": "retained partial"}],
        )
        raise RuntimeError("model failed after partial output")

    query, runtime, question, durable, _ = runtime_fixture(
        script,
        batch=SemanticBatch(1, 1000, 100),
    )
    with pytest.raises(RuntimeError, match="model failed"):
        await run(query, runtime, question)
    assert "retained partial" in text_in(durable[-1])
    terminal_root = query.storage.executions.finish.await_args.args[2]
    assert "retained partial" in text_in(terminal_root)
    assert terminal_root["unknown"] == {"keep": True}


async def test_unknown_approval_boundary_stops_inherited_guard_before_next_tool():
    effects = []

    class GuardAgent(ToolGuardMixin, ReActAgent):
        async def reply(self, msg):
            await self.memory.add(msg)
            response = await self._reasoning()
            for tool_call in response.get_content_blocks("tool_use"):
                await self._acting(tool_call)
            return response

        async def _reasoning(self, tool_choice=None):
            return await ReActAgent._reasoning(self, tool_choice)

        async def _acting_impl(self, tool_call):
            effects.append(tool_call["id"])
            await self.memory.add(Msg("system", "tool-fact", "system"))

    async def script(turn):
        yield ChatResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "one",
                    "name": "guarded",
                    "input": {},
                },
                {
                    "type": "tool_use",
                    "id": "two",
                    "name": "guarded",
                    "input": {},
                },
            ],
        )

    def fail(state):
        if "tool-fact" in text_in(state):
            raise UnknownCommit("d" * 64)

    query, runtime, question, _, model = runtime_fixture(
        script,
        agent_type=GuardAgent,
        fail=fail,
    )
    with pytest.raises(UnknownCommit):
        await run(query, runtime, question)
    assert effects == ["one"]
    assert model.calls == 1


async def test_closing_stream_drains_model_before_removing_durability_hooks():
    advanced = []
    stopped = asyncio.Event()

    async def script(turn):
        try:
            yield ChatResponse(content=[{"type": "text", "text": "partial"}])
            await asyncio.sleep(0.05)
            advanced.append("next model event")
            yield ChatResponse(content=[{"type": "text", "text": "late"}])
        finally:
            stopped.set()

    query, runtime, question, durable, _ = runtime_fixture(script)
    async with query.admit([question]):
        await query.bind_runtime(runtime)
        source = stream_printing_messages(
            [runtime.agent],
            runtime.agent(question),
        )
        stream = query.stream(source)
        await anext(stream)
        await stream.aclose()
        assert all(task.done() for task in query.semantic_publisher.producers)
        await source.aclose()
    await asyncio.sleep(0.06)
    assert advanced == []


async def test_partial_occurrence_does_not_replace_historical_duplicate_id():
    durable = None

    async def script(turn):
        yield ChatResponse(content=[{"type": "text", "text": "current"}])

    query, runtime, question, durable, _ = runtime_fixture(script)
    historical = Msg("assistant", "old", "assistant")
    historical.id = "duplicate"
    await runtime.agent.memory.add(historical)
    async with query.admit([question]):
        await query.bind_runtime(runtime)
        current = Msg("assistant", "current", "assistant")
        current.id = historical.id
        await runtime.agent.print(current, True)
        content = durable[-1]["agent"]["memory"]["content"]
        assert [row[0]["content"] for row in content] == ["old", "current"]
        await runtime.agent.memory.add(current)
        root = await query.semantic_publisher.snapshot()
        assert len(root["agent"]["memory"]["content"]) == 2
        await query.semantic_publisher.close()


async def test_parallel_boundary_failure_drains_started_peer_without_replay():
    effects = []
    peer_started = asyncio.Event()
    peer_drained = asyncio.Event()

    async def record(which: str) -> ToolResponse:
        effects.append(which)
        if which == "first":
            await peer_started.wait()
            return ToolResponse(
                content=[{"type": "text", "text": "tool-fact"}],
            )
        peer_started.set()
        try:
            await asyncio.sleep(1)
            effects.append("peer completed")
            return ToolResponse(content=[{"type": "text", "text": "late"}])
        finally:
            peer_drained.set()

    async def script(turn):
        effects.append("model")
        yield ChatResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "one",
                    "name": "record",
                    "input": {"which": "first"},
                },
                {
                    "type": "tool_use",
                    "id": "two",
                    "name": "record",
                    "input": {"which": "peer"},
                },
            ],
        )

    def fail(state):
        if "tool-fact" in text_in(state):
            raise UnknownCommit("c" * 64)

    query, runtime, question, _, model = runtime_fixture(
        script,
        tool=record,
        fail=fail,
    )
    runtime.agent.parallel_tool_calls = True
    with pytest.raises(UnknownCommit):
        await run(query, runtime, question)
    assert effects == ["model", "first", "peer"]
    assert peer_drained.is_set()
    assert all(task.done() for task in query.semantic_publisher.producers)
    assert model.calls == 1


@pytest.mark.parametrize("handler_type", ["prompt", "command", "http"])
@pytest.mark.parametrize("failure_site", ["tool_result", "post_context"])
@pytest.mark.parametrize(
    "error_type",
    [UnknownCommit, StorageError, ValueError],
)
async def test_configured_tool_failure_hook_fences_storage_failure(
    tmp_path,
    monkeypatch,
    handler_type,
    failure_site,
    error_type,
):
    from swe.agents.hook_runtime.models import (
        HookConfig,
        HookEventName,
        HookHandlerResult,
        HookOutput,
    )

    error = error_type("a" * 64)
    started = []

    class GuardAgent(ToolGuardMixin, ReActAgent):
        async def _acting_impl(self, tool_call):
            return await self._run_guarded_tool_call(
                None,
                tool_call,
                tool_call["name"],
                {},
                "",
            )

        async def _execute_guard_action_or_tool_call(self, *args):
            await self.memory.add(Msg("system", "tool-fact", "system"))
            return None

    async def script(turn):
        yield ChatResponse(content=[{"type": "text", "text": "unused"}])

    query, runtime, question, _, _ = runtime_fixture(
        script,
        agent_type=GuardAgent,
    )
    agent = runtime.agent
    agent._request_context = {"session_id": "s", "channel": "console"}
    agent._workspace_dir = tmp_path
    options = {
        "prompt": {"prompt": "Review the failure"},
        "command": {"command": "echo {}"},
        "http": {"url": "https://failure.invalid/hook"},
    }
    agent._agent_config = SimpleNamespace(
        hooks=HookConfig.model_validate(
            {
                "enabled": True,
                "events": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "id": "post",
                                    "type": "command",
                                    "command": "echo {}",
                                },
                            ],
                        },
                    ],
                    "PostToolUseFailure": [
                        {
                            "hooks": [
                                {
                                    "id": "failure",
                                    "type": handler_type,
                                    **options[handler_type],
                                },
                            ],
                        },
                    ],
                },
            },
        ),
    )
    agent._load_tenant_hook_config = HookConfig
    agent._extract_current_tool_response = lambda *args, **kwargs: None
    agent._emit_tool_trace_end = AsyncMock()

    async def execute(handler, context, *, workspace_dir):
        started.append((handler.type, context.hook_event_name))
        return HookHandlerResult(
            handler_id=handler.id,
            order=0,
            output=HookOutput(
                hook_specific_output={
                    "additionalContext": "post-context",
                },
            ),
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    original_add = agent.memory.add

    async def add(message, *args, **kwargs):
        target = (
            "tool-fact" if failure_site == "tool_result" else "post-context"
        )
        if target in message.get_text_content():
            if isinstance(error, UnknownCommit):
                query.execution.pending_operation = error.operation_key
            raise error
        return await original_add(message, *args, **kwargs)

    agent.memory.add = add
    async with query.admit([question]):
        await query.bind_runtime(runtime)
        try:
            with pytest.raises(error_type) as caught:
                await agent._acting(
                    {"id": "one", "name": "record", "input": {}},
                )
            assert caught.value is error
            failure_started = any(
                event == HookEventName.POST_TOOL_USE_FAILURE
                for _, event in started
            )
            if isinstance(error, StorageError):
                assert not failure_started
                assert query.semantic_publisher.error is error
                with pytest.raises(error_type) as next_step:
                    await query.semantic_publisher.pre_step(agent, {})
                assert next_step.value is error
            else:
                assert failure_started
                assert query.semantic_publisher.error is None
            if isinstance(error, UnknownCommit):
                assert query.execution.pending_operation == error.operation_key
        finally:
            await query.semantic_publisher.close()
    assert "_conversation_check_tool_failure" not in agent.__dict__


async def test_close_keeps_failure_fence_until_cancelled_tool_is_drained():
    started = asyncio.Event()
    cleanup_checks = []

    class GuardAgent(ToolGuardMixin, ReActAgent):
        async def _acting_impl(self, tool_call):
            return await self._run_guarded_tool_call(
                None,
                tool_call,
                tool_call["name"],
                {},
                "",
            )

        async def _execute_guard_action_or_tool_call(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_checks.append(self._conversation_check_tool_failure)
                raise RuntimeError("tool converted cancellation") from None

    async def script(turn):
        yield ChatResponse(content=[{"type": "text", "text": "unused"}])

    query, runtime, question, _, _ = runtime_fixture(
        script,
        agent_type=GuardAgent,
    )
    agent = runtime.agent
    previous = Mock()
    agent._conversation_check_tool_failure = previous
    agent._emit_tool_hook = AsyncMock()
    async with query.admit([question]):
        await query.bind_runtime(runtime)
        publisher = query.semantic_publisher
        producer = asyncio.create_task(
            agent._acting(
                {
                    "id": "cancel",
                    "name": "record",
                    "input": {},
                },
            ),
        )
        await started.wait()
        await publisher.close()
        assert producer.cancelled()
        assert cleanup_checks == [publisher.check_tool_failure]
        agent._emit_tool_hook.assert_not_awaited()
        assert agent._conversation_check_tool_failure is previous
        previous.assert_not_called()
