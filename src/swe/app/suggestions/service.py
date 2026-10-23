# -*- coding: utf-8 -*-
"""猜你想问服务 - 使用轻量级模型调用生成后续问题建议."""

import asyncio
import json
import logging
import re
from collections import OrderedDict
from typing import Any, List, Optional

from swe.agents.model_factory import (
    create_model_and_formatter,
    prepare_model_config_async,
)
from swe.tracing import capture_current_trace_context
from swe.config.context import get_current_effective_tenant_id
from swe.app.agent_context import get_current_agent_id
from swe.runtime_cache import (
    dispose_cached_model,
    dispose_cached_model_async,
)

logger = logging.getLogger(__name__)
_MODEL_CACHE_LIMIT = 32

# 建议生成的 Prompt 模板
SUGGESTION_PROMPT_TEMPLATE = """根据以下对话，生成{max_count}个用户可能想问的后续问题。
问题要简短（不超过20字）、具体、自然，符合用户的真实提问习惯。

用户问题：{user_message}
助手回答（摘要）：{assistant_response}

直接输出JSON数组格式，如：["问题1", "问题2", "问题3"]
如果没有合适的问题，输出空数组 []。
不要输出任何其他内容、解释或前缀后缀。"""


def extract_key_content(text: str, max_length: int = 500) -> str:
    """提取文本的关键内容（标题、要点、开头、结尾）.

    当文本超长时，不简单截断，而是提取并保留：
    - Markdown 标题（# ## ### 开头的行）
    - 列表项要点（- 或 1. 2. 数字开头的行）
    - 开头段落（背景/问题定义）
    - 结尾段落（结论/建议）

    Args:
        text: 原文本
        max_length: 最大输出长度

    Returns:
        提取后的关键内容
    """
    if len(text) <= max_length:
        return text

    lines = text.split("\n")

    # 1. 提取标题行（# ## ### 开头）
    title_lines = [
        line.strip() for line in lines if line.strip().startswith("#")
    ]

    # 2. 提取列表项要点（- 或 * 或数字开头）
    list_pattern = re.compile(r"^\s*[-*]\s+|^\s*\d+\.\s+")
    list_lines = [line.strip() for line in lines if list_pattern.match(line)]

    # 3. 保留开头（前 100 字）
    head_len = min(100, max_length // 4)
    head_text = text[:head_len]

    # 4. 保留结尾（后 100 字）
    tail_len = min(100, max_length // 4)
    tail_text = text[-tail_len:]

    # 组合：开头 + 标题 + 要点 + 结尾
    combined_parts = [head_text]

    if title_lines:
        # 最多保留 5 个标题
        titles = "\n".join(title_lines[:5])
        combined_parts.append(f"\n【主要标题】\n{titles}")

    if list_lines:
        # 最多保留 10 个要点
        points = "\n".join(list_lines[:10])
        combined_parts.append(f"\n【关键要点】\n{points}")

    combined_parts.append(f"\n...{tail_text}")

    result = "\n".join(combined_parts)

    # 最终截断到 max_length（兜底保护）
    if len(result) > max_length:
        # 优先保留结尾，因为通常包含结论
        keep_tail = max_length // 3
        result = result[: max_length - keep_tail] + result[-keep_tail:]

    return result


class SuggestionService:
    """建议生成服务，管理模型实例和生成逻辑."""

    _models_by_trace: "OrderedDict[tuple[str, tuple[str, str]], Any]" = (
        OrderedDict()
    )
    _lock = asyncio.Lock()
    _model_cache_generation = 0

    @staticmethod
    def _runtime_scope_key() -> tuple[str, str]:
        """返回影响模型解析的运行时作用域。"""
        tenant_id = str(get_current_effective_tenant_id() or "")
        agent_id = str(get_current_agent_id() or "")
        return tenant_id, agent_id

    @classmethod
    def _trace_cache_key(
        cls,
        trace_context: Optional[dict[str, Any]],
    ) -> tuple[str, tuple[str, str]]:
        """生成建议模型缓存键。"""
        trace_id = str((trace_context or {}).get("trace_id") or "").strip()
        return (trace_id or "__no_trace__"), cls._runtime_scope_key()

    @classmethod
    async def get_model(cls):
        """获取当前 trace 对应的模型实例。"""
        trace_context = capture_current_trace_context()
        cache_key = cls._trace_cache_key(trace_context)
        model = cls._models_by_trace.get(cache_key)
        if model is not None:
            cls._models_by_trace.move_to_end(cache_key)
            return model

        generation = cls._model_cache_generation
        prepared = await prepare_model_config_async()
        async with cls._lock:
            model = cls._models_by_trace.get(cache_key)
            if model is None:
                model, _ = create_model_and_formatter(
                    trace_context=trace_context,
                    prepared_model_config=prepared,
                )
                if generation != cls._model_cache_generation:
                    return model
                cls._models_by_trace[cache_key] = model
                while len(cls._models_by_trace) > _MODEL_CACHE_LIMIT:
                    _, evicted_model = cls._models_by_trace.popitem(
                        last=False,
                    )
                    await dispose_cached_model_async(evicted_model)
            else:
                cls._models_by_trace.move_to_end(cache_key)
        return model

    @classmethod
    def reset_model(cls, scope_id: str | None = None) -> None:
        """清理建议模型缓存，用于配置刷新或测试隔离。"""
        cls._model_cache_generation += 1
        if scope_id is None:
            keys = list(cls._models_by_trace)
        else:
            keys = [
                key
                for key in cls._models_by_trace
                if key[1][0] == str(scope_id)
            ]
        cached_models = [cls._models_by_trace.pop(key) for key in keys]
        for model in cached_models:
            dispose_cached_model(model)


def _extract_text_from_response(response) -> str:
    """从模型响应中提取文本内容."""
    if hasattr(response, "text"):
        return response.text or ""
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, str):
            return content
        if isinstance(content, list) and len(content) > 0:
            first = content[0]
            if hasattr(first, "text"):
                return first.text or ""
    return str(response) if response else ""


async def _extract_text_from_streaming_response(response) -> str:
    """从流式响应中提取最后一个 chunk 的文本内容."""
    last_chunk_text = ""
    async for chunk in response:
        if not hasattr(chunk, "content") or not chunk.content:
            continue
        for content_block in chunk.content:
            if (
                isinstance(content_block, dict)
                and content_block.get("type") == "text"
            ):
                last_chunk_text = content_block.get("text", "")
    return last_chunk_text


def _parse_suggestions_json(text: str, max_suggestions: int) -> List[str]:
    """解析模型输出的 JSON 数组为建议列表."""
    text = text.strip()

    # 尝试直接解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            suggestions = [
                s.strip() for s in data if isinstance(s, str) and s.strip()
            ]
            return suggestions[:max_suggestions]
    except json.JSONDecodeError:
        pass

    # 尝试从文本中提取 JSON 数组
    json_match = re.search(r"\[[\s\S]*\]", text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if isinstance(data, list):
                suggestions = [
                    s.strip() for s in data if isinstance(s, str) and s.strip()
                ]
                return suggestions[:max_suggestions]
        except json.JSONDecodeError:
            pass

    # 无法解析时返回空列表（静默失败）
    logger.debug("Failed to parse suggestions JSON: %s", text[:100])
    return []


async def generate_suggestions(
    user_message: str,
    assistant_response: str,
    max_suggestions: int = 3,
    timeout_seconds: float = 5.0,
    user_message_max_length: int = 200,
    assistant_response_max_length: int = 500,
) -> List[str]:
    """异步生成后续问题建议.

    Args:
        user_message: 用户最后的问题文本
        assistant_response: 助手的回答文本（可截断）
        max_suggestions: 最大建议数量 (1-5)
        timeout_seconds: 超时时间（秒）
        user_message_max_length: 用户提问截断长度
        assistant_response_max_length: 助手回答截断长度

    Returns:
        建议问题列表，失败或无建议时返回空列表
    """
    if not user_message or not assistant_response:
        return []

    # 智能提取关键内容（而非简单截断）
    truncated_response = extract_key_content(
        assistant_response,
        max_length=assistant_response_max_length,
    )

    # 用户提问截断
    truncated_user = (
        user_message[:user_message_max_length]
        if len(user_message) > user_message_max_length
        else user_message
    )

    # 调试日志：展示提取后的关键内容
    logger.info(
        "Extracted key content for suggestions:\n"
        "Original length: %d -> Extracted length: %d\n"
        "Extracted content:\n%s",
        len(assistant_response),
        len(truncated_response),
        truncated_response,
    )

    prompt = SUGGESTION_PROMPT_TEMPLATE.format(
        max_count=max_suggestions,
        user_message=truncated_user,
        assistant_response=truncated_response,
    )

    messages = [
        {
            "role": "system",
            "content": "你是一个助手，负责生成用户可能想问的后续问题。只输出JSON数组，不要其他内容。",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        model = await SuggestionService.get_model()

        # 使用超时保护 (Python 3.10 compatible)
        response = await asyncio.wait_for(
            model(messages),
            timeout=timeout_seconds,
        )

        # 处理流式响应
        if hasattr(response, "__aiter__"):
            text = await _extract_text_from_streaming_response(response)
        else:
            text = _extract_text_from_response(response)

        return _parse_suggestions_json(text, max_suggestions)

    except asyncio.TimeoutError:
        logger.debug(
            "Suggestion generation timed out after %s seconds",
            timeout_seconds,
        )
        return []
    except Exception as e:
        logger.debug("Suggestion generation failed: %s", e)
        return []
