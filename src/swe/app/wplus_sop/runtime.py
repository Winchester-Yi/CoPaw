# -*- coding: utf-8 -*-
"""Bridge W+ commands into the owning Chat's existing Agent runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..answer_turn.models import TurnIdentity
from ..channels.base import ContentType, TextContent
from .runtime_context import WPlusRuntimeContext, bind_wplus_runtime

WPLUS_SOP_SKILL_NAME = "wplus-sop-miner"
logger = logging.getLogger(__name__)

_STAGE_PROPOSAL_EXAMPLE = {
    "stages": [
        {
            "stage_id": "stage-1",
            "name": "确认需求范围",
            "description": "确认流程入口、对象范围与目标。",
            "status": "pending",
        },
        {
            "stage_id": "stage-2",
            "name": "验证交付结果",
            "description": "预跑已确认流程并核对输出。",
            "status": "pending",
        },
    ],
}

_QUESTION_BATCH_EXAMPLE = {
    "batch_id": "question-batch-stage-1-v1",
    "stage_id": "stage-1",
    "questions": [
        {
            "question_id": "confirm-scope",
            "prompt": "请确认当前环节的适用范围。",
            "type": "single_select",
            "required": True,
            "options": [
                {
                    "option_id": "confirmed",
                    "label": "范围正确",
                    "requires_custom_input": False,
                },
                {
                    "option_id": "custom",
                    "label": "需要调整",
                    "requires_custom_input": True,
                },
            ],
            "help_text": "选择“需要调整”时请补充具体范围。",
        },
    ],
}

_SOP_RESULT_EXAMPLE = {
    "result": {
        "sop_spec": {
            "name": "客户经营 SOP",
            "version": 1,
            "stages": [],
        },
        "readable_sop": "# 客户经营 SOP\n\n按已确认环节执行。",
        "html": "<article><h1>客户经营 SOP</h1></article>",
        "example_result_html": "<article><h1>脱敏示例结果</h1></article>",
        "artifacts": [
            {
                "artifact_id": artifact_id,
                "name": name,
                "static_file_name": name,
                "static_url": (
                    "http://files.example/static/tenant/agent/" + name
                ),
                "sha256": "0" * 64,
                "copied_by": "copy_file_to_static",
            }
            for artifact_id, name in (
                ("sop_spec", "sop_spec.json"),
                ("sop_render_md", "sop_render.md"),
                ("sop_render_html", "sop_render.html"),
                ("example_result_html", "example_result.html"),
            )
        ],
        "validation": {
            "schema_validator": "scripts/validate_sop.py",
            "schema_exit_code": 0,
            "renderers": ["scripts/render_md.py", "scripts/render_sop.py"],
        },
    },
}

_MEMORY_CANDIDATES_EXAMPLE = {
    "candidates": [
        {
            "candidate_id": "memory-common-page-fact-1",
            "summary": "保存已验证的平台事实",
            "type": "common_wplus_knowledge",
            "value": {
                "page": "客户筛选",
                "fact": "支持按产品到期日筛选",
            },
            "evidence": "用户在本次对话中确认该页面能力已验证。",
        },
    ],
}


class WPlusChatRunBusyError(RuntimeError):
    """Raised when the owning Chat already has an unrelated active run."""


@dataclass(frozen=True)
class WPlusTurnStart:
    """Trusted identifiers for a newly claimed owning-Chat turn."""

    chat_id: str
    logical_chat_session_id: str
    message_id: str
    run_id: str
    attempt_id: str


@dataclass(frozen=True)
class WPlusSafeStreamTraceEntry:
    """One display-safe assistant text or tool activity entry."""

    entry_id: str
    kind: str
    status: str
    text: str = ""
    tool_name: str = ""
    server_label: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "status": self.status,
        }
        if self.kind == "assistant_text":
            payload["text"] = self.text
        else:
            payload["tool_name"] = self.tool_name
            if self.server_label:
                payload["server_label"] = self.server_label
        return payload


@dataclass(frozen=True)
class WPlusSafeStreamTraceSnapshot:
    """Bounded, non-persisted safe frame summaries for one W+ Agent run."""

    sequence: int
    summary_text: str
    truncated: bool
    entries: tuple[WPlusSafeStreamTraceEntry, ...]


@dataclass
class _WPlusSafeTextPart:
    text: str
    delta: bool


@dataclass
class _WPlusSafeAssistantMessage:
    parts: list[_WPlusSafeTextPart] = field(default_factory=list)
    status: str = "running"

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts)


@dataclass
class _WPlusSafeStreamTraceRun:
    sequence: int = 0
    messages: OrderedDict[str, _WPlusSafeAssistantMessage] = field(
        default_factory=OrderedDict,
    )
    tools: OrderedDict[str, WPlusSafeStreamTraceEntry] = field(
        default_factory=OrderedDict,
    )
    timeline: OrderedDict[str, tuple[str, str]] = field(
        default_factory=OrderedDict,
    )
    truncated: bool = False
    finished: bool = False


class WPlusSafeStreamTraceRegistry:
    """Collect bounded plain text from ordinary assistant messages only."""

    def __init__(
        self,
        *,
        max_chars: int = 4_000,
        max_lines: int = 80,
        max_active_runs: int = 32,
    ):
        if max_chars < 1 or max_lines < 1 or max_active_runs < 1:
            raise ValueError("debug stream limits must be positive")
        self._max_chars = max_chars
        self._max_lines = max_lines
        self._max_active_runs = max_active_runs
        self._runs: OrderedDict[
            tuple[str, str],
            _WPlusSafeStreamTraceRun,
        ] = OrderedDict()

    def start_run(self, session_id: str, run_id: str) -> None:
        """Start one trace, discard this Session's old run, and cap entries."""
        for key in tuple(self._runs):
            if key[0] == session_id:
                del self._runs[key]
        self._runs[(session_id, run_id)] = _WPlusSafeStreamTraceRun()
        while len(self._runs) > self._max_active_runs:
            self._runs.popitem(last=False)

    def finish_run(self, session_id: str, run_id: str) -> None:
        """Freeze a completed trace until the Session starts its next run."""
        run = self._runs.get((session_id, run_id))
        if run is None:
            return
        if not self._entries(run):
            self._runs.pop((session_id, run_id), None)
            return
        run.finished = True

    def ingest(self, session_id: str, run_id: str, sse_chunk: str) -> None:
        """Apply allowlisted text frames with Chat Builder merge semantics."""
        run = self._runs.get((session_id, run_id))
        if run is None or run.finished or not isinstance(sse_chunk, str):
            return
        for line in sse_chunk.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                frame = json.loads(line.removeprefix("data:").strip())
            except (json.JSONDecodeError, TypeError):
                continue
            self._ingest_frame(run, frame)

    def snapshot(
        self,
        session_id: str,
        run_id: str,
    ) -> WPlusSafeStreamTraceSnapshot | None:
        run = self._runs.get((session_id, run_id))
        if run is None:
            return None
        return WPlusSafeStreamTraceSnapshot(
            sequence=run.sequence,
            summary_text=self._render(run),
            truncated=run.truncated,
            entries=self._entries(run),
        )

    def _ingest_frame(
        self,
        run: _WPlusSafeStreamTraceRun,
        frame: Any,
    ) -> None:
        if not isinstance(frame, dict):
            return
        if frame.get("object") == "message":
            if frame.get("type") == "message":
                self._ingest_message(run, frame)
            else:
                self._ingest_tool(run, frame)
        elif frame.get("object") == "content":
            self._ingest_content(run, frame)

    def _ingest_message(
        self,
        run: _WPlusSafeStreamTraceRun,
        frame: dict[str, Any],
    ) -> None:
        if frame.get("role") != "assistant" or frame.get("type") != "message":
            return
        message_id = frame.get("id")
        if not isinstance(message_id, str) or not message_id:
            return
        message = run.messages.setdefault(
            message_id,
            _WPlusSafeAssistantMessage(),
        )
        run.timeline.setdefault(
            f"assistant_text:{message_id}",
            ("assistant_text", message_id),
        )
        message.status = self._safe_status(frame.get("status"))
        content = frame.get("content")
        if isinstance(content, list) and content:
            parts = [
                _WPlusSafeTextPart(
                    text=item["text"],
                    delta=item.get("delta") is True,
                )
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            message.parts = parts
            if parts:
                run.sequence += 1
        self._enforce_limits(run)

    def _ingest_tool(
        self,
        run: _WPlusSafeStreamTraceRun,
        frame: dict[str, Any],
    ) -> None:
        message_type = frame.get("type")
        if message_type not in {
            "function_call",
            "function_call_output",
            "plugin_call",
            "plugin_call_output",
            "component_call",
            "component_call_output",
            "mcp_call",
            "mcp_call_output",
        }:
            return
        data = self._first_data_payload(frame.get("content"))
        if data is None:
            return
        fallback_id = frame.get("id")
        call_id = next(
            (
                value
                for key in ("call_id", "tool_call_id", "id")
                if isinstance((value := data.get(key)), str) and value
            ),
            fallback_id if isinstance(fallback_id, str) else "",
        )
        if not call_id:
            return
        existing = run.tools.get(call_id)
        tool_name = self._safe_tool_label(self._tool_name(data))
        server_label = self._safe_tool_label(
            data.get("server_label") or data.get("mcp_server"),
        )
        is_output = message_type.endswith("_output")
        raw_status = data.get("tool_status") or frame.get("status")
        status = self._safe_status(raw_status, completed=is_output)
        if data.get("tool_error") or data.get("isError") is True:
            status = "failed"
        elif not is_output:
            status = "running"
        run.tools[call_id] = WPlusSafeStreamTraceEntry(
            entry_id=f"tool:{call_id}",
            kind="tool",
            status=status,
            tool_name=(
                tool_name or existing.tool_name if existing else tool_name
            ),
            server_label=(
                server_label or existing.server_label
                if existing
                else server_label
            ),
        )
        run.timeline.setdefault(f"tool:{call_id}", ("tool", call_id))
        run.sequence += 1
        self._enforce_limits(run)

    @staticmethod
    def _first_data_payload(content: Any) -> dict[str, Any] | None:
        if not isinstance(content, list):
            return None
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "data":
                continue
            data = item.get("data")
            if isinstance(data, dict):
                return data
        return None

    @staticmethod
    def _tool_name(data: dict[str, Any]) -> Any:
        direct = (
            data.get("name")
            or data.get("tool_name")
            or data.get(
                "mcp_tool_name",
            )
        )
        if direct:
            return direct
        for key in ("function", "tool", "tool_call", "mcp_tool"):
            nested = data.get(key)
            if isinstance(nested, dict) and nested.get("name"):
                return nested["name"]
        return ""

    @staticmethod
    def _safe_tool_label(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        normalized = " ".join(value.split()).strip()
        if not normalized or any(char in normalized for char in '{}[]"'):
            return ""
        return normalized[:96]

    @staticmethod
    def _safe_status(value: Any, *, completed: bool = False) -> str:
        if value in {"failed", "rejected", "canceled"}:
            return "failed"
        if completed or value == "completed":
            return "completed"
        return "running"

    def _ingest_content(
        self,
        run: _WPlusSafeStreamTraceRun,
        frame: dict[str, Any],
    ) -> None:
        if frame.get("type") != "text" or not isinstance(
            frame.get("text"),
            str,
        ):
            return
        message_id = frame.get("msg_id")
        if not isinstance(message_id, str):
            return
        message = run.messages.get(message_id)
        if message is None:
            return
        text = frame["text"]
        is_delta = frame.get("delta") is True
        if is_delta:
            if message.parts and message.parts[-1].delta:
                message.parts[-1].text += text
            else:
                message.parts.append(
                    _WPlusSafeTextPart(text=text, delta=True),
                )
        elif message.parts:
            message.parts[-1] = _WPlusSafeTextPart(
                text=text,
                delta=False,
            )
        else:
            message.parts.append(
                _WPlusSafeTextPart(text=text, delta=False),
            )
        run.sequence += 1
        self._enforce_limits(run)

    @staticmethod
    def _render(run: _WPlusSafeStreamTraceRun) -> str:
        return "\n".join(
            text for message in run.messages.values() if (text := message.text)
        )

    @staticmethod
    def _entries(
        run: _WPlusSafeStreamTraceRun,
    ) -> tuple[WPlusSafeStreamTraceEntry, ...]:
        entries: list[WPlusSafeStreamTraceEntry] = []
        for kind, item_id in run.timeline.values():
            if kind == "tool":
                if tool := run.tools.get(item_id):
                    entries.append(tool)
                continue
            message = run.messages.get(item_id)
            if message and message.text:
                entries.append(
                    WPlusSafeStreamTraceEntry(
                        entry_id=f"assistant_text:{item_id}",
                        kind="assistant_text",
                        status=message.status,
                        text=message.text,
                    ),
                )
        return tuple(entries)

    def _enforce_limits(self, run: _WPlusSafeStreamTraceRun) -> None:
        while len(run.timeline) > self._max_lines:
            _, (kind, item_id) = run.timeline.popitem(last=False)
            if kind == "tool":
                run.tools.pop(item_id, None)
            else:
                run.messages.pop(item_id, None)
            run.truncated = True

        while len(run.messages) > self._max_lines:
            message_id, removed = run.messages.popitem(last=False)
            run.timeline.pop(f"assistant_text:{message_id}", None)
            if removed.text:
                run.truncated = True

        while len(self._render(run).splitlines()) > self._max_lines:
            if len(run.messages) > 1:
                message_id, _ = run.messages.popitem(last=False)
                run.timeline.pop(f"assistant_text:{message_id}", None)
            else:
                message = next(iter(run.messages.values()))
                lines = message.text.splitlines()
                message.parts = [
                    _WPlusSafeTextPart(
                        text="\n".join(lines[-self._max_lines :]),
                        delta=(
                            message.parts[-1].delta if message.parts else False
                        ),
                    ),
                ]
            run.truncated = True

        while len(self._render(run)) > self._max_chars:
            if len(run.messages) > 1:
                message_id, _ = run.messages.popitem(last=False)
                run.timeline.pop(f"assistant_text:{message_id}", None)
            else:
                message = next(iter(run.messages.values()))
                message.parts = [
                    _WPlusSafeTextPart(
                        text=message.text[-self._max_chars :],
                        delta=(
                            message.parts[-1].delta if message.parts else False
                        ),
                    ),
                ]
            run.truncated = True


def get_wplus_safe_stream_trace_registry(
    workspace: Any,
) -> WPlusSafeStreamTraceRegistry:
    """Return the process-local safe trace registry scoped to one workspace."""
    attribute = "_wplus_sop_safe_stream_trace_registry"
    registry = getattr(workspace, attribute, None)
    if registry is None:
        registry = WPlusSafeStreamTraceRegistry()
        setattr(workspace, attribute, registry)
    return registry


def _build_trial_command_contract(
    *,
    run_id: str,
    attempt_id: str,
    requires_plan: bool,
) -> str:
    plan_step = (
        "1. 先提交 trial_plan，冻结本轮步骤与脱敏输出契约；\n"
        if requires_plan
        else "1. 这是执行态重试，沿用已持久化的预跑计划，不要重复提交 trial_plan；\n"
    )
    completion_example = {
        "run_id": run_id,
        "summary": (
            "执行范围：按已确认输入检查目标分组；实际关键发现：示例分组命中"
            "两项待处理条件；可执行建议：优先执行复核动作；证据与限制："
            "依据本轮 OpenCLI 脱敏结果，仍有一个口径待确认。"
        ),
        "result_lists": [
            {
                "list_id": "actionable-findings",
                "label": "业务发现与建议",
                "columns": [
                    {
                        "field": "segment",
                        "label": "对象分组",
                        "type": "string",
                    },
                    {
                        "field": "finding",
                        "label": "关键发现",
                        "type": "string",
                    },
                    {
                        "field": "recommended_action",
                        "label": "建议动作",
                        "type": "string",
                    },
                    {
                        "field": "evidence",
                        "label": "判断依据",
                        "type": "string",
                    },
                    {
                        "field": "affected_count",
                        "label": "影响数量",
                        "type": "number",
                    },
                ],
                "rows": [
                    {
                        "segment": "示例分组",
                        "finding": "命中两项待处理条件",
                        "recommended_action": "优先执行复核动作",
                        "evidence": "本轮规则命中数为 2",
                        "affected_count": 2,
                    },
                ],
                "truncated": False,
                "total_count": 1,
            },
        ],
        "warnings": [],
        "confirmed_facts": ["本轮已完成目标分组检查"],
        "unknowns": ["一个业务口径仍待确认"],
        "schema_validated": True,
    }
    return (
        "\n本命令必须在同一个后台 Agent 回合内完成预跑闭环，不得在提交 "
        "trial_plan 后停止或等待另一个后台任务。按以下顺序执行：\n"
        + plan_step
        + "2. 提交 trial_execution_started；\n"
        "3. 业务执行只能直接执行 references 中已确认的 opencli 命令，"
        "不得调用其他业务工具，也不得让用户自行执行；\n"
        "4. 可提交 trial_execution_progress；\n"
        "5. OpenCLI 成功后提交且只提交一个 trial_execution_completed；"
        "失败、拒绝、超时或权限不足时提交且只提交一个 "
        "trial_execution_failed。终态事件成功持久化前不得结束本回合。\n"
        "所有预跑事件的 run_id 必须严格等于命令中的 run_id="
        + json.dumps(run_id, ensure_ascii=False)
        + "；trial_execution_started 的 attempt_id 必须严格等于命令中的 "
        "attempt_id="
        + json.dumps(attempt_id, ensure_ascii=False)
        + "。trial_execution_completed 还必须用 confirmed_facts 提交截至本轮"
        "的累计已确认事实，并用 unknowns 提交当前明确未知项；这些内容只写"
        "脱敏业务摘要。完成结果必须达到可供用户判断和行动的详细度：summary "
        "必须依次说明执行范围、实际关键发现、可执行建议、证据与限制，不能只复述"
        "环节名称、用户输入或预跑目标。只要 OpenCLI 返回了可枚举的业务对象或"
        "分组，就必须写入 result_lists，并按每个对象或分组一行提供适用的"
        "对象分组、关键发现、建议动作、判断依据、影响数量；结果天然只有汇总值时"
        "可使用一个汇总行。每个结果列表必须提供可读 columns，并准确填写 "
        "total_count 与 truncated；没有命中项时也要用空 rows、total_count=0 和"
        "具体 summary 说明查询范围，不得省略结果列表。部分数据、降级执行、"
        "schema 偏差或其他可信度限制写入 warnings，仍未确认的业务信息写入 "
        "unknowns。不得为了满足详细度编造结果；无法取得可靠证据时提交 "
        "trial_execution_failed，或把实际限制明确写入 warnings 与 unknowns。"
        "结果只保留脱敏摘要、计数、schema 校验、警告和失败位置；不得把原始客户"
        "响应、账户值或自由文本备注写入事件。\n"
        "trial_execution_completed payload 示例（仅示范结构，必须替换为本轮"
        "真实脱敏结果）：\n"
        + json.dumps(
            completion_example,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _build_finalizing_command_contract(
    *,
    final_result_persisted: bool,
    memory_user_scope_available: bool,
) -> str:
    if final_result_persisted:
        sequence = (
            "本次重试检测到 sop_result 已成功持久化；只提交且只提交一个 "
            "memory_candidates，不得重复提交 sop_result。"
        )
    else:
        sequence = (
            "先提交且只提交一个 sop_result；工具返回 ok=true 后，再提交且只提交"
            "一个 memory_candidates。"
        )
    sop_example = (
        "\nsop_result payload 示例：\n"
        + json.dumps(
            _SOP_RESULT_EXAMPLE,
            ensure_ascii=False,
            sort_keys=True,
        )
        if not final_result_persisted
        else ""
    )
    return (
        "\n本命令必须在同一个后台 Agent 回合内完成最终产出闭环。"
        + sequence
        + "不得提交 kind='retry_started'，该事件不属于 W+ SOP 协议；也不得用 "
        "lifecycle_progress 代替上述业务边界事件。若 emit_wplus_sop_event 工具"
        "返回 ok=false，必须根据返回的 allowed agent events 修正参数后重试。"
        "最终化阶段由你实际生成文件，不得让平台下载路由临时拼接结果。先写出 "
        "sop_spec.json，使用 execute_shell_command 调用 "
        "scripts/validate_sop.py；成功后调用 scripts/render_md.py 和 "
        "scripts/render_sop.py。必须从 assets/example-result-templates/ 中选择匹配"
        "模板生成 example_result.html；模板缺失时提交 recoverable_failure，不能"
        "伪造模板或省略文件。四个文件生成后逐个调用 copy_file_to_static，并只"
        "使用该工具真实返回的 static URL 和文件名填写 artifacts。不得使用 shell "
        "复制到 static，也不得手写、猜测 static URL。计算每个 static 文件的 "
        "SHA-256 后再提交 sop_result。"
        "终态事件成功持久化前不得结束本回合。sop_result 必须包含 sop_spec、"
        "readable_sop、html、example_result_html、四项 artifacts 和真实 validation "
        "证据；memory_candidates 即使为空"
        "也必须提交 candidates=[]，由服务端据此进入 OutputReview；用户确认结果后"
        "才进入记忆处理或完成态。候选只能提交 pending 状态，不得伪造 approved、"
        "failed、写入回执或目标位置。"
        "每个候选必须提供 type、非空对象 value 和准确对话 evidence；"
        "type 只能是 common_wplus_knowledge、user_wplus_usage 或 sop_case。"
        + (
            "调用方提供了匿名 user_scope，可以在有用户明确证据时提交 "
            "user_wplus_usage。"
            if memory_user_scope_available
            else "调用方没有提供匿名 user_scope，必须跳过 user_wplus_usage 候选。"
        )
        + sop_example
        + "\nmemory_candidates payload 示例：\n"
        + json.dumps(
            _MEMORY_CANDIDATES_EXAMPLE,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _build_cumulative_refresh_command_contract(
    payload: dict[str, Any],
) -> str:
    return (
        "\n本命令的同一个后台 Agent 回合只负责累计刷新。"
        "先严格按 payload.confirmed_snapshots 的顺序生成累计 JSON、Markdown "
        "和 HTML，只能包含这些已确认快照；按 wplus-sop-miner 的累计产物契约"
        "校验三份文件，并逐个调用 copy_file_to_static。只能使用工具真实返回的"
        "文件名、static URL 和 SHA-256 填写 preview.artifacts。随后调用 "
        "emit_wplus_sop_event，提交且只提交一个 kind='cumulative_refreshed'；"
        "payload.preview 必须包含与 confirmed_snapshots 完全一致的 stage_order "
        "和 snapshots。若工具返回 ok=false，必须按返回的 allowed agent events "
        "修正后重试。累计事件成功持久化后结束当前回合；不得在本回合生成下一"
        "环节问题、执行预跑或生成最终结果。平台会在本回合完成后启动下一步的"
        "独立 Agent 回合。"
    )


def _build_memory_write_command_contract(
    payload: dict[str, Any],
    *,
    run_id: str,
) -> str:
    candidates = payload.get("candidates")
    bound_candidates = candidates if isinstance(candidates, list) else []
    example_results: list[dict[str, Any]] = []
    for index, candidate in enumerate(bound_candidates):
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        if index == 0:
            example_results.append(
                {
                    "candidate_id": candidate_id,
                    "status": "succeeded",
                    "target_scope": candidate.get("target_scope"),
                    "target_file": candidate.get("target_file"),
                    "result": "appended",
                    "script": "scripts/memory_store.py",
                },
            )
        else:
            example_results.append(
                {
                    "candidate_id": candidate_id,
                    "status": "failed",
                    "error_code": "store_failed",
                    "summary": "脱敏后的失败原因",
                    "script": "scripts/memory_store.py",
                },
            )
    event_example = {
        "kind": "memory_write_batch_result",
        "payload": {"results": example_results},
        "event_key": f"memory-write-batch-result-{run_id}",
    }
    return (
        "\n这是一次批量记忆写入回合。只能处理服务端绑定的 candidates："
        + json.dumps(bound_candidates, ensure_ascii=False, sort_keys=True)
        + "。必须在同一个 Agent 回合内通过 execute_shell_command 逐项调用当前 "
        "wplus-sop-miner 的 "
        "scripts/memory_store.py，每项使用绑定的 target_file 和 --approved；不得"
        "修改目标、补充未授权候选或使用其他写入方式。即使某项失败也继续处理剩余"
        "候选。全部处理完后，调用 emit_wplus_sop_event 且只提交一次 "
        "memory_write_batch_result，payload.results 必须按候选逐项给出结果。成功项"
        "使用 status=succeeded、绑定的 target_scope/target_file、脚本返回的 result "
        "和 script=scripts/memory_store.py；失败项使用 status=failed、error_code、"
        "脱敏 summary 和相同 script。批量事件成功持久化前不得结束本回合。"
        "event_key 必须固定为 memory-write-batch-result- 加本命令 run_id，重试时"
        "复用同一个值，不得逐候选生成 event_key。\n"
        "memory_write_batch_result 完整调用示例：\n"
        + json.dumps(event_example, ensure_ascii=False, sort_keys=True)
    )


def _resolve_target_state(
    command: str,
    payload: dict[str, Any],
    target_state: str | None,
) -> str | None:
    if target_state is not None:
        return target_state
    if command == "propose_stage_queue":
        return "GeneratingStageProposal"
    if command == "retry_current_turn":
        retry_target = payload.get("target_state")
        return retry_target if isinstance(retry_target, str) else None
    return None


def _expected_event_sequence(
    target_state: str | None,
    payload: dict[str, Any],
) -> list[str] | None:
    if target_state in {"GeneratingTrial", "ExecutingTrial"}:
        sequence = []
        if target_state == "GeneratingTrial":
            sequence.append("trial_plan")
        return [
            *sequence,
            "trial_execution_started",
            "trial_execution_progress?",
            "trial_execution_completed|trial_execution_failed",
        ]
    if target_state == "GeneratingQuestions":
        return [
            "[question_batch|trial_plan]",
            "trial_execution_started?",
            "trial_execution_progress?",
            "trial_execution_completed|trial_execution_failed",
        ]
    if target_state == "RefreshingCumulative":
        return ["cumulative_refreshed"]
    if target_state == "FinalizingOutputs":
        if payload.get("final_result_persisted") is True:
            return ["memory_candidates"]
        return ["sop_result", "memory_candidates"]
    if target_state == "WritingMemory":
        return ["memory_write_batch_result"]
    return None


def build_wplus_command_text(
    *,
    command: str,
    sop_session_id: str,
    run_id: str,
    attempt_id: str,
    payload: dict[str, Any],
    target_state: str | None = None,
) -> str:
    """Build a deterministic instruction without putting ownership in user data."""
    effective_target_state = _resolve_target_state(
        command,
        payload,
        target_state,
    )
    expected_event_kind = (
        {"GeneratingStageProposal": "stage_proposal"}.get(
            effective_target_state,
        )
        if effective_target_state is not None
        else None
    )
    is_trial_turn = effective_target_state in {
        "GeneratingTrial",
        "ExecutingTrial",
    }
    is_question_or_trial_turn = effective_target_state == "GeneratingQuestions"
    is_cumulative_refresh_turn = (
        effective_target_state == "RefreshingCumulative"
    )
    is_finalizing_turn = effective_target_state == "FinalizingOutputs"
    is_memory_write_turn = effective_target_state == "WritingMemory"
    expected_sequence = _expected_event_sequence(
        effective_target_state,
        payload,
    )
    body = {
        "protocol": "wplus-sop-command-v1",
        "command": command,
        "sop_session_id": sop_session_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "payload": payload,
    }
    if effective_target_state is not None:
        body["target_state"] = effective_target_state
    if expected_event_kind is not None:
        body["expected_event_kind"] = expected_event_kind
    if expected_sequence is not None:
        body["expected_event_sequence"] = expected_sequence
    command_contract = ""
    if expected_event_kind == "stage_proposal":
        memory_user_scope = payload.get("memory_user_scope")
        personal_memory = (
            "memory/users/"
            + memory_user_scope
            + "/wplus-usage-preferences.jsonl"
            if isinstance(memory_user_scope, str) and memory_user_scope
            else None
        )
        command_contract = (
            "\n在提出第一个澄清问题或拟定队列前，依次尝试读取 "
            "memory/common-wplus-knowledge.jsonl、"
            + (personal_memory + "、" if personal_memory is not None else "")
            + "memory/cases/sop-cases.jsonl。文件不存在表示当前没有对应记忆，"
            "不得仅因缺失而创建文件；没有匿名 user_scope 时不得读取个性化记忆。"
            "本回合只允许成功持久化一个业务边界事件。若 "
            "emit_wplus_sop_event 工具返回 ok=false，可根据返回的 allowed "
            "agent events 与 current_stage_id 修正参数后重试；失败调用不计入"
            "已持久化事件。不得成功持久化其他 W+ SOP 事件。调用参数必须满足 "
            "kind='stage_proposal'、"
            "event_key='stage-proposal-v1'，payload 必须是按下方 schema "
            "新生成的候选环节对象，不得把命令输入中的 payload 原样提交。"
            "不得只输出 Markdown；Markdown "
            "只能作为工具提交成功后的可读摘要。payload 顶层只能包含 stages "
            "数组，每个环节必须且只能包含 stage_id、name、description、"
            "status；status 使用 pending。\n"
            "stage_proposal payload 示例：\n"
            + json.dumps(
                _STAGE_PROPOSAL_EXAMPLE,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif is_question_or_trial_turn:
        current_stage_id = payload.get("current_stage_id")
        question_batch_example = {
            **_QUESTION_BATCH_EXAMPLE,
            **(
                {"stage_id": current_stage_id}
                if isinstance(current_stage_id, str) and current_stage_id
                else {}
            ),
        }
        command_contract = (
            "\n本回合允许按需选择以下两条路径之一：\n"
            "A) 如果当前环节仍需澄清（信息不足、用户上一次回答引发新问题等），"
            "提交且只提交一个 question_batch。若 emit_wplus_sop_event 工具返回 "
            "ok=false，可根据返回的 allowed agent events 与 current_stage_id "
            "修正参数后重试；失败调用不计入已持久化事件。"
            "不得提交 kind='stage_queue_confirmed'，该确认事件已由工作流服务端"
            "持久化。event_key 必须根据当前 stage_id 保持稳定。payload 必须根据"
            "已确认环节队列、用户历史回答和当前环节新生成，不得把命令输入中的 "
            "payload 原样提交。"
            "question_batch.stage_id 必须严格等于命令 payload.current_stage_id="
            + json.dumps(current_stage_id, ensure_ascii=False)
            + "。question_batch 最终 payload 示例：\n"
            + json.dumps(
                question_batch_example,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\nB) 如果当前环节的入口、范围、口径、规则、输出和下一动作都已"
            "确认、明确未知或不适用，直接进入预跑：按 trial_plan → "
            "trial_execution_started → trial_execution_progress? → "
            "trial_execution_completed|trial_execution_failed 顺序执行。"
            "严禁在信息明显不足时跳过 question_batch 直接提交 trial_plan。\n"
            "路径 B 的详细执行指令如下："
            + _build_trial_command_contract(
                run_id=run_id,
                attempt_id=attempt_id,
                requires_plan=True,
            )
        )
    elif is_trial_turn:
        command_contract = _build_trial_command_contract(
            run_id=run_id,
            attempt_id=attempt_id,
            requires_plan=effective_target_state == "GeneratingTrial",
        )
    elif is_cumulative_refresh_turn:
        command_contract = _build_cumulative_refresh_command_contract(
            payload,
        )
    elif is_finalizing_turn:
        command_contract = _build_finalizing_command_contract(
            final_result_persisted=(
                payload.get("final_result_persisted") is True
            ),
            memory_user_scope_available=(
                payload.get("memory_user_scope_available") is True
            ),
        )
    elif is_memory_write_turn:
        command_contract = _build_memory_write_command_contract(
            payload,
            run_id=run_id,
        )
    return (
        "执行下面由专用 W+ SOP 工作流界面提交的结构化命令。"
        "严格遵守 wplus-sop-miner，并在每个业务边界调用 "
        "emit_wplus_sop_event；不要从 Markdown 生成交互状态。\n"
        + json.dumps(body, ensure_ascii=False, sort_keys=True)
        + command_contract
    )


async def start_wplus_chat_turn(
    *,
    workspace: Any,
    chat: Any,
    user_id: str,
    source_id: str,
    sop_session_id: str,
    command: str,
    payload: dict[str, Any],
    run_id: str,
    attempt_id: str,
    target_state: str | None = None,
    on_complete: Callable[[], Awaitable[None]] | None = None,
    before_start: Callable[[], None] | None = None,
) -> WPlusTurnStart:
    """Start one background Agent turn on the persisted owning Chat."""
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise RuntimeError("Console channel is unavailable")

    message_id = str(uuid.uuid4())
    native_payload: dict[str, Any] = {
        "channel_id": "console",
        "sender_id": user_id,
        "content_parts": [
            TextContent(
                type=ContentType.TEXT,
                text=build_wplus_command_text(
                    command=command,
                    sop_session_id=sop_session_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    payload=payload,
                    target_state=target_state,
                ),
            ),
        ],
        "meta": {
            "session_id": chat.session_id,
            "user_id": user_id,
            "source_id": source_id,
            "msgid": message_id,
            "selected_skill_names": [WPLUS_SOP_SKILL_NAME],
            "wplus_sop_session_id": sop_session_id,
            "wplus_sop_run_id": run_id,
            "wplus_sop_attempt_id": attempt_id,
            "wplus_sop_command": command,
        },
    }
    if target_state is not None:
        native_payload["meta"]["wplus_sop_target_state"] = target_state

    trusted_runtime = WPlusRuntimeContext(
        sop_session_id=sop_session_id,
        run_id=run_id,
        attempt_id=attempt_id,
        command=command,
    )
    with bind_wplus_runtime(trusted_runtime):
        coordinator = workspace.answer_turn_coordinator
        if coordinator is None:
            raise RuntimeError("answer-turn coordinator is not configured")

        async def producer(
            identity: TurnIdentity,
            bound_payload: dict[str, Any],
        ):
            next_payload = {
                **bound_payload,
                "meta": {
                    **(bound_payload.get("meta") or {}),
                    "answer_turn_identity": identity,
                    "msgid": identity.msgid,
                },
            }
            async for event in console_channel.stream_one(next_payload):
                yield event

        lease = await coordinator.start_or_attach(
            chat.id,
            native_payload,
            producer,
            msgid=message_id,
            before_start=before_start,
        )
        queue, is_new_run = lease.queue, lease.is_new_run
        identity = lease.identity
    if not is_new_run:
        await workspace.task_tracker.detach_subscriber(identity, queue)
        raise WPlusChatRunBusyError(
            "The owning Chat already has an active Agent run",
        )

    if on_complete is None:
        await workspace.task_tracker.detach_subscriber(identity, queue)
    else:
        safe_traces = get_wplus_safe_stream_trace_registry(workspace)
        safe_traces.start_run(sop_session_id, run_id)

        async def _watch_completion() -> None:
            try:
                async for chunk in workspace.task_tracker.stream(
                    identity,
                    queue,
                ):
                    safe_traces.ingest(sop_session_id, run_id, chunk)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "W+ Agent stream watcher failed for run %s",
                    run_id,
                )
            finally:
                try:
                    await on_complete()
                finally:
                    safe_traces.finish_run(sop_session_id, run_id)

        asyncio.create_task(_watch_completion())

    return WPlusTurnStart(
        chat_id=chat.id,
        logical_chat_session_id=chat.session_id,
        message_id=message_id,
        run_id=run_id,
        attempt_id=attempt_id,
    )
