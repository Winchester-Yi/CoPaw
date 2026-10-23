# -*- coding: utf-8 -*-
"""Staged checkpoint-compaction budget decision coverage."""

import asyncio

import pytest
from agentscope.message import Msg

from swe.agents.hooks.memory_compaction import ContextBudgetDecision
from swe.agents.hooks.memory_compaction import decide_context_budget
from swe.agents.hooks.memory_compaction import MemoryCompactionHook
from swe.agents.memory.chat_checkpoint import CheckpointRecord
from swe.agents.memory.chat_checkpoint import CheckpointEvent
from swe.agents.memory.conversation_archive import CheckpointArchiveState
from swe.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager
from swe.agents.utils.swe_token_counter import SweEstimateTokenCounter
from swe.config.config import ContextCompactConfig
from swe.config.config import ToolResultCompactConfig


def test_budget_stages_follow_confirmed_65_5_80_90_contract() -> None:
    config = ContextCompactConfig()

    assert decide_context_budget(64, 100, config).stage == "normal"
    assert decide_context_budget(65, 100, config).stage == "governance"
    assert decide_context_budget(69, 100, config).precompaction_watermark == 0
    assert decide_context_budget(70, 100, config).precompaction_watermark == 1
    assert decide_context_budget(80, 100, config).stage == "active"
    assert decide_context_budget(90, 100, config).stage == "emergency"


def test_budget_decision_rejects_invalid_window() -> None:
    config = ContextCompactConfig()

    try:
        decide_context_budget(1, 0, config)
    except ValueError as exc:
        assert "max_input_length" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("zero input window must be rejected")


@pytest.mark.asyncio
async def test_projected_tokens_sum_online_messages_and_fixed_text() -> None:
    counter = SweEstimateTokenCounter(token_count_estimate_divisor=1.0)
    message = Msg(name="user", role="user", content="m" * 400)
    fixed_text = "system prompt and compressed summary"

    message_tokens = await counter.count(messages=[message.to_dict()])
    fixed_text_tokens = await counter.count(messages=[], text=fixed_text)

    assert await MemoryCompactionHook._count_projected_tokens(
        counter,
        [message],
        fixed_text,
    ) == (message_tokens + fixed_text_tokens)


@pytest.mark.asyncio
async def test_projected_tokens_include_structured_tool_result_output() -> (
    None
):
    counter = SweEstimateTokenCounter(token_count_estimate_divisor=1.0)
    message = Msg(
        name="tool",
        role="assistant",
        content=[
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "read_file",
                "output": [{"type": "text", "text": "r" * 400}],
            },
        ],
    )
    fixed_text = "system prompt"
    fixed_text_tokens = await counter.count(messages=[], text=fixed_text)

    projected_tokens = await MemoryCompactionHook._count_projected_tokens(
        counter,
        [message],
        fixed_text,
    )

    assert projected_tokens >= fixed_text_tokens + 400


@pytest.mark.asyncio
async def test_governance_schedules_only_new_watermarks() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    manager = SimpleNamespace(schedule_precompaction=AsyncMock())
    hook = MemoryCompactionHook(manager)
    agent = SimpleNamespace(
        _request_context={"chat_id": "chat-1"},
        memory=SimpleNamespace(),
        model=object(),
        formatter=object(),
    )
    running = SimpleNamespace(
        max_input_length=100,
        context_compact=ContextCompactConfig(),
    )

    assert await hook._apply_checkpoint_budget_stage(agent, running, [], 65)
    await asyncio.sleep(0)
    assert await hook._apply_checkpoint_budget_stage(agent, running, [], 69)
    assert await hook._apply_checkpoint_budget_stage(agent, running, [], 70)
    await asyncio.sleep(0)

    assert manager.schedule_precompaction.await_count == 2
    assert (
        manager.schedule_precompaction.await_args_list[0].kwargs["watermark"]
        == 0
    )
    assert (
        manager.schedule_precompaction.await_args_list[1].kwargs["watermark"]
        == 1
    )
    await asyncio.gather(*hook._precompaction_tasks.values())


@pytest.mark.asyncio
async def test_governance_scheduling_is_side_effect_only() -> None:
    from inspect import signature
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    manager = SimpleNamespace(schedule_precompaction=AsyncMock())
    hook = MemoryCompactionHook(manager)
    agent = SimpleNamespace(
        model=object(),
        formatter=object(),
        memory=SimpleNamespace(),
    )
    decision = ContextBudgetDecision(65, 0.65, "governance", 0)

    hook._schedule_governance_precompaction(
        agent,
        [],
        "chat-1",
        decision,
        ("chat-1", 1),
    )

    assert (
        signature(hook._schedule_governance_precompaction).return_annotation
        is None
    )
    await asyncio.gather(*hook._precompaction_tasks.values())


