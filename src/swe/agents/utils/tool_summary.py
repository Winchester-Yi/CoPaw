# -*- coding: utf-8 -*-
"""Tool summary generator for user-friendly display."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import OrderedDict
from pathlib import PurePath
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from ...agents.model_factory import create_model_and_formatter
from ...tracing import capture_current_trace_context
from ...config.context import get_current_effective_tenant_id
from ...app.agent_context import get_current_agent_id
from ...runtime_cache import dispose_cached_model, dispose_cached_model_async

logger = logging.getLogger(__name__)

MODEL_SUMMARY_TIMEOUT_SECONDS = 0.6
_MODEL_SUMMARY_CACHE_LIMIT = 256
_SHELL_COMMAND_TOOLS = {"execute_shell_command", "start_background_process"}
_SUMMARY_MODEL_CACHE_LIMIT = 32

TOOL_DISPLAY_NAMES = {
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "append_file": "追加文件",
    "execute_shell_command": "执行操作",
    "grep_search": "内容搜索",
    "glob_search": "文件查找",
    "memory_search": "记忆检索",
    "browser_use": "网页操作",
    "desktop_screenshot": "截取屏幕",
    "get_current_time": "获取时间",
    "send_file_to_user": "发送文件",
}

_model_summary_cache: dict[str, str] = {}
_summary_models_by_trace: "OrderedDict[tuple[str, tuple[str, str]], Any]" = (
    OrderedDict()
)
_summary_model_lock = asyncio.Lock()


def _runtime_scope_key() -> tuple[str, str]:
    """返回影响摘要模型解析的运行时作用域。"""
    tenant_id = str(get_current_effective_tenant_id() or "")
    agent_id = str(get_current_agent_id() or "")
    return tenant_id, agent_id


def _summary_trace_cache_key(
    trace_context: Optional[dict[str, Any]],
) -> tuple[str, tuple[str, str]]:
    """按 trace 和运行时作用域生成摘要模型缓存键。"""
    trace_id = str((trace_context or {}).get("trace_id") or "").strip()
    return (trace_id or "__no_trace__"), _runtime_scope_key()


def reset_summary_caches(scope_id: str | None = None) -> None:
    """清理摘要生成相关缓存。"""
    if scope_id is None:
        keys = list(_summary_models_by_trace)
    else:
        keys = [
            key
            for key in _summary_models_by_trace
            if key[1][0] == str(scope_id)
        ]
    cached_models = [_summary_models_by_trace.pop(key) for key in keys]
    _model_summary_cache.clear()
    for model in cached_models:
        dispose_cached_model(model)


def get_tool_display_name(
    tool_name: str,
    server_label: Optional[str] = None,
) -> str:
    """Return a Chinese display name for a tool."""
    label = TOOL_DISPLAY_NAMES.get(tool_name) or _humanize_tool_name(tool_name)
    if server_label:
        return f"[{server_label}] {label}"
    return label


def _humanize_tool_name(tool_name: str) -> str:
    """Turn an internal tool name into a softer Chinese label."""
    if not tool_name:
        return "工具操作"
    normalized = re.sub(r"[_\-]+", " ", tool_name).strip().lower()
    hints = {
        "search": "搜索",
        "query": "查询",
        "read": "读取",
        "write": "写入",
        "edit": "编辑",
        "image": "图片",
        "video": "视频",
        "browser": "网页",
        "time": "时间",
        "file": "文件",
        "memory": "记忆",
        "shell": "操作",
        "command": "操作",
    }
    words = [hints[word] for word in normalized.split() if word in hints]
    if words:
        result = "".join(dict.fromkeys(words))
        return f"{result}操作" if not result.endswith("操作") else result
    return "工具操作"


def _truncate_text(text: str, max_length: int = 50) -> str:
    """Truncate text to max_length with ellipsis."""
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def _clean_object_text(value: str) -> str:
    """Keep human-readable object text while removing obvious noise."""
    text = re.sub(r"\s+", " ", value).strip(" \t\r\n\"'")
    if not text:
        return ""
    text = re.sub(r"^(https?://)?(www\.)?", "", text, flags=re.IGNORECASE)
    text = text.strip("/")
    text = re.sub(r"[_\-]+", " ", text)
    return _truncate_text(text, 60)


def _extract_filename(path_value: str) -> str:
    """Extract a readable filename from a path or URL path."""
    cleaned = path_value.strip()
    if not cleaned:
        return ""
    name = PurePath(cleaned).name
    return _clean_object_text(name or cleaned)


def _extract_browser_object(arguments: Any) -> str:
    """Build a user-facing browser target description from arguments."""
    parsed = _parse_json_like(arguments)
    if not isinstance(parsed, dict):
        return ""

    action = str(parsed.get("action") or "").strip().lower()
    url = str(parsed.get("url") or "").strip()
    text = str(parsed.get("text") or "").strip()
    prompt_text = str(parsed.get("prompt_text") or "").strip()
    selector = str(parsed.get("selector") or "").strip()
    filename = str(parsed.get("filename") or "").strip()

    if action in {"open", "navigate"} and url:
        parsed_url = urlparse(url)
        host = parsed_url.netloc.lower()
        query = parse_qs(parsed_url.query)
        keyword = (
            query.get("q", [""])[0]
            or query.get("query", [""])[0]
            or query.get("keyword", [""])[0]
        )
        if keyword:
            if "github" in host:
                return f"GitHub 搜索 {_clean_object_text(keyword)}"
            return f"搜索 {_clean_object_text(keyword)}"
        path_name = _extract_filename(parsed_url.path)
        if path_name:
            if "github" in host:
                return f"GitHub 上的 {path_name}"
            return path_name
        if host:
            return _clean_object_text(host)

    for value in (text, prompt_text, filename, selector):
        if value:
            return _clean_object_text(value)
    return ""


def _extract_common_object(arguments: Any) -> str:
    """Extract a compact human-readable object from tool arguments."""
    parsed = _parse_json_like(arguments)
    if isinstance(parsed, dict):
        file_value = parsed.get("file_path") or parsed.get("path")
        if isinstance(file_value, str) and file_value.strip():
            return _extract_filename(file_value)

        for key in ("query", "pattern", "keyword", "url", "text", "name"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                if key == "url":
                    browser_target = _extract_browser_object(parsed)
                    if browser_target:
                        return browser_target
                return _clean_object_text(value)
        return ""

    if isinstance(arguments, str) and not isinstance(parsed, (dict, list)):
        return _clean_object_text(arguments)
    return ""


def _get_call_object_hint(tool_name: str, arguments: Any) -> str:
    """Return a safe object hint for call-summary prompts."""
    if tool_name in _SHELL_COMMAND_TOOLS:
        return "无"
    if tool_name == "browser_use":
        return _extract_browser_object(arguments) or "无"
    return _extract_common_object(arguments) or "无"


def _parse_json_like(
    value: str | Dict[str, Any] | None,
) -> Dict[str, Any] | list[Any] | None:
    """Parse a JSON-like string, returning None on failure."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


