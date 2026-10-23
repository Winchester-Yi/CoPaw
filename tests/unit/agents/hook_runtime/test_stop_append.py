# -*- coding: utf-8 -*-
"""验证 Stop 追加协议、执行隔离和阶段预算。"""

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from swe.agents.hook_runtime.models import (
    CommandHookHandlerConfig,
    HookConfig,
    HookContext,
    HookEventName,
    HookHandlerResult,
    HookMatcherGroupConfig,
    HttpHookHandlerConfig,
    PromptHookHandlerConfig,
    LoadedSkillHookSource,
    HookSessionOverlay,
    HookOverlayEntry,
)
from swe.agents.hook_runtime.output import normalize_hook_output
from swe.agents.hook_runtime.resolver import HookResolver
from swe.agents.hook_runtime.runtime import HookRuntime
from swe.agents.hook_runtime.executor import execute_handler
from swe.config.config import HookRuntimeRunningConfig


def context(tmp_path):
    return HookContext(
        session_id="s",
        transcript_path="",
        cwd=str(tmp_path),
        hook_event_name=HookEventName.STOP,
        tenant_id="t",
        effective_tenant_id="t",
        user_id="u",
        agent_id="a",
        channel="console",
        assistant_response="answer",
    )


def handler(name="append", **kwargs):
    return CommandHookHandlerConfig(
        id=name,
        argv=["python", "hook.py"],
        outputAppend=True,
        **kwargs,
    )


def config(*handlers, event=HookEventName.STOP):
    return HookConfig(
        enabled=True,
        events={event: [HookMatcherGroupConfig(id="g", hooks=list(handlers))]},
    )


def test_append_config_contract():
    assert handler().output_append is True
    assert (
        CommandHookHandlerConfig(id="x", argv=["true"]).output_append is False
    )
    for kwargs in ({"once": True}, {"outputTransform": True}):
        with pytest.raises(ValidationError):
            config(handler(**kwargs))
    with pytest.raises(ValidationError):
        config(handler(), event=HookEventName.PRE_TOOL_USE)
    assert HookRuntimeRunningConfig().max_stop_append_seconds == 30
    for budget in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            HookRuntimeRunningConfig(max_stop_append_seconds=budget)


@pytest.mark.parametrize("specific", [{}, {"appendText": "  hello\n世界  "}])
def test_append_output_preserves_text(specific):
    result = normalize_hook_output(
        handler_id="x",
        order=0,
        raw_output={
            "decision": "allow",
            "reason": "ok",
            "hookSpecificOutput": specific,
        },
        event_name=HookEventName.STOP,
        output_append=True,
    )
    assert result.append_text == specific.get("appendText")


@pytest.mark.parametrize(
    "raw",
    [
        {"decision": "block", "reason": "no"},
        {"decision": "allow", "reason": ""},
        {"decision": "allow", "reason": "x" * 2001},
        {"decision": "allow", "reason": "ok", "continue": True},
        {
            "decision": "allow",
            "reason": "ok",
            "hookSpecificOutput": {"appendText": " "},
        },
        {
            "decision": "allow",
            "reason": "ok",
            "hookSpecificOutput": {"appendText": "界" * 5462},
        },
        {
            "decision": "allow",
            "reason": "ok",
            "hookSpecificOutput": {"appendText": None},
        },
        {
            "decision": "allow",
            "reason": "ok",
            "hookSpecificOutput": {"replacementText": "x"},
        },
    ],
)
def test_append_output_rejects_invalid_protocol(raw):
    with pytest.raises(ValueError):
        normalize_hook_output(
            handler_id="x",
            order=0,
            raw_output=raw,
            event_name=HookEventName.STOP,
            output_append=True,
        )


def test_append_plan_is_separate_and_source_qualified(tmp_path):
    resolver = HookResolver(
        tenant_config=config(handler()),
        agent_config=config(handler()),
    )
    ctx = context(tmp_path)
    assert not resolver.resolve_event_plan(ctx).handlers
    assert not resolver.resolve_stop_validator_plan(ctx).handlers
    assert not resolver.resolve_stop_transformer_plan(ctx).handlers
    assert [
        x.source for x in resolver.resolve_stop_append_plan(ctx).handlers
    ] == [
        "tenant",
        "agent",
    ]


