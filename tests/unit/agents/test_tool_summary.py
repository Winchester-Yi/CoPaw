# -*- coding: utf-8 -*-
from collections import OrderedDict

import pytest

from swe.agents.utils import tool_summary


def test_tool_display_name_returns_chinese_label() -> None:
    assert tool_summary.get_tool_display_name("grep_search") == "内容搜索"


def test_rule_call_summary_hides_shell_details() -> None:
    summary = tool_summary.generate_tool_call_summary(
        tool_name="execute_shell_command",
        arguments='{"command": "rm -rf /tmp/demo"}',
    )

    assert summary == "开始执行操作"


def test_rule_call_summary_hides_background_process_command() -> None:
    shell_summary = tool_summary.generate_tool_call_summary(
        tool_name="execute_shell_command",
        arguments='{"command": "rm -rf /tmp/demo"}',
    )
    summary = tool_summary.generate_tool_call_summary(
        tool_name="start_background_process",
        arguments='{"command": "rm -rf /tmp/demo"}',
    )

    assert summary == shell_summary
    assert "rm -rf" not in summary
    assert "/tmp/demo" not in summary


def test_rule_call_summary_keeps_browser_search_target() -> None:
    summary = tool_summary.generate_tool_call_summary(
        tool_name="browser_use",
        arguments=(
            '{"action": "open", "url": '
            '"https://github.com/search?q=copaw&type=repositories"}'
        ),
    )

    assert summary == "正在 GitHub 搜索 copaw"


def test_rule_call_summary_describes_file_read_action() -> None:
    summary = tool_summary.generate_tool_call_summary(
        tool_name="read_file",
        arguments='{"file_path": "/tmp/demo.txt"}',
    )

    assert summary == "正在读取 demo.txt"


def test_rule_call_summary_hides_unknown_tool_json_arguments() -> None:
    summary = tool_summary.generate_tool_call_summary(
        tool_name="query_business_opportunity",
        arguments='{"bbkOrgId": "V00", "brnOrgId": "V00001"}',
    )

    assert summary == "正在查询操作"
    assert "bbkOrgId" not in summary
    assert "V00001" not in summary


@pytest.mark.asyncio
async def test_async_call_summary_uses_model_for_all_tools(
    monkeypatch,
) -> None:
    async def fake_run_summary_model(prompt: str) -> str:
        assert "读取文件" in prompt
        return "查看资料内容"

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        fake_run_summary_model,
    )
    monkeypatch.setattr(
        tool_summary,
        "_model_summary_cache",
        {},
    )

    summary = await tool_summary.async_generate_tool_call_summary(
        tool_name="read_file",
        arguments='{"file_path": "/tmp/demo.txt"}',
    )

    assert summary == "查看资料内容"


@pytest.mark.asyncio
async def test_async_call_summary_guides_model_to_keep_object(
    monkeypatch,
) -> None:
    captured = {}

    async def fake_run_summary_model(prompt: str) -> str:
        captured["prompt"] = prompt
        return "正在 GitHub 搜索 copaw"

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        fake_run_summary_model,
    )
    monkeypatch.setattr(
        tool_summary,
        "_model_summary_cache",
        {},
    )

    summary = await tool_summary.async_generate_tool_call_summary(
        tool_name="browser_use",
        arguments=(
            '{"action": "open", "url": '
            '"https://github.com/search?q=copaw&type=repositories"}'
        ),
    )

    assert summary == "正在 GitHub 搜索 copaw"
    assert "建议保留的操作对象: GitHub 搜索 copaw" in captured["prompt"]
    assert "建议动作表达: 正在 GitHub 搜索 copaw" in captured["prompt"]


@pytest.mark.asyncio
async def test_async_call_summary_redacts_shell_command(
    monkeypatch,
) -> None:
    captured = {}

    async def fake_run_summary_model(prompt: str) -> str:
        captured["prompt"] = prompt
        return "处理系统中的一项操作"

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        fake_run_summary_model,
    )
    monkeypatch.setattr(
        tool_summary,
        "_model_summary_cache",
        {},
    )

    summary = await tool_summary.async_generate_tool_call_summary(
        tool_name="execute_shell_command",
        arguments='{"command": "cat /etc/passwd"}',
    )

    assert summary == "处理系统中的一项操作"
    assert "cat /etc/passwd" not in captured["prompt"]
    assert "执行了一项系统操作" in captured["prompt"]