_OBJECT_ACTION_HINTS = {
    "browser_use": "",
    "read_file": "读取",
    "write_file": "写入",
    "append_file": "写入",
    "edit_file": "编辑",
    "grep_search": "搜索",
    "memory_search": "搜索",
    "glob_search": "查找",
}


def _format_object_action(
    display_name: str,
    obj: str,
    action: str | None = None,
) -> str:
    if obj == "无":
        return f"正在{display_name}"
    if action is None:
        return f"正在{display_name}：{obj}"
    if action:
        return f"正在{action} {obj}"
    return f"正在 {obj}"


def _build_call_action_hint(
    tool_name: str,
    server_label: Optional[str],
    arguments: Any,
) -> str:
    """Build a concrete user-facing action hint for summaries."""
    display_name = get_tool_display_name(tool_name, server_label)
    if tool_name in _SHELL_COMMAND_TOOLS:
        if tool_name != "execute_shell_command":
            display_name = get_tool_display_name(
                "execute_shell_command",
                server_label,
            )
        return f"开始{display_name}"

    obj = _get_call_object_hint(tool_name, arguments)
    if tool_name in _OBJECT_ACTION_HINTS:
        return _format_object_action(
            display_name,
            obj,
            _OBJECT_ACTION_HINTS[tool_name],
        )
    return _format_object_action(display_name, obj)


def _generate_rule_based_call_summary(
    tool_name: str,
    arguments: str | Dict[str, Any],
    server_label: Optional[str] = None,
) -> str:
    """Generate a rule-based summary for tool call."""
    return _build_call_action_hint(tool_name, server_label, arguments)


_GOVERNANCE_OUTPUT_SUMMARY = {
    "pending": "操作等待审批",
    "rejected": "操作已拒绝",
    "blocked": "操作已拦截",
}


def _resolve_output_governance(value: Any) -> str | None:
    """返回可信内部标记携带的工具治理状态。"""
    if isinstance(value, str) and value in _GOVERNANCE_OUTPUT_SUMMARY:
        return value
    return None