@pytest.mark.asyncio
async def test_append_stage_keeps_completed_success_and_cancels_pending(
    tmp_path,
    monkeypatch,
):
    cleaned = asyncio.Event()

    async def execute(h, ctx, **kwargs):
        if h.id == "slow":
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.set()
        return HookHandlerResult(
            handler_id=h.id,
            order=0,
            append_text=" kept ",
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    runtime = HookRuntime(
        tenant_config=config(handler("fast"), handler("slow")),
    )
    results = await runtime.emit_stop_append(
        context(tmp_path),
        workspace_dir=tmp_path,
        max_append_seconds=0.03,
    )
    assert [(x.handler_id, x.text) for x in results] == [("fast", " kept ")]
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_append_stop_discards_results_and_awaits_cleanup(
    tmp_path,
    monkeypatch,
):
    stop = asyncio.Event()
    cleaned = asyncio.Event()

    async def execute(h, ctx, **kwargs):
        if h.id == "fast":
            stop.set()
            return HookHandlerResult(
                handler_id=h.id,
                order=0,
                append_text="hidden",
            )
        try:
            await asyncio.sleep(10)
        finally:
            cleaned.set()

    async def requested():
        return stop.is_set()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    runtime = HookRuntime(
        tenant_config=config(handler("fast"), handler("slow")),
    )
    assert (
        await runtime.emit_stop_append(
            context(tmp_path),
            workspace_dir=tmp_path,
            max_append_seconds=10,
            stop_requested=requested,
        )
        == []
    )
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_append_stop_during_timeout_cleanup_discards_results(
    tmp_path,
    monkeypatch,
):
    stop = asyncio.Event()

    async def execute(h, ctx, **kwargs):
        if h.id == "slow":
            try:
                await asyncio.sleep(10)
            finally:
                stop.set()
        return HookHandlerResult(
            handler_id=h.id,
            order=0,
            append_text="hidden",
        )

    async def requested():
        return stop.is_set()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    runtime = HookRuntime(
        tenant_config=config(handler("fast"), handler("slow")),
    )
    assert (
        await runtime.emit_stop_append(
            context(tmp_path),
            workspace_dir=tmp_path,
            max_append_seconds=0.02,
            stop_requested=requested,
        )
        == []
    )


@pytest.mark.asyncio
async def test_append_cumulative_limit_skips_whole_contribution(
    tmp_path,
    monkeypatch,
):
    async def execute(h, ctx, **kwargs):
        return HookHandlerResult(
            handler_id=h.id,
            order=0,
            append_text="a" * (16384 if h.id != "last" else 1),
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    runtime = HookRuntime(
        tenant_config=config(
            *(handler(str(i)) for i in range(5)),
            handler("last"),
        ),
    )
    results = await runtime.emit_stop_append(
        context(tmp_path),
        workspace_dir=tmp_path,
        max_append_seconds=1,
    )
    assert [x.handler_id for x in results] == ["0", "1", "2", "3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script, failure",
    [
        ("import sys;sys.stderr.write('secret');sys.exit(2)", "non_zero_exit"),
        ("print('x'*262145)", "output_limit"),
        ("import sys;sys.stderr.write('x'*65537)", "output_limit"),
    ],
)
async def test_append_command_failures_are_diagnostic(
    tmp_path,
    script,
    failure,
):
    (tmp_path / "hook.py").write_text(script)
    result = await execute_handler(
        handler(failPolicy="block"),
        context(tmp_path),
        workspace_dir=tmp_path,
    )
    assert result.failed and result.failure_type == failure
    assert result.decision == "none"
    assert "secret" not in result.reason


@pytest.mark.asyncio
async def test_append_command_success(tmp_path):
    raw = {
        "decision": "allow",
        "reason": "ok",
        "hookSpecificOutput": {"appendText": " hi\n"},
    }
    (tmp_path / "hook.py").write_text("print(" + repr(json.dumps(raw)) + ")")
    result = await execute_handler(
        handler(),
        context(tmp_path),
        workspace_dir=tmp_path,
    )
    assert result.append_text == " hi\n"


@pytest.mark.asyncio
async def test_append_snapshot_is_frozen_once_and_handlers_are_isolated(
    tmp_path,
    monkeypatch,
):
    captures = []
    seen = []

    async def snapshot():
        captures.append(True)
        return {"messages": [{"role": "assistant", "content": "before"}]}

    async def execute(h, ctx, **kwargs):
        seen.append((ctx.assistant_response, str(ctx.conversation_snapshot)))
        ctx.assistant_response = "changed"
        return HookHandlerResult(handler_id=h.id, order=0, append_text=h.id)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    runtime = HookRuntime(
        tenant_config=config(
            handler("a", includeConversationSnapshot=True),
            handler("b", includeConversationSnapshot=True),
        ),
    )
    await runtime.emit_stop_append(
        context(tmp_path),
        workspace_dir=tmp_path,
        max_append_seconds=1,
        conversation_snapshot_provider=snapshot,
    )
    assert captures == [True]
    assert len(seen) == 2 and seen[0] == seen[1]
    assert seen[0][0] == "answer"


def test_append_skill_order_matching_and_overlay(tmp_path):
    def skill(name):
        h = handler("skill:" + name + ":append")
        hooks = config(h)
        hooks.events[HookEventName.STOP][0].id = "skill:" + name + ":g"
        return LoadedSkillHookSource(
            sourceId="skill:" + name,
            skillName=name,
            skillRoot=str(tmp_path),
            sourcePath=str(tmp_path / name),
            hookConfig=hooks,
        )

    sources = [skill("z"), skill("a")]
    overlay = HookSessionOverlay.model_construct(
        loaded_skill_sources=sources,
        monitored_skill_sources=[],
        entries=[HookOverlayEntry(hook_id="disabled", enabled=False)],
        once_executed={},
    )
    resolver = HookResolver(
        tenant_config=config(
            handler("disabled"),
            handler("skip", **{"if": "False"}),
            handler("tenant"),
        ),
        agent_config=config(handler("agent")),
        session_overlay=overlay,
    )
    assert [
        x.source
        for x in resolver.resolve_stop_append_plan(context(tmp_path)).handlers
    ] == [
        "tenant",
        "agent",
        "skill:a",
        "skill:z",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, body, expected",
    [
        (
            200,
            '{"decision":"allow","reason":"ok","hookSpecificOutput":{"appendText":"hello"}}',
            "hello",
        ),
        (409, '{"decision":"allow","reason":"ok"}', None),
        (200, "x" * 262145, None),
        (
            200,
            '{"decision":"allow","reason":"ok","systemMessage":"secret"}',
            None,
        ),
    ],
)
async def test_append_http_protocol_and_limits(
    tmp_path,
    monkeypatch,
    status,
    body,
    expected,
):
    import httpx

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, content=body),
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.append.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    h = HttpHookHandlerConfig(
        id="http",
        url="https://example.test",
        outputAppend=True,
        failPolicy="block",
    )
    result = await execute_handler(
        h,
        context(tmp_path),
        workspace_dir=tmp_path,
    )
    assert result.append_text == expected
    assert result.decision not in {"block", "stop", "deny"}
    assert result.failed == (expected is None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected_failure",
    [
        (
            '{"decision":"allow","reason":"ok","hookSpecificOutput":{"appendText":" hi "}}',
            "",
        ),
        ("{decision:'allow',reason:'ok'}", "invalid_output"),
        ("x" * 262145, "output_limit"),
    ],
)
async def test_append_prompt_protocol_and_limits(
    tmp_path,
    monkeypatch,
    text,
    expected_failure,
):
    closed = []

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "sent", False):
                raise StopAsyncIteration
            self.sent = True
            return text

        async def aclose(self):
            closed.append(True)

    async def model(messages):
        assert "appendText" in messages[0]["content"]
        return Stream()

    async def factory(**kwargs):
        return model, None

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.create_model_and_formatter_async",
        factory,
    )
    h = PromptHookHandlerConfig(
        id="prompt",
        prompt="append",
        outputAppend=True,
    )
    result = await execute_handler(
        h,
        context(tmp_path),
        workspace_dir=tmp_path,
    )
    assert result.failure_type == expected_failure
    assert result.decision != "block"
    assert closed == [True]
    if not expected_failure:
        assert result.append_text == " hi "


