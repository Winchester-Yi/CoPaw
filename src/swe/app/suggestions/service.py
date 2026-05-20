# -*- coding: utf-8 -*-
"""猜你想问服务 - 使用轻量级模型调用生成后续问题建议."""

import asyncio
import json
import logging
import re
from typing import List, Optional

from agentscope.model import ChatModelBase

from swe.agents.model_factory import create_model_and_formatter

logger = logging.getLogger(__name__)

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

    _model: Optional[ChatModelBase] = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_model(cls) -> ChatModelBase:
        """获取或创建模型实例（懒加载单例）."""
        if cls._model is None:
            async with cls._lock:
                if cls._model is None:
                    model, _ = create_model_and_formatter()
                    cls._model = model
        return cls._model

    @classmethod
    def reset_model(cls) -> None:
        """重置模型实例（用于配置变更时）."""
        cls._model = None


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
    """从流式响应中提取完整文本内容."""
    accumulated = ""
    async for chunk in response:
        if not hasattr(chunk, "content") or not chunk.content:
            continue
        chunk_text = ""
        for content_block in chunk.content:
            if (
                isinstance(content_block, dict)
                and content_block.get("type") == "text"
            ):
                chunk_text += content_block.get("text", "")
        if not chunk_text:
            continue
        if chunk_text.startswith(accumulated):
            accumulated = chunk_text
        else:
            accumulated += chunk_text
    return accumulated


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


def _render_suggestion_prompt(
    prompt_template: str | None,
    max_count: int,
    user_message: str,
    assistant_response: str,
) -> str:
    """渲染建议生成 prompt，模板异常时回退到内置模板。"""
    candidate_template = (
        prompt_template.strip()
        if isinstance(prompt_template, str) and prompt_template.strip()
        else SUGGESTION_PROMPT_TEMPLATE
    )
    try:
        return candidate_template.format(
            max_count=max_count,
            user_message=user_message,
            assistant_response=assistant_response,
        )
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "Failed to render suggestion prompt template, fallback default: %s",
            exc,
        )
        return SUGGESTION_PROMPT_TEMPLATE.format(
            max_count=max_count,
            user_message=user_message,
            assistant_response=assistant_response,
        )


async def generate_suggestions(
    user_message: str,
    assistant_response: str,
    max_suggestions: int = 3,
    timeout_seconds: float = 5.0,
    user_message_max_length: int = 200,
    assistant_response_max_length: int = 500,
    prompt_template: str | None = None,
) -> List[str]:
    """异步生成后续问题建议.

    Args:
        user_message: 用户最后的问题文本
        assistant_response: 助手的回答文本（可截断）
        max_suggestions: 最大建议数量 (1-5)
        timeout_seconds: 超时时间（秒）
        user_message_max_length: 用户提问截断长度
        assistant_response_max_length: 助手回答截断长度
        prompt_template: 可选的提示词模板

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

    prompt = _render_suggestion_prompt(
        prompt_template=prompt_template,
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