def _generate_rule_based_output_summary(
    tool_name: str,
    output: str | Dict[str, Any] | None,
    governance_status: Any = None,
) -> str:
    """Generate a rule-based summary for tool output."""
    governance = _resolve_output_governance(governance_status)
    if governance is not None:
        return _GOVERNANCE_OUTPUT_SUMMARY[governance]

    display_name = get_tool_display_name(tool_name)
    if tool_name in _SHELL_COMMAND_TOOLS:
        parsed = (
            _parse_json_like(output) if isinstance(output, str) else output
        )
        if isinstance(parsed, dict) and parsed.get("error"):
            return "这项操作未成功完成"
        return "这项操作已完成"

    if not output:
        return f"{display_name}已完成"

    parsed = _parse_json_like(output) if isinstance(output, str) else output
    if isinstance(parsed, list) and not parsed:
        return "这次没有找到相关内容"
    if isinstance(parsed, dict):
        if parsed.get("error"):
            return f"{display_name}未成功完成"
        files = parsed.get("files")
        matches = parsed.get("matches")
        if isinstance(files, list):
            return f"找到了 {len(files)} 项相关内容"
        if isinstance(matches, list):
            return f"找到了 {len(matches)} 项相关内容"
    if isinstance(parsed, list):
        return f"共找到 {len(parsed)} 项内容"
    return f"{display_name}已完成"


