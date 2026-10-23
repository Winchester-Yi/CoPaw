# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swe.agents.memory.base_memory_manager import BaseMemoryManager
from swe.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager
from swe.config.config import ToolResultCompactConfig


class _ConcreteMemoryManager(BaseMemoryManager):
    async def start(self) -> None:  # pragma: no cover - test stub
        return None

    async def close(self) -> bool:  # pragma: no cover - test stub
        return True

    async def compact_tool_result(self, **kwargs) -> None:  # pragma: no cover
        del kwargs
        return None

    async def check_context(self, **kwargs) -> tuple:  # pragma: no cover
        del kwargs
        return (), (), True

    async def compact_memory(
        self,
        messages,
        previous_summary="",
        **kwargs,
    ) -> str:  # pragma: no cover
        del messages, previous_summary, kwargs
        return ""

    async def summary_memory(
        self,
        messages,
        **kwargs,
    ) -> str:  # pragma: no cover
        del messages, kwargs
        return ""

    async def archive_checkpoint_messages(
        self,
        **kwargs,
    ) -> Any:  # pragma: no cover
        del kwargs

    async def schedule_precompaction(
        self,
        **kwargs,
    ) -> bool:  # pragma: no cover
        del kwargs
        return False

    async def install_ready_precompaction(
        self,
        **kwargs,
    ) -> bool:  # pragma: no cover
        del kwargs
        return False

    async def recover_evidence(self, **kwargs) -> Any:  # pragma: no cover
        del kwargs
        return []

    async def reset_context_epoch(self, **kwargs) -> Any:  # pragma: no cover
        del kwargs

    async def dream_memory(
        self,
        tenant_id: str | None = None,
        **kwargs,
    ) -> None:  # pragma: no cover
        del tenant_id, kwargs

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ) -> Any:  # pragma: no cover
        del query, max_results, min_score
        return None

    def get_in_memory_memory(self, **kwargs) -> Any:  # pragma: no cover
        del kwargs


@pytest.mark.asyncio
async def test_add_async_summary_task_freezes_model_snapshot() -> None:
    observed = []
    manager = _ConcreteMemoryManager(
        working_dir="/tmp/ws",
        agent_id="default",
        tenant_id="tenant-a",
    )

    async def fake_summary_memory(messages, **kwargs) -> str:
        del messages
        observed.append(
            (
                kwargs.get("_bound_chat_model"),
                kwargs.get("_bound_formatter"),
            ),
        )
        return "done"

    manager.summary_memory = fake_summary_memory  # type: ignore[method-assign]

    manager.add_async_summary_task(
        messages=[],
        chat_model="model-a",
        formatter="formatter-a",
        scope_id="session-a",
    )
    await manager.await_summary_tasks("session-a")

    assert observed == [
        ("model-a", "formatter-a"),
    ]


@pytest.mark.asyncio
async def test_await_summary_tasks_isolates_scopes() -> None:
    observed = []
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    manager = _ConcreteMemoryManager(
        working_dir="/tmp/ws",
        agent_id="default",
        tenant_id="tenant-a",
    )

    async def fake_summary_memory(messages, **kwargs) -> str:
        del messages
        scope_id = str(kwargs.get("_scope_id") or "")
        observed.append(("start", scope_id))
        if scope_id == "session-a":
            await release_a.wait()
        else:
            await release_b.wait()
        observed.append(("done", scope_id))
        return scope_id

    manager.summary_memory = fake_summary_memory  # type: ignore[method-assign]

    manager.add_async_summary_task(
        messages=[],
        chat_model="model-a",
        formatter="formatter-a",
        scope_id="session-a",
        _scope_id="session-a",
    )
    manager.add_async_summary_task(
        messages=[],
        chat_model="model-b",
        formatter="formatter-b",
        scope_id="session-b",
        _scope_id="session-b",
    )

    release_a.set()
    result_a = await manager.await_summary_tasks("session-a")

    assert "session-a" in result_a
    assert manager.get_summary_task_count("session-a") == 0
    assert manager.get_summary_task_count("session-b") == 1

    release_b.set()
    result_b = await manager.await_summary_tasks("session-b")

    assert "session-b" in result_b
    assert manager.get_summary_task_count("session-b") == 0


