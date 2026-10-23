# -*- coding: utf-8 -*-
"""Acceptance coverage across real Stop, session, model, archive and share stages."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse

from swe.agents.hook_runtime.models import (
    CommandHookHandlerConfig,
    HookConfig,
    HookEventName,
    HookMatcherGroupConfig,
    HookSessionOverlay,
)
from swe.agents.memory.conversation_archive import attach_conversation_archive
from swe.app.answer_turn.coordinator import AnswerTurnCoordinator
from swe.app.answer_turn.in_memory import (
    InMemoryApproval,
    InMemoryExecution,
    InMemoryGoal,
    InMemorySession,
    InMemoryStream,
    InMemorySubagent,
)
from swe.app.chat_sharing.service import ChatSharingService
from swe.app.runner.api import (
    _archive_page,
    _build_chat_history,
    _slice_answer_turn,
)
from swe.app.runner.models import ChatSpec
from swe.app.runner.runner import AgentRunner, _QueryTurnOutcome
from swe.app.runner.session import SafeJSONSession
from swe.app.runner.turn_lifecycle import _stream_standard_completion_lifecycle

APPEND_TEXT = "  **脚本补充**\n\nPreserve this Markdown exactly.  "
PRIVATE_REASON = "private-handler-reason"
PRIVATE_STDERR = "private-execution-diagnostic"


class CapturingModel(ChatModelBase):
    def __init__(self):
        super().__init__(model_name="local-capture", stream=False)
        self.prompts = []

    async def __call__(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return ChatResponse(content=[{"type": "text", "text": "next answer"}])


@pytest_asyncio.fixture(params=[2, 3])
# Keep the full production-stage acceptance setup together for both consumers.
async def appended_session(  # pylint: disable=too-many-statements
    tmp_path,
    request,
):
    """Only the initial model turn is supplied; all Stop stages are production."""
    coordinator = AnswerTurnCoordinator(
        stream=InMemoryStream(),
        execution=InMemoryExecution(),
        session=InMemorySession(),
        goal=InMemoryGoal(),
        subagent=InMemorySubagent(),
        approval=InMemoryApproval(),
    )
    chat_id = str(uuid4())
    lease = await coordinator.start_or_attach(
        chat_id,
        {},
        AsyncMock(),
        msgid="question",
    )
    owner = object.__new__(AgentRunner)
    owner._answer_turn_coordinator = coordinator
    owner.workspace_dir = tmp_path
    owner.tenant_id = "default"
    owner.agent_id = "test"
    owner.session = SafeJSONSession(str(tmp_path / "sessions"))
    await owner.session.save_merged_state(
        "s",
        user_id="u",
        state={"other_module": {"keep": True}},
    )
    if request.param == 3:
        await owner.session.migrate_terminal_turns(
            "s",
            user_id="u",
            dry_run=False,
        )

    payload_log = tmp_path / "append-inputs.jsonl"
    output = {
        "decision": "allow",
        "reason": PRIVATE_REASON,
        "hookSpecificOutput": {"appendText": APPEND_TEXT},
    }
    # Absolute executables are subject to the normal tenant path boundary.
    python_name = Path(sys.executable).name
    command_env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
    }
    script = (
        "import json, pathlib, sys\n"
        "payload = json.load(sys.stdin)\n"
        "with pathlib.Path(sys.argv[1]).open('a') as log:\n"
        "    log.write(json.dumps(payload) + '\\n')\n"
        f"print({PRIVATE_STDERR!r}, file=sys.stderr)\n"
        f"print(json.dumps({output!r}))\n"
    )
    hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.STOP: [
                HookMatcherGroupConfig(
                    id="completion",
                    hooks=[
                        CommandHookHandlerConfig(
                            id="validate",
                            argv=[
                                python_name,
                                "-c",
                                'print(\'{"decision":"allow","reason":"approved"}\')',
                            ],
                            env=command_env,
                        ),
                        CommandHookHandlerConfig(
                            id="supplement",
                            argv=[python_name, "-c", script, str(payload_log)],
                            env=command_env,
                            outputAppend=True,
                            includeConversationSnapshot=True,
                        ),
                    ],
                ),
            ],
        },
    )
    memory = InMemoryMemory()
    question = Msg("user", "question", "user")
    question.id = "question"
    original = Msg("Friday", "approved answer", "assistant")
    await memory.add(question)
    agent = SimpleNamespace(
        memory=memory,
        state_dict=lambda: {"memory": memory.state_dict()},
    )
    runtime = SimpleNamespace(
        agent=agent,
        session_id="s",
        user_id="u",
        skip_history=False,
        tenant_hooks=hooks,
        agent_config=SimpleNamespace(),
        hook_overlay=HookSessionOverlay(),
    )
    query = SimpleNamespace(
        channel="console",
        session_id="s",
        user_id="u",
        trace_id=None,
        channel_meta={
            "chat_id": chat_id,
            "turn_id": lease.identity.turn_id,
            "answer_turn_identity": lease.identity,
        },
    )
    outcome = _QueryTurnOutcome()

    async def model_turn(**_):
        await memory.add(original)
        outcome.assistant_response = original.content
        yield original, True

    owner._stream_agent_turns = model_turn
    async with owner.session.execution("s", user_id="u") as execution:
        runtime.session_execution = execution
        delivered = [
            message
            async for message, last in _stream_standard_completion_lifecycle(
                owner,
                request=query,
                runtime=runtime,
                plan=SimpleNamespace(
                    original_user_message="question",
                    turn_msgs=[],
                ),
                outcome=outcome,
            )
        ]
        assert [msg.get_text_content() for msg in delivered] == [
            original.content,
            APPEND_TEXT,
        ]
        assert outcome.stop_append_approved and outcome.stop_append_started
        assert not outcome.stop_output_buffer_required
        assert (
            execution.state["agent"]["memory"]["content"][-1][0]["id"]
            == delivered[-1].id
        )

    inputs = [
        json.loads(line) for line in payload_log.read_text().splitlines()
    ]
    assert len(inputs) == 1
    assert inputs[0]["assistant_response"] == original.content
    assert inputs[0]["conversation_snapshot"][-1]["content"] == [
        {"type": "text", "text": original.content},
    ]
    state = await owner.session.get_session_state_dict("s", user_id="u")
    assert state["schema_version"] == request.param
    assert state["other_module"] == {"keep": True}
    restored = InMemoryMemory()
    restored.load_state_dict(state["agent"]["memory"])
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    chat = ChatSpec(id=chat_id, session_id="s", user_id="u", channel="console")
    history = await _build_chat_history(
        chat,
        session=owner.session,
        workspace=workspace,
    )
    assert [message.id for message in history.messages] == [
        question.id,
        original.id,
        delivered[-1].id,
    ]
    marker = delivered[-1].metadata["stop_append"]
    assert marker["message_id"] == delivered[-1].id
    assert marker["turn_id"] == lease.identity.turn_id
    assert marker["chat_id"] == chat_id
    assert history.messages[-1].metadata["metadata"]["stop_append"] == marker
    assert (
        _slice_answer_turn(history.messages, msgid=question.id)
        == history.messages
    )
    return SimpleNamespace(
        memory=restored,
        history=history,
        chat=chat,
        workspace=workspace,
        marker=marker,
    )


@pytest.mark.asyncio
async def test_committed_command_append_reaches_actual_next_model_call(
    appended_session,
):
    scenario = appended_session
    model = CapturingModel()
    agent = ReActAgent(
        name="Friday",
        sys_prompt="System policy",
        model=model,
        formatter=OpenAIChatFormatter(),
        memory=scenario.memory,
    )
    agent.set_console_output_enabled(False)
    await scenario.memory.add(Msg("user", "next question", "user"))
    response = await agent._reasoning()
    assert response.get_text_content() == "next answer"
    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    expected = [
        ("system", "System policy"),
        ("user", "question"),
        ("assistant", "approved answer"),
        ("assistant", APPEND_TEXT),
        ("user", "next question"),
    ]
    assert [(item["role"], item["content"]) for item in prompt] == [
        (role, [{"type": "text", "text": text}]) for role, text in expected
    ]
    serialized = json.dumps(prompt)
    for private in (
        PRIVATE_REASON,
        PRIVATE_STDERR,
        "stop_append",
        "supplement",
    ):
        assert private not in serialized


@pytest.mark.asyncio
async def test_compaction_archive_and_share_preserve_same_turn_append(
    appended_session,
    tmp_path,
):
    scenario = appended_session
    messages = await scenario.memory.get_memory()
    attach_conversation_archive(
        scenario.memory,
        tmp_path / "dialog",
        scenario.chat.id,
    )
    await scenario.memory.archive_compacted_messages(messages)
    assert await scenario.memory.get_memory() == []
    page = await _archive_page(scenario.workspace, scenario.chat.id, None, 50)
    assert [message.id for message in page.messages] == [
        message.id for message in scenario.history.messages
    ]
    assert (
        page.messages[-1].metadata["metadata"]["stop_append"]
        == scenario.marker
    )
    assert page.messages[-1].content[0].text == APPEND_TEXT
    assert _slice_answer_turn(page.messages, msgid="question") == page.messages

    store = SimpleNamespace(
        create=AsyncMock(),
        get=AsyncMock(),
        record_access=AsyncMock(),
    )
    sharing = ChatSharingService(store, tmp_path / "shares")
    record = await sharing.create_snapshot(
        chat_id=scenario.chat.id,
        chat_name="Append acceptance",
        messages=page.messages,
        selected_turn_ids=["question"],
        turn_statuses={"question": "completed"},
        creator_id="u",
    )
    store.get.return_value = record
    snapshot = await sharing.get_snapshot(record.token)
    assert [message["role"] for message in snapshot["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]
    appended = snapshot["messages"][-1]
    assert appended["id"] == scenario.marker["message_id"]
    assert appended["metadata"]["metadata"]["stop_append"] == scenario.marker
    assert appended["content"][0]["text"] == APPEND_TEXT