def _normalize_preview(value: Any, max_length: int = 1200) -> str:
    """Serialize and trim prompt inputs for the summary model."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return _truncate_text(text, max_length)


def _redact_for_model(kind: str, tool_name: str, value: Any) -> str:
    """Redact sensitive or overly technical details before prompting."""
    if tool_name in _SHELL_COMMAND_TOOLS:
        if kind == "call":
            return "执行了一项系统操作，请概括目的，不要透露命令、参数、路径。"
        return "系统操作已返回结果，请概括是否完成，不要透露输出细节。"
    return _normalize_preview(value)


def _build_cache_key(
    kind: str,
    tool_name: str,
    payload: str,
    extra: str = "",
) -> str:
    digest = hashlib.sha256(
        f"{kind}|{tool_name}|{payload}|{extra}".encode("utf-8"),
    ).hexdigest()
    return f"{kind}:{tool_name}:{digest}"


def _remember_summary(cache_key: str, summary: str) -> str:
    if len(_model_summary_cache) >= _MODEL_SUMMARY_CACHE_LIMIT:
        _model_summary_cache.clear()
    _model_summary_cache[cache_key] = summary
    return summary


def _extract_text_from_chunk(chunk: Any) -> str:
    if hasattr(chunk, "text") and isinstance(chunk.text, str):
        return chunk.text
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        return "".join(texts)
    return ""


def _extract_text_from_response(response: Any) -> str:
    if hasattr(response, "text") and isinstance(response.text, str):
        return response.text
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        if texts:
            return "".join(texts)
    return ""


def _sanitize_model_summary(summary: str) -> str:
    summary = re.sub(r"\s+", " ", summary).strip()
    summary = summary.strip("\"'“”‘’[]")
    summary = re.sub(r"^(摘要|调用摘要|结果摘要)[:：]\s*", "", summary)
    summary = re.sub(
        r"\b(grep|glob|json|mcp|shell|stdout|stderr|cli|api|tool)\b",
        "",
        summary,
        flags=re.IGNORECASE,
    )
    summary = re.sub(
        r"\b(arguments?|params?|fields?)\b",
        "",
        summary,
        flags=re.IGNORECASE,
    )
    summary = re.sub(r"\s+", " ", summary).strip(" ，,；;:-")
    return _truncate_text(summary or "已完成", 80)


async def _get_summary_model():
    trace_context = capture_current_trace_context()
    cache_key = _summary_trace_cache_key(trace_context)
    model = _summary_models_by_trace.get(cache_key)
    if model is not None:
        _summary_models_by_trace.move_to_end(cache_key)
        return model

    async with _summary_model_lock:
        model = _summary_models_by_trace.get(cache_key)
        if model is None:
            model, _ = create_model_and_formatter(
                trace_context=trace_context,
            )
            _summary_models_by_trace[cache_key] = model
            while len(_summary_models_by_trace) > _SUMMARY_MODEL_CACHE_LIMIT:
                _, evicted_model = _summary_models_by_trace.popitem(
                    last=False,
                )
                await dispose_cached_model_async(evicted_model)
        else:
            _summary_models_by_trace.move_to_end(cache_key)
    return model


async def _run_summary_model(prompt: str) -> str:
    model = await _get_summary_model()
    messages = [
        {
            "role": "system",
            "content": (
                "你负责为 Web Console 生成工具执行摘要。"
                "只输出一句简短中文。"
                "不要出现技术术语、内部字段名、英文工具名。"
                "用用户能直接看懂的表达描述现在在做什么或结果如何。"
                "摘要不能只写成泛化的工具类别，必须尽量说清楚动作和对象。"
                "优先使用‘正在读取…’‘正在搜索…’‘正在打开…’这类表达。"
                "如果是系统操作，只能概括目的或结果，不能透露命令、"
                "参数、路径、输出细节。"
                "不要解释，不要编造，不要加前缀。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    response = await model(messages)
    if hasattr(response, "__aiter__"):
        accumulated = ""
        async for chunk in response:
            text = _extract_text_from_chunk(chunk)
            if text:
                accumulated = text
        return accumulated
    return _extract_text_from_response(response)


async def async_generate_tool_call_summary(
    tool_name: str,
    arguments: str | Dict[str, Any],
    server_label: Optional[str] = None,
) -> str:
    """Generate a tool-call summary with model first, rule fallback."""
    fallback = _generate_rule_based_call_summary(
        tool_name,
        arguments,
        server_label,
    )
    payload = _redact_for_model("call", tool_name, arguments)
    cache_key = _build_cache_key(
        "call",
        tool_name,
        payload,
        server_label or "",
    )
    if cache_key in _model_summary_cache:
        return _model_summary_cache[cache_key]

    prompt = (
        f"工具显示名: {get_tool_display_name(tool_name, server_label)}\n"
        f"内部工具名: {tool_name or 'unknown'}\n"
        f"调用信息: {payload or '无'}\n"
        f"建议保留的操作对象: {_get_call_object_hint(tool_name, arguments)}\n"
        "建议动作表达: "
        f"{_build_call_action_hint(tool_name, server_label, arguments)}\n"
        "请用一句自然中文描述这次操作在做什么。"
        "不要只说工具类别，要尽量写清楚正在做的动作和对象。"
    )

    try:
        summary = await asyncio.wait_for(
            _run_summary_model(prompt),
            timeout=MODEL_SUMMARY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("Failed to generate model tool call summary: %s", exc)
        return fallback

    summary = _sanitize_model_summary(summary)
    if not summary:
        return fallback
    return _remember_summary(cache_key, summary)


async def async_generate_tool_output_summary(
    tool_name: str,
    output: str | Dict[str, Any] | None,
    arguments: str | Dict[str, Any] | None = None,
) -> str:
    """Generate a tool-output summary with model first, rule fallback."""
    fallback = _generate_rule_based_output_summary(tool_name, output)
    output_payload = _redact_for_model("output", tool_name, output)
    argument_payload = _redact_for_model("call", tool_name, arguments)
    cache_key = _build_cache_key(
        "output",
        tool_name,
        output_payload,
        argument_payload,
    )
    if cache_key in _model_summary_cache:
        return _model_summary_cache[cache_key]

    prompt = (
        f"工具显示名: {get_tool_display_name(tool_name)}\n"
        f"内部工具名: {tool_name or 'unknown'}\n"
        f"调用信息: {argument_payload or '无'}\n"
        f"结果信息: {output_payload or '无输出'}\n"
        "请用一句自然中文描述结果，不要出现技术术语。"
        "如果没有结果，要明确说没有找到或没有内容。"
    )

    try:
        summary = await asyncio.wait_for(
            _run_summary_model(prompt),
            timeout=MODEL_SUMMARY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("Failed to generate model tool output summary: %s", exc)
        return fallback

    summary = _sanitize_model_summary(summary)
    if not summary:
        return fallback
    return _remember_summary(cache_key, summary)


def generate_tool_call_summary(
    tool_name: str,
    arguments: str | Dict[str, Any],
    server_label: Optional[str] = None,
) -> str:
    """Generate user-friendly summary for tool call."""
    return _generate_rule_based_call_summary(
        tool_name,
        arguments,
        server_label,
    )


def generate_tool_output_summary(
    tool_name: str,
    output: str | Dict[str, Any] | None,
    governance_status: Any = None,
) -> str:
    """Generate user-friendly summary for tool output."""
    return _generate_rule_based_output_summary(
        tool_name,
        output,
        governance_status,
    )