@pytest.mark.asyncio
async def test_dream_memory_uses_fresh_execution_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed = {}

    class FakeDreamAgent:
        def __init__(self, **kwargs):
            observed["model"] = kwargs["model"]
            observed["formatter"] = kwargs["formatter"]
            self._total_input_tokens = 0
            self._total_output_tokens = 0

        async def reply(self, _msg):
            return SimpleNamespace(get_text_content=lambda: "dream-ok")

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.ReActAgent",
        FakeDreamAgent,
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.capture_current_trace_context",
        lambda: {
            "trace_id": "trace-dream",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "source_id": "source-1",
        },
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        lambda agent_id, tenant_id=None: SimpleNamespace(
            agent_id=agent_id,
            language="zh",
            running=SimpleNamespace(
                tool_result_compact=ToolResultCompactConfig(),
            ),
        ),
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.resolve_tool_result_compact_config",
        lambda config: config,
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.set_current_workspace_dir",
        lambda _path: None,
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.set_current_recent_max_bytes",
        lambda _value: None,
    )

    async def fake_create_execution_model_formatter(*, trace_context=None):
        observed["trace_context"] = trace_context
        return "fresh-model", "fresh-formatter"

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager.working_dir = str(tmp_path)
    manager.summary_toolkit = object()
    manager._warn_if_version_mismatch = lambda: None
    manager._create_execution_model_formatter_async = (
        fake_create_execution_model_formatter
    )
    manager._get_dream_prompt = lambda language, current_date: "dream prompt"
    manager._log_dream_result = lambda **kwargs: observed.setdefault(
        "log_status",
        kwargs["status"],
    )

    await manager.dream_memory(tenant_id="tenant-a", trigger="manual")

    assert observed["model"] == "fresh-model"
    assert observed["formatter"] == "fresh-formatter"
    assert observed["trace_context"]["trace_id"] == "trace-dream"
    assert observed["log_status"] == "success"