@pytest.mark.asyncio
async def test_active_stage_installs_ready_candidate_before_reme() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    manager = SimpleNamespace(
        install_ready_precompaction=AsyncMock(return_value=True),
    )
    hook = MemoryCompactionHook(manager)
    agent = SimpleNamespace(
        _request_context={"chat_id": "chat-1"},
        memory=SimpleNamespace(),
    )
    running = SimpleNamespace(
        max_input_length=100,
        context_compact=ContextCompactConfig(),
    )

    assert await hook._apply_checkpoint_budget_stage(agent, running, [], 80)
    manager.install_ready_precompaction.assert_awaited_once_with(
        chat_id="chat-1",
        messages=[],
        memory=agent.memory,
    )


@pytest.mark.asyncio
async def test_active_candidate_remeasures_before_one_legacy_fallback() -> (
    None
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    manager = SimpleNamespace(
        install_ready_precompaction=AsyncMock(return_value=True),
    )
    hook = MemoryCompactionHook(manager)
    agent = SimpleNamespace(
        _request_context={"chat_id": "chat-1"},
        memory=SimpleNamespace(),
    )
    running = SimpleNamespace(
        max_input_length=100,
        context_compact=ContextCompactConfig(),
    )
    remeasure = AsyncMock(return_value=80)

    assert not await hook._apply_checkpoint_budget_stage(
        agent,
        running,
        [],
        80,
        remeasure,
    )
    remeasure.assert_awaited_once()


@pytest.mark.asyncio
async def test_install_checkpoint_stage_requests_legacy_fallback_when_active() -> (
    None
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    manager = SimpleNamespace(
        install_ready_precompaction=AsyncMock(return_value=True),
    )
    hook = MemoryCompactionHook(manager)
    agent = SimpleNamespace(
        _request_context={"chat_id": "chat-1"},
        memory=SimpleNamespace(),
    )
    running = SimpleNamespace(
        max_input_length=100,
        context_compact=ContextCompactConfig(),
    )
    decision = ContextBudgetDecision(80, 0.8, "active", None)
    remeasure = AsyncMock(return_value=80)

    assert not await hook._install_checkpoint_stage(
        agent,
        running,
        [],
        "chat-1",
        decision,
        remeasure,
    )
    manager.install_ready_precompaction.assert_awaited_once_with(
        chat_id="chat-1",
        messages=[],
        memory=agent.memory,
    )
    remeasure.assert_awaited_once()


@pytest.mark.asyncio
async def test_emergency_stage_installs_degraded_reference_checkpoint_once() -> (
    None
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    manager = SimpleNamespace(
        install_ready_precompaction=AsyncMock(return_value=False),
        install_degraded_checkpoint=AsyncMock(),
    )
    hook = MemoryCompactionHook(manager)
    agent = SimpleNamespace(
        _request_context={"chat_id": "chat-1"},
        memory=SimpleNamespace(),
    )
    running = SimpleNamespace(
        max_input_length=100,
        context_compact=ContextCompactConfig(),
    )

    assert not await hook._apply_checkpoint_budget_stage(
        agent,
        running,
        [],
        90,
    )
    manager.install_degraded_checkpoint.assert_awaited_once_with(
        chat_id="chat-1",
        messages=[],
        memory=agent.memory,
    )


@pytest.mark.asyncio
async def test_emergency_degradation_remeasures_then_retries_reme_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    message = Msg(name="user", role="user", content="message-1")
    message.id = "message-1"
    memory = SimpleNamespace(
        get_compressed_summary=lambda: "",
        get_memory=AsyncMock(return_value=[message]),
        archive_compacted_messages=AsyncMock(return_value=None),
        update_compressed_summary=AsyncMock(),
    )
    manager = SimpleNamespace(
        agent_id="default",
        tenant_id=None,
        compact_tool_result=AsyncMock(),
        check_context=AsyncMock(return_value=([message], [], True)),
        install_ready_precompaction=AsyncMock(return_value=False),
        install_degraded_checkpoint=AsyncMock(return_value=True),
        compact_memory=AsyncMock(return_value="summary"),
    )
    running = SimpleNamespace(
        max_input_length=100,
        memory_compact_threshold=100,
        memory_compact_reserve=0,
        tool_result_compact=ToolResultCompactConfig(enabled=False),
        memory_summary=SimpleNamespace(memory_summary_enabled=False),
        context_compact=ContextCompactConfig(),
    )
    token_counter = SimpleNamespace(
        count=AsyncMock(side_effect=[0, 90, 0, 90, 0]),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.load_agent_config",
        lambda *_args, **_kwargs: SimpleNamespace(running=running),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.get_swe_token_counter",
        lambda _config: token_counter,
    )
    agent = SimpleNamespace(
        name="agent",
        sys_prompt="",
        memory=memory,
        model=object(),
        formatter=object(),
        print=AsyncMock(),
        _request_context={"chat_id": "chat-1"},
    )

    await MemoryCompactionHook(manager)(agent, {})

    manager.install_degraded_checkpoint.assert_awaited_once_with(
        chat_id="chat-1",
        messages=[message],
        memory=memory,
    )
    assert token_counter.count.await_count == 5
    manager.compact_memory.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_context_compaction_skips_checkpoint_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    message = Msg(name="user", role="user", content="message-1")
    message.id = "message-1"
    memory = SimpleNamespace(
        get_compressed_summary=lambda: "",
        get_memory=AsyncMock(return_value=[message]),
    )
    manager = SimpleNamespace(
        agent_id="default",
        tenant_id=None,
        compact_tool_result=AsyncMock(),
        check_context=AsyncMock(return_value=([message], [], True)),
        schedule_precompaction=AsyncMock(),
        install_ready_precompaction=AsyncMock(return_value=True),
        compact_memory=AsyncMock(return_value="summary"),
    )
    running = SimpleNamespace(
        max_input_length=100,
        memory_compact_threshold=100,
        memory_compact_reserve=0,
        tool_result_compact=ToolResultCompactConfig(enabled=False),
        memory_summary=SimpleNamespace(memory_summary_enabled=False),
        context_compact=ContextCompactConfig(context_compact_enabled=False),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.load_agent_config",
        lambda *_args, **_kwargs: SimpleNamespace(running=running),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.get_swe_token_counter",
        lambda _config: SimpleNamespace(count=AsyncMock(return_value=90)),
    )
    agent = SimpleNamespace(
        name="agent",
        sys_prompt="",
        memory=memory,
        model=object(),
        formatter=object(),
        print=AsyncMock(),
        _request_context={"chat_id": "chat-1"},
    )

    await MemoryCompactionHook(manager)(agent, {})

    manager.check_context.assert_not_awaited()
    manager.schedule_precompaction.assert_not_awaited()
    manager.install_ready_precompaction.assert_not_awaited()
    manager.compact_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_precompaction_persists_revision_bound_candidate() -> (
    None
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    chat_id = "e1574b04-2ca1-44c5-82a0-d9d5cc410f3d"
    record = CheckpointRecord.new(chat_id=chat_id, epoch=1)
    event = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="message_added",
        facts={"message_id": "message-1", "role": "user"},
        source_refs=("message:message-1",),
    )
    message = Msg(name="user", role="user", content="message-1")
    message.id = "message-1"
    store = SimpleNamespace(
        read_checkpoint_prefix=AsyncMock(
            return_value=CheckpointArchiveState(record, (event,), 1),
        ),
        write_pending_candidate=AsyncMock(),
    )
    manager = object.__new__(ReMeLightMemoryManager)
    manager.get_in_memory_memory = lambda **_kwargs: SimpleNamespace(
        chat_checkpoint_store=store,
    )

    assert await manager.schedule_precompaction(
        chat_id=chat_id,
        watermark=0,
        messages=[message],
        memory=SimpleNamespace(
            content=[(message, [])],
            chat_checkpoint_store=store,
        ),
    )
    store.read_checkpoint_prefix.assert_awaited_once_with(chat_id, 1)
    candidate = store.write_pending_candidate.await_args.args[1]
    assert candidate.base_revision == 0
    assert candidate.record.revision == 1
    assert candidate.record.applied_event_sequence == 1
    assert candidate.source_message_ids == ("message-1",)
    assert candidate.record.critical_context[0].evidence_refs == (
        "message:message-1",
    )


@pytest.mark.asyncio
async def test_schedule_precompaction_rejects_ambiguous_duplicate_online_ids() -> (
    None
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    chat_id = "e1574b04-2ca1-44c5-82a0-d9d5cc410f3d"
    record = CheckpointRecord.new(chat_id=chat_id, epoch=1)
    first = SimpleNamespace(id="duplicate-id", role="user")
    second = SimpleNamespace(id="duplicate-id", role="user")
    event = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="message_added",
        facts={"message_id": "duplicate-id", "role": "user"},
        source_refs=("message:duplicate-id",),
    )
    store = SimpleNamespace(
        read_checkpoint_prefix=AsyncMock(
            return_value=CheckpointArchiveState(record, (event,), 1),
        ),
        write_pending_candidate=AsyncMock(),
    )
    memory = SimpleNamespace(
        chat_checkpoint_store=store,
        content=[(first, []), (second, [])],
    )
    manager = object.__new__(ReMeLightMemoryManager)
    manager.get_in_memory_memory = lambda **_kwargs: memory

    assert not await manager.schedule_precompaction(
        chat_id=chat_id,
        watermark=0,
        messages=[first, second],
        memory=memory,
    )
    store.write_pending_candidate.assert_not_awaited()