@pytest.mark.asyncio
async def test_async_output_summary_falls_back_on_model_error(
    monkeypatch,
) -> None:
    async def boom(_prompt: str) -> str:
        raise RuntimeError("llm timeout")

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        boom,
    )
    monkeypatch.setattr(
        tool_summary,
        "_model_summary_cache",
        {},
    )

    summary = await tool_summary.async_generate_tool_output_summary(
        tool_name="glob_search",
        output='{"files": ["a.py", "b.py"]}',
        arguments='{"pattern": "**/*.py"}',
    )

    assert summary == "找到了 2 项相关内容"


@pytest.mark.asyncio
async def test_async_output_summary_preserves_empty_result_message(
    monkeypatch,
) -> None:
    async def fake_run_summary_model(_prompt: str) -> str:
        return "没有找到相关内容"

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        fake_run_summary_model,
    )
    monkeypatch.setattr(
        tool_summary,
        "_model_summary_cache",
        {},
    )

    summary = await tool_summary.async_generate_tool_output_summary(
        tool_name="memory_search",
        output="[]",
        arguments='{"query": "tenant provider"}',
    )

    assert summary == "没有找到相关内容"


@pytest.mark.asyncio
async def test_async_output_summary_hides_shell_output_details(
    monkeypatch,
) -> None:
    captured = {}

    async def fake_run_summary_model(prompt: str) -> str:
        captured["prompt"] = prompt
        return "这项操作已经完成"

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        fake_run_summary_model,
    )
    monkeypatch.setattr(
        tool_summary,
        "_model_summary_cache",
        {},
    )

    summary = await tool_summary.async_generate_tool_output_summary(
        tool_name="execute_shell_command",
        output="very technical stdout details",
        arguments='{"command": "pwd"}',
    )

    assert summary == "这项操作已经完成"
    assert "very technical stdout details" not in captured["prompt"]


@pytest.mark.asyncio
async def test_run_summary_model_binds_current_trace_context(
    monkeypatch,
) -> None:
    observed = {}

    async def fake_model(_messages):
        return "查看资料内容"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        observed["trace_context"] = trace_context
        return fake_model, object()

    monkeypatch.setattr(
        tool_summary,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        tool_summary,
        "capture_current_trace_context",
        lambda: {
            "trace_id": "trace-summary",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "source_id": "source-1",
        },
    )

    summary = await tool_summary._run_summary_model("读取文件")

    assert summary == "查看资料内容"
    assert observed["trace_context"]["trace_id"] == "trace-summary"


@pytest.mark.asyncio
async def test_run_summary_model_reuses_cached_model_within_same_trace(
    monkeypatch,
) -> None:
    created = []

    async def fake_model(_messages):
        return "查看资料内容"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    monkeypatch.setattr(
        tool_summary,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        tool_summary,
        "capture_current_trace_context",
        lambda: {"trace_id": "trace-summary"},
    )
    monkeypatch.setattr(
        tool_summary,
        "_summary_models_by_trace",
        OrderedDict(),
    )

    summary_a = await tool_summary._run_summary_model("读取文件")
    summary_b = await tool_summary._run_summary_model("继续读取文件")

    assert summary_a == "查看资料内容"
    assert summary_b == "查看资料内容"
    assert len(created) == 1


def test_reset_summary_caches_disposes_cached_models() -> None:
    closed = []

    class FakeModel:
        def close(self) -> None:
            closed.append("closed")

    tool_summary._summary_models_by_trace = OrderedDict(
        [(("trace-a", ("tenant-a", "agent-a")), FakeModel())],
    )
    tool_summary._model_summary_cache = {"key": "value"}

    tool_summary.reset_summary_caches()

    assert closed == ["closed"]
    assert tool_summary._summary_models_by_trace == OrderedDict()
    assert tool_summary._model_summary_cache == {}


