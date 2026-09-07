# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from swe.app.runner.context_usage import (
    CONTEXT_USAGE_STATE_KEY,
    ContextUsageSnapshot,
    capture_context_usage,
)


class _CountingTokenCounter:
    async def count(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        text: str | None = None,
        **_kwargs,
    ) -> int:
        if text is not None:
            return len(text.encode("utf-8"))
        payloads = [message.get("content", "") for message in messages]
        tokens = (
            len(
                json.dumps(
                    payloads,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8"),
            )
            if payloads
            else 0
        )
        if tools is not None:
            tokens += len(
                json.dumps(
                    tools,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8"),
            )
        return tokens


class _FailingTokenCounter:
    async def count(self, *_args, **_kwargs) -> int:
        raise RuntimeError("counter unavailable")


class _Memory:
    def __init__(
        self,
        *,
        online: list[Msg] | None = None,
        prefix: list[Msg] | None = None,
    ) -> None:
        self.online = online or []
        self.prefix = prefix or []

    async def get_memory(self, *, prepend_summary: bool = True) -> list[Msg]:
        if prepend_summary:
            return [*self.prefix, *self.online]
        return list(self.online)


class _Toolkit:
    def __init__(self, schemas: list[dict]) -> None:
        self.schemas = schemas
        self.inactive_schemas = [{"name": "inactive-tool"}]

    def get_json_schemas(self) -> list[dict]:
        return self.schemas


def _agent(
    *,
    sys_prompt: str = "system",
    online: list[Msg] | None = None,
    prefix: list[Msg] | None = None,
    schemas: list[dict] | None = None,
    max_input_length: int = 1000,
):
    compact = SimpleNamespace(
        lightweight_governance_ratio=0.65,
        memory_compact_ratio=0.80,
        emergency_compact_ratio=0.90,
    )
    return SimpleNamespace(
        sys_prompt=sys_prompt,
        memory=_Memory(online=online, prefix=prefix),
        toolkit=_Toolkit(schemas or []),
        _agent_config=SimpleNamespace(
            running=SimpleNamespace(
                max_input_length=max_input_length,
                context_compact=compact,
            ),
        ),
    )


def _agent_state(*messages: Msg, **extra) -> dict:
    return {
        "memory": {
            "content": [[message.to_dict(), []] for message in messages],
        },
        **extra,
    }


@pytest.fixture
def counter(monkeypatch) -> _CountingTokenCounter:
    value = _CountingTokenCounter()
    monkeypatch.setattr(
        "swe.app.runner.context_usage.get_swe_token_counter",
        lambda _config: value,
    )
    return value


@pytest.mark.asyncio
async def test_empty_history_still_counts_fixed_system_context(
    counter,
) -> None:
    sampled_at = datetime(2026, 9, 2, tzinfo=timezone.utc)

    snapshot = await capture_context_usage(
        _agent(sys_prompt="fixed prompt"),
        _agent_state(),
        sampled_at=sampled_at,
    )

    assert CONTEXT_USAGE_STATE_KEY == "context_usage"
    assert snapshot.schema_version == 1
    assert snapshot.system_context_tokens == len("fixed prompt")
    assert snapshot.tool_definition_tokens == 0
    assert snapshot.conversation_tokens == 0
    assert snapshot.used_tokens == sum(
        (
            snapshot.system_context_tokens,
            snapshot.tool_definition_tokens,
            snapshot.conversation_tokens,
        ),
    )
    assert snapshot.as_of == sampled_at
    assert "fixed prompt" not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_prefix_is_counted_once_and_cleaned_online_state_is_not_duplicated(
    counter,
) -> None:
    stale_live_message = Msg("user", "do not count live copy", "user")
    prefix = Msg(
        "user",
        "# Memories\nremembered\n# Summary\ncompressed",
        "user",
    )
    cleaned_message = Msg("user", "cleaned message", "user")
    agent = _agent(
        sys_prompt="sys",
        online=[stale_live_message],
        prefix=[prefix],
    )

    snapshot = await capture_context_usage(
        agent,
        _agent_state(cleaned_message),
    )

    expected_prefix_tokens = await counter.count(
        messages=[prefix.to_dict()],
    )
    expected_conversation_tokens = await counter.count(
        messages=[cleaned_message.to_dict()],
    )
    assert (
        snapshot.system_context_tokens == len("sys") + expected_prefix_tokens
    )
    assert snapshot.conversation_tokens == expected_conversation_tokens
    assert "do not count live copy" not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_only_active_tool_schemas_are_counted(counter) -> None:
    active = [{"name": "active-tool", "parameters": {"type": "object"}}]

    snapshot = await capture_context_usage(
        _agent(schemas=active),
        _agent_state(),
    )

    assert snapshot.tool_definition_tokens == await counter.count(
        messages=[],
        tools=active,
    )
    assert "inactive-tool" not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_structured_tool_messages_are_counted(counter) -> None:
    tool_use = Msg(
        "Friday",
        [
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "search",
                "input": {"q": "context"},
            },
        ],
        "assistant",
    )
    tool_result = Msg(
        "search",
        [
            {
                "type": "tool_result",
                "id": "call-1",
                "output": [{"type": "text", "text": "result"}],
            },
        ],
        "assistant",
    )

    snapshot = await capture_context_usage(
        _agent(),
        _agent_state(tool_use, tool_result),
    )

    assert snapshot.conversation_tokens == await counter.count(
        messages=[tool_use.to_dict(), tool_result.to_dict()],
    )


@pytest.mark.asyncio
async def test_external_state_is_excluded_from_snapshot(counter) -> None:
    visible = Msg("user", "online", "user")
    state = _agent_state(
        visible,
        archived_history=[{"content": "archived"}],
        composer_draft="unsent",
        token_usage={"input_tokens": 999999},
    )

    snapshot = await capture_context_usage(_agent(), state)

    assert snapshot.conversation_tokens == await counter.count(
        messages=[visible.to_dict()],
    )
    serialized = snapshot.model_dump_json()
    assert "archived" not in serialized
    assert "unsent" not in serialized
    assert "999999" not in serialized


@pytest.mark.asyncio
async def test_overflow_clamps_only_remaining_capacity(monkeypatch) -> None:
    class _FixedCounter:
        async def count(
            self,
            messages,
            tools=None,
            text=None,
            **_kwargs,
        ) -> int:
            del messages
            if text is not None:
                return 30
            if tools is not None:
                return 40
            return 50

    monkeypatch.setattr(
        "swe.app.runner.context_usage.get_swe_token_counter",
        lambda _config: _FixedCounter(),
    )

    snapshot = await capture_context_usage(
        _agent(schemas=[{"name": "tool"}], max_input_length=100),
        _agent_state(Msg("user", "message", "user")),
    )

    assert snapshot == ContextUsageSnapshot(
        used_tokens=120,
        max_tokens=100,
        remaining_tokens=0,
        usage_ratio=1.2,
        system_context_tokens=30,
        tool_definition_tokens=40,
        conversation_tokens=50,
        governance_threshold_ratio=0.65,
        active_threshold_ratio=0.80,
        emergency_threshold_ratio=0.90,
        status="overflow",
        as_of=snapshot.as_of,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("used", "status"),
    [
        (64, "normal"),
        (65, "governance"),
        (80, "active"),
        (90, "emergency"),
        (100, "overflow"),
    ],
)
async def test_status_follows_runtime_thresholds(
    monkeypatch,
    used: int,
    status: str,
) -> None:
    class _FixedCounter:
        calls = 0

        async def count(self, *_args, **_kwargs) -> int:
            self.calls += 1
            return used if self.calls == 1 else 0

    monkeypatch.setattr(
        "swe.app.runner.context_usage.get_swe_token_counter",
        lambda _config: _FixedCounter(),
    )

    snapshot = await capture_context_usage(
        _agent(max_input_length=100),
        _agent_state(),
    )

    assert snapshot.status == status


@pytest.mark.asyncio
async def test_counter_failure_propagates_without_calling_a_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "swe.app.runner.context_usage.get_swe_token_counter",
        lambda _config: _FailingTokenCounter(),
    )
    model = SimpleNamespace(
        __call__=lambda *_args: pytest.fail("model called")
    )
    agent = _agent()
    agent.model = model

    with pytest.raises(RuntimeError, match="counter unavailable"):
        await capture_context_usage(agent, _agent_state())