@pytest.mark.asyncio
async def test_append_command_timeout_reaps_subprocess(tmp_path):
    (tmp_path / "hook.py").write_text(
        "import os,time\nopen('pid','w').write(str(os.getpid()))\ntime.sleep(10)",
    )
    result = await execute_handler(
        handler(timeout=0.1),
        context(tmp_path),
        workspace_dir=tmp_path,
    )
    assert result.failure_type == "timeout"
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_append_does_not_start_handlers_after_accepted_stop(
    tmp_path,
    monkeypatch,
):
    executions = []

    async def execute(h, ctx, **kwargs):
        executions.append(h.id)
        return HookHandlerResult(handler_id=h.id, order=0)

    async def requested():
        await asyncio.sleep(0.001)
        return True

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    runtime = HookRuntime(tenant_config=config(handler()))
    assert (
        await runtime.emit_stop_append(
            context(tmp_path),
            workspace_dir=tmp_path,
            max_append_seconds=1,
            stop_requested=requested,
        )
        == []
    )
    assert executions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_failure", [False, True])
async def test_append_logging_failure_preserves_successful_contributions(
    tmp_path,
    monkeypatch,
    snapshot_failure,
):
    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("logging unavailable")

    async def execute(h, ctx, **kwargs):
        return HookHandlerResult(handler_id=h.id, order=0, append_text="kept")

    async def snapshot():
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    loggers = [
        logging.getLogger("swe.agents.hook_runtime." + name)
        for name in ("append", "runtime")
    ]
    levels = [item.level for item in loggers]
    broken_handler = BrokenHandler()
    for item in loggers:
        item.setLevel(logging.INFO)
        item.addHandler(broken_handler)
    runtime = HookRuntime(
        tenant_config=config(
            handler(
                includeConversationSnapshot=snapshot_failure,
            ),
        ),
    )
    try:
        contributions = await runtime.emit_stop_append(
            context(tmp_path),
            workspace_dir=tmp_path,
            max_append_seconds=1,
            conversation_snapshot_provider=(
                snapshot if snapshot_failure else None
            ),
        )
    finally:
        for item, level in zip(loggers, levels):
            item.removeHandler(broken_handler)
            item.setLevel(level)
    assert [item.text for item in contributions] == ["kept"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fd", [1, 2])
async def test_append_flood_reclaims_pipes_before_handler_timeout(
    tmp_path,
    monkeypatch,
    fd,
):
    from swe.agents.hook_runtime import append

    processes = []
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        proc = await create(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(append.asyncio, "create_subprocess_exec", capture)
    (tmp_path / "hook.py").write_text(
        "import os\nwhile True: os.write(" + str(fd) + ", b'x' * 65536)\n",
    )
    task = asyncio.create_task(
        execute_handler(
            handler(timeout=3),
            context(tmp_path),
            workspace_dir=tmp_path,
        ),
    )
    try:
        done, _ = await asyncio.wait([task], timeout=1)
        assert (
            task in done
        ), "bounded output cleanup waited for handler timeout"
        assert task.result().failure_type == "output_limit"
        assert processes[0].returncode is not None
        assert processes[0]._transport.is_closing()
    finally:
        # 旧实现的管道暂停会阻止 wait，测试失败后仍需释放测试进程。
        for proc in processes:
            proc._transport.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_append_interruption_closes_process_transport(
    tmp_path,
    monkeypatch,
    mode,
):
    from swe.agents.hook_runtime import append

    processes = []
    started = asyncio.Event()
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        proc = await create(*args, **kwargs)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(append.asyncio, "create_subprocess_exec", capture)
    (tmp_path / "hook.py").write_text("import time\ntime.sleep(10)\n")
    task = asyncio.create_task(
        execute_handler(
            handler(timeout=0.1 if mode == "timeout" else 3),
            context(tmp_path),
            workspace_dir=tmp_path,
        ),
    )
    await started.wait()
    if mode == "cancel":
        task.cancel()
        task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    if mode == "timeout":
        assert results[0].failure_type == "timeout"
    else:
        assert isinstance(results[0], asyncio.CancelledError)
    try:
        assert processes[0].returncode is not None
        assert processes[0]._transport.is_closing()
    finally:
        processes[0]._transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_append_cleanup_finishes_despite_interruption(
    tmp_path,
    monkeypatch,
    mode,
):
    from swe.agents.hook_runtime import append

    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned = []
    cleanup = append._cleanup_process_resources

    async def held_cleanup(proc, tasks):
        entered.set()
        await release.wait()
        await cleanup(proc, tasks)
        cleaned.append(proc)

    monkeypatch.setattr(append, "_cleanup_process_resources", held_cleanup)
    (tmp_path / "hook.py").write_text("print('invalid json')\n")
    task = asyncio.create_task(
        execute_handler(
            handler(timeout=0.1 if mode == "timeout" else 3),
            context(tmp_path),
            workspace_dir=tmp_path,
        ),
    )
    await entered.wait()
    if mode == "cancel":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    else:
        expired = asyncio.Event()
        asyncio.get_running_loop().call_later(0.15, expired.set)
        await expired.wait()
    assert not task.done()
    release.set()
    results = await asyncio.gather(task, return_exceptions=True)
    assert cleaned and cleaned[0]._transport.is_closing()
    if mode == "cancel":
        assert isinstance(results[0], asyncio.CancelledError)
    else:
        assert results[0].failure_type == "timeout"


@pytest.mark.asyncio
async def test_append_deadline_records_safe_pending_diagnostic(
    tmp_path,
    monkeypatch,
):
    diagnostics = []

    async def execute(h, ctx, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.append.log_append_diagnostic",
        lambda message, *args: diagnostics.append(message % args),
    )
    runtime = HookRuntime(tenant_config=config(handler()))
    assert (
        await runtime.emit_stop_append(
            context(tmp_path),
            workspace_dir=tmp_path,
            max_append_seconds=0.01,
        )
        == []
    )
    assert any(
        "stage_deadline" in item and "tenant" in item and "append" in item
        for item in diagnostics
    )


@pytest.mark.asyncio
async def test_append_supports_python_without_asyncio_timeout(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    (tmp_path / "hook.py").write_text(
        'print(\'{"decision":"allow","reason":"ok","hookSpecificOutput":{"appendText":"kept"}}\')\n',
    )
    result = await execute_handler(
        handler(),
        context(tmp_path),
        workspace_dir=tmp_path,
    )
    assert result.append_text == "kept"


@pytest.mark.asyncio
async def test_append_keeps_success_finished_during_slow_stop_probe(
    tmp_path,
    monkeypatch,
):
    checks = 0

    async def requested():
        nonlocal checks
        checks += 1
        if checks == 3:
            await asyncio.sleep(0.04)
        return False

    async def execute(h, ctx, **kwargs):
        await asyncio.sleep(0.005)
        return HookHandlerResult(handler_id=h.id, order=0, append_text="kept")

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        execute,
    )
    runtime = HookRuntime(tenant_config=config(handler()))
    contributions = await runtime.emit_stop_append(
        context(tmp_path),
        workspace_dir=tmp_path,
        max_append_seconds=0.02,
        stop_requested=requested,
    )
    assert [item.text for item in contributions] == ["kept"]