@pytest.mark.asyncio
async def test_run_summary_model_reuses_cached_model_without_trace_within_scope(
    monkeypatch,
) -> None:
    created = []

    async def fake_model(_messages):
        return "查看资料内容"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    monkeypatch.setattr(
        tool_summary,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        tool_summary,
        "capture_current_trace_context",
        lambda: None,
    )
    monkeypatch.setattr(
        tool_summary,
        "_summary_models_by_trace",
        OrderedDict(),
    )

    await tool_summary._run_summary_model("读取文件")
    await tool_summary._run_summary_model("继续读取文件")

    assert len(created) == 1


@pytest.mark.asyncio
async def test_run_summary_model_cache_key_includes_runtime_scope(
    monkeypatch,
) -> None:
    created = []
    runtime_scopes = [("tenant-a", "agent-a"), ("tenant-a", "agent-b")]

    async def fake_model(_messages):
        return "查看资料内容"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    monkeypatch.setattr(
        tool_summary,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        tool_summary,
        "capture_current_trace_context",
        lambda: {"trace_id": "trace-summary"},
    )
    monkeypatch.setattr(
        tool_summary,
        "_runtime_scope_key",
        lambda: runtime_scopes.pop(0),
    )
    monkeypatch.setattr(
        tool_summary,
        "_summary_models_by_trace",
        OrderedDict(),
    )

    await tool_summary._run_summary_model("读取文件")
    await tool_summary._run_summary_model("继续读取文件")

    assert len(created) == 2


@pytest.mark.asyncio
async def test_run_summary_model_evicts_oldest_cached_trace(
    monkeypatch,
) -> None:
    created = []
    trace_ids = iter(
        [
            {"trace_id": "trace-1"},
            {"trace_id": "trace-2"},
            {"trace_id": "trace-3"},
            {"trace_id": "trace-1"},
        ],
    )

    async def fake_model(_messages):
        return "查看资料内容"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    monkeypatch.setattr(
        tool_summary,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        tool_summary,
        "capture_current_trace_context",
        lambda: next(trace_ids),
    )
    monkeypatch.setattr(
        tool_summary,
        "_runtime_scope_key",
        lambda: ("tenant-a", "agent-a"),
    )
    monkeypatch.setattr(
        tool_summary,
        "_summary_models_by_trace",
        OrderedDict(),
    )
    monkeypatch.setattr(tool_summary, "_SUMMARY_MODEL_CACHE_LIMIT", 2)

    await tool_summary._run_summary_model("读取文件")
    await tool_summary._run_summary_model("读取文件")
    await tool_summary._run_summary_model("读取文件")
    await tool_summary._run_summary_model("读取文件")

    assert [item["trace_id"] for item in created] == [
        "trace-1",
        "trace-2",
        "trace-3",
        "trace-1",
    ]


@pytest.mark.asyncio
async def test_run_summary_model_eviction_disposes_oldest_cached_model(
    monkeypatch,
) -> None:
    closed = []
    trace_ids = iter(
        [
            {"trace_id": "trace-1"},
            {"trace_id": "trace-2"},
            {"trace_id": "trace-3"},
        ],
    )

    class FakeModel:
        def __init__(self, label: str) -> None:
            self.label = label

        async def __call__(self, _messages):
            return "查看资料内容"

        async def aclose(self) -> None:
            closed.append(self.label)

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        return FakeModel(trace_context["trace_id"]), object()

    monkeypatch.setattr(
        tool_summary,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        tool_summary,
        "capture_current_trace_context",
        lambda: next(trace_ids),
    )
    monkeypatch.setattr(
        tool_summary,
        "_runtime_scope_key",
        lambda: ("tenant-a", "agent-a"),
    )
    monkeypatch.setattr(
        tool_summary,
        "_summary_models_by_trace",
        OrderedDict(),
    )
    monkeypatch.setattr(tool_summary, "_SUMMARY_MODEL_CACHE_LIMIT", 2)

    await tool_summary._run_summary_model("读取文件")
    await tool_summary._run_summary_model("读取文件")
    await tool_summary._run_summary_model("读取文件")

    assert closed == ["trace-1"]


@pytest.fixture(autouse=True)
def prepared_model_boundary(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        tool_summary,
        "prepare_model_config_async",
        AsyncMock(return_value=object()),
    )