@pytest.mark.asyncio
async def test_compact_memory_uses_bound_execution_model_without_shared_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    class FakeReMe:
        async def compact_memory(self, **kwargs):
            observed["model"] = kwargs["as_llm"]
            observed["formatter"] = kwargs["as_llm_formatter"]
            return {"history_compact": "ok", "is_valid": True}

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        lambda agent_id, tenant_id=None: SimpleNamespace(
            agent_id=agent_id,
            language="zh",
            workspace_dir="/tmp/ws",
            running=SimpleNamespace(
                max_input_length=128000,
                context_compact=SimpleNamespace(
                    memory_compact_ratio=0.5,
                    compact_with_thinking_block=False,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.get_swe_token_counter",
        lambda _config: "token-counter",
    )

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager._reme = FakeReMe()
    manager._warn_if_version_mismatch = lambda: None

    result = await manager.compact_memory(
        messages=[],
        previous_summary="",
        _bound_chat_model="bound-model",
        _bound_formatter="bound-formatter",
    )

    assert result == "ok"
    assert observed["model"] == "bound-model"
    assert observed["formatter"] == "bound-formatter"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_summary_completion_releases_tasks_and_keeps_bounded_status(
    outcome,
):
    manager = _ConcreteMemoryManager("/tmp/ws", "default")

    async def summary(messages, **kwargs):
        if outcome == "failure":
            raise ValueError("secret=bad " + "中" * 1000)
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return "中" * 1000

    manager.summary_memory = summary
    manager.add_async_summary_task([], scope_id="a")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert manager.get_summary_task_count("a") == 0
    assert "a" not in manager._summary_tasks_by_scope
    assert manager.has_summary_task_results("a")
    result = await manager.await_summary_tasks("a")
    assert len(result.encode()) < 1100
    assert "secret=bad" not in result
    assert not manager.has_summary_task_results("a")


@pytest.mark.asyncio
async def test_summary_await_snapshot_preserves_added_tasks_and_parallel_waiters():
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    releases = [asyncio.Event(), asyncio.Event()]

    async def summary(messages, **kwargs):
        await releases[messages[0]].wait()
        return str(messages[0])

    manager.summary_memory = summary
    manager.add_async_summary_task([0], scope_id="a")
    first = asyncio.create_task(manager.await_summary_tasks("a"))
    second = asyncio.create_task(manager.await_summary_tasks("a"))
    await asyncio.sleep(0)
    manager.add_async_summary_task([1], scope_id="a")
    releases[0].set()
    assert "completed: 0" in await first
    assert "completed: 0" in await second
    assert manager.get_summary_task_count("a") == 1
    releases[1].set()
    assert "completed: 1" in await manager.await_summary_tasks("a")


@pytest.mark.asyncio
async def test_cancelled_summary_waiter_preserves_background_and_history():
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    release = asyncio.Event()

    async def summary(messages, **kwargs):
        if messages:
            await release.wait()
        return "saved"

    manager.summary_memory = summary
    manager.add_async_summary_task([], scope_id="a")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    manager.add_async_summary_task([1], scope_id="a")
    waiter = asyncio.create_task(manager.await_summary_tasks("a"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert manager.get_summary_task_count("a") == 1
    assert manager.has_summary_task_results("a")
    release.set()
    assert (await manager.await_summary_tasks("a")).count(
        "completed: saved",
    ) == 2


@pytest.mark.asyncio
async def test_summary_history_limits_expiry_and_weak_registry(monkeypatch):
    import swe.agents.memory.base_memory_manager as module

    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    for i in range(1000):
        manager.add_async_summary_task([], scope_id=str(i))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not manager._summary_tasks_by_scope
    assert len(manager._summary_results) == 256
    assert manager._summary_result_bytes <= 512 * 1024
    monkeypatch.setattr(module.time, "monotonic", lambda: float("inf"))
    await module.prune_summary_task_results()
    assert not manager._summary_results
    assert manager._summary_result_bytes == 0
    assert manager.summary_tasks == []
    assert not manager._summary_tasks_by_scope


@pytest.mark.asyncio
async def test_summary_context_allowlist_copies_mutables_and_redacts_secrets():
    from contextvars import ContextVar
    from swe.config import context
    from swe.constant import env_var_overrides, EnvVarLoader
    from swe.tracing.sanitizer import register_sensitive_values

    unknown = ContextVar("summary-test-unknown", default=None)
    unknown.set(object())
    headers = {"x-test": "before"}
    token = context.current_passthrough_headers.set(headers)
    register_sensitive_values(["very-secret-value"])
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    observed = {}

    async def summary(messages, **kwargs):
        observed["unknown"] = unknown.get()
        observed["headers"] = context.current_passthrough_headers.get()
        observed["env"] = EnvVarLoader.get_str("SUMMARY_TEST_VAR", "")
        observed["trace"] = kwargs.get("_summary_trace_context")
        return "very-secret-value"

    manager.summary_memory = summary
    try:
        with env_var_overrides({"SUMMARY_TEST_VAR": "snapshot"}):
            manager.add_async_summary_task([])
            headers["x-test"] = "after"
        result = await manager.await_summary_tasks()
    finally:
        context.current_passthrough_headers.reset(token)
    assert observed["unknown"] is None
    assert observed["headers"] == {"x-test": "before"}
    assert observed["env"] == "snapshot"
    assert "very-secret-value" not in result


@pytest.mark.asyncio
async def test_summary_context_preserves_workload_and_source_config_snapshot():
    from swe.app.source_system_config.models import (
        EffectiveSourceSystemConfig,
        SourceSystemConfig,
    )
    from swe.app.source_system_config.runtime import (
        bind_source_system_config,
        get_current_source_system_config,
        resolve_tool_result_compact_config,
    )
    from swe.config.llm_workload import (
        LLM_WORKLOAD_CRON,
        bind_llm_workload,
        get_current_llm_workload,
    )

    raw_config = SourceSystemConfig.model_validate(
        {
            "llm_max_qpm": 7,
            "tool_result_compact": {"recent_max_bytes": 32000},
        },
    )
    source_config = EffectiveSourceSystemConfig(
        source_id="source-a",
        config=raw_config.merged_with_defaults(),
        raw_config=raw_config,
    )
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    observed = {}

    async def summary(messages, **kwargs):
        del messages, kwargs
        current = get_current_source_system_config()
        observed["workload"] = get_current_llm_workload()
        observed["qpm"] = current.config.llm_max_qpm
        observed["recent_max_bytes"] = resolve_tool_result_compact_config(
            ToolResultCompactConfig(),
        ).recent_max_bytes
        return "done"

    manager.summary_memory = summary
    with (
        bind_llm_workload(LLM_WORKLOAD_CRON),
        bind_source_system_config(
            source_config,
        ),
    ):
        manager.add_async_summary_task([])
        updated_raw_config = SourceSystemConfig.model_validate(
            {
                "llm_max_qpm": 100,
                "tool_result_compact": {"recent_max_bytes": 50000},
            },
        )
        source_config.raw_config = updated_raw_config
        source_config.config = updated_raw_config.merged_with_defaults()

    await manager.await_summary_tasks()

    assert observed == {
        "workload": LLM_WORKLOAD_CRON,
        "qpm": 7,
        "recent_max_bytes": 32000,
    }


@pytest.mark.asyncio
async def test_summary_close_cleans_tasks_without_reme_and_rejects_new(
    monkeypatch,
):
    import swe.agents.memory.base_memory_manager as module

    monkeypatch.setattr(module, "SUMMARY_CLOSE_GRACE_SECONDS", 0)
    monkeypatch.setattr(module, "SUMMARY_CLOSE_CANCEL_SECONDS", 0.1)
    manager = object.__new__(ReMeLightMemoryManager)
    BaseMemoryManager.__init__(manager, "/tmp/ws", "default")
    manager._reme = None
    manager._warn_if_version_mismatch = lambda: None

    async def summary(messages, **kwargs):
        await asyncio.Event().wait()

    manager.summary_memory = summary
    manager.add_async_summary_task([])
    await asyncio.sleep(0)
    assert await manager.close()
    assert not manager._summary_tasks_by_scope
    assert not manager._summary_results
    assert await manager.close()
    with pytest.raises(RuntimeError, match="closing"):
        manager.add_async_summary_task([])


@pytest.mark.asyncio
async def test_await_summary_command_reports_already_completed_results():
    from swe.agents.command_handler import CommandHandler

    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    manager.add_async_summary_task([], scope_id="a")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    handler = object.__new__(CommandHandler)
    handler.memory_manager = manager
    handler._has_memory_manager = lambda: True
    handler._summary_task_scope_id = lambda: "a"

    async def system_message(text):
        return text

    handler._make_system_msg = system_message
    result = await handler._process_await_summary([])
    assert "Summary Tasks Complete" in result
    assert "completed:" in result
    assert "All tasks have finished" not in result


@pytest.mark.asyncio
async def test_summary_close_failure_retains_uncancellable_task_and_backend(
    monkeypatch,
):
    import swe.agents.memory.base_memory_manager as module

    monkeypatch.setattr(module, "SUMMARY_CLOSE_GRACE_SECONDS", 0)
    monkeypatch.setattr(module, "SUMMARY_CLOSE_CANCEL_SECONDS", 0)
    manager = object.__new__(ReMeLightMemoryManager)
    BaseMemoryManager.__init__(manager, "/tmp/ws", "default")
    closed = []

    class Backend:
        async def close(self):
            closed.append(True)
            return True

    manager._reme = Backend()
    manager._warn_if_version_mismatch = lambda: None
    release = asyncio.Event()

    async def summary(messages, **kwargs):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return "done"

    manager.summary_memory = summary
    manager.add_async_summary_task([])
    await asyncio.sleep(0)
    assert not await manager.close()
    assert manager.get_summary_task_count() == 1
    assert not closed
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await manager.close()
    assert closed == [True]
    assert await manager.close()
    assert closed == [True]


@pytest.mark.asyncio
async def test_summary_releases_model_exception_payload_and_parent_context():
    import gc
    import weakref
    from contextvars import ContextVar

    class Payload:
        pass

    parent_context = ContextVar("summary-test-heavy")
    parent_payload = Payload()
    parent_ref = weakref.ref(parent_payload)
    token = parent_context.set(parent_payload)
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    model = Payload()
    model_ref = weakref.ref(model)

    async def summary(messages, **kwargs):
        payload = kwargs["_bound_chat_model"]
        raise ValueError("failed")

    manager.summary_memory = summary
    manager.add_async_summary_task([], chat_model=model)
    parent_context.reset(token)
    del token, parent_payload, model
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    assert parent_ref() is None
    assert model_ref() is None
    manager_ref = weakref.ref(manager)
    del manager
    gc.collect()
    assert manager_ref() is None


@pytest.mark.asyncio
async def test_summary_await_uses_bounded_fallback_after_history_eviction():
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    release = asyncio.Event()

    async def summary(messages, **kwargs):
        await release.wait()
        return str(messages[0])

    manager.summary_memory = summary
    for i in range(20):
        manager.add_async_summary_task([i], scope_id="a")
    waiter = asyncio.create_task(manager.await_summary_tasks("a"))
    await asyncio.sleep(0)
    release.set()
    result = await waiter
    assert result.count("completed:") == 20
    assert not manager.has_summary_task_results("a")


@pytest.mark.asyncio
async def test_summary_reme_factory_uses_explicit_plain_trace(monkeypatch):
    observed = {}
    manager = object.__new__(ReMeLightMemoryManager)
    manager._reme = SimpleNamespace(summary_memory=None)

    async def factory(*, trace_context):
        observed["trace"] = trace_context
        raise ValueError("stop after factory")

    manager._create_execution_model_formatter_async = factory
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.capture_current_trace_context",
        lambda: {"trace_id": "wrong-parent"},
    )
    trace = {"trace_id": "original"}
    with pytest.raises(ValueError, match="stop after factory"):
        await manager.summary_memory([], _summary_trace_context=trace)
    assert observed["trace"] == trace


@pytest.mark.asyncio
async def test_summary_command_observes_completion_before_done_callback():
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    manager.add_async_summary_task([], scope_id="a")
    # First loop turn completes the coroutine; its callback is queued for next turn.
    await asyncio.sleep(0)
    assert manager.get_summary_task_count("a") == 0
    assert manager.has_summary_task_results("a")
    assert "completed:" in await manager.await_summary_tasks("a")


@pytest.mark.asyncio
async def test_summary_malformed_exception_does_not_break_completion_callback():
    class BrokenMessage(Exception):
        def __str__(self):
            raise ValueError("cannot render")

    manager = _ConcreteMemoryManager("/tmp/ws", "default")

    async def summary(messages, **kwargs):
        raise BrokenMessage()

    manager.summary_memory = summary
    manager.add_async_summary_task([])
    result = await manager.await_summary_tasks()
    assert "failed:" in result
    assert not manager._summary_tasks_by_scope


@pytest.mark.asyncio
async def test_summary_history_accounts_keys_and_scope_bytes():
    manager = _ConcreteMemoryManager("/tmp/ws", "default")
    manager.add_async_summary_task([], scope_id="中" * 100)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    record = next(iter(manager._summary_results.values()))
    record_fields = {
        "task_id": record.task_id,
        "scope": record.scope,
        "status": record.status,
        "finished_at": record.finished_at,
        "expires_at": record.expires_at,
        "bounded_message": record.message,
    }
    minimum = sum(
        len(str(key).encode()) + len(str(value).encode())
        for key, value in record_fields.items()
    )
    assert manager._summary_result_bytes >= minimum
