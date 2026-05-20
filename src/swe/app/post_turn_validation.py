# -*- coding: utf-8 -*-
"""Post-turn task completion validation for confirmed continuation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from swe.agents.model_factory import create_model_and_formatter

from .suggestions.service import extract_key_content

logger = logging.getLogger(__name__)

_VALIDATION_PROMPT_TEMPLATE = """请判断下面这次回答后，用户原任务是否已经完成。

判定标准：
1. 只有在助手已经完成用户任务，或者明确被用户输入/权限/审批等外部条件阻塞时，才输出 completed=true。
2. 如果助手其实还能继续执行、继续分析、继续调用工具、继续补充结果，就输出 completed=false。
3. 当 completed=false 时，follow_up_prompt 必须写成给同一个助手的内部续跑指令，简短、可执行、不要客套、不要重复背景、不要把它写成给用户的话。
4. “猜你想问”、推荐后续问题、提示用户还可以继续提问，都是回答后的引导，不代表用户原任务未完成。
5. 不要因为回答还能扩展背景知识、补充可选建议、生成猜你想问，就输出 completed=false。
6. 不要泄露“后校验”或“内部续跑”这些概念给最终用户。

用户原任务：
{user_message}

助手最新回答（摘要）：
{assistant_response}

只输出一个 JSON 对象，不要输出其他内容，格式如下：
{{"completed": true, "reason": "...", "follow_up_prompt": ""}}"""


@dataclass(slots=True)
class PostTurnValidationResult:
    completed: bool
    reason: str = ""
    follow_up_prompt: str = ""


def _extract_text_from_response(response) -> str:
    if hasattr(response, "text"):
        return response.text or ""
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, str):
            return content
        if isinstance(content, list) and content:
            first = content[0]
            if hasattr(first, "text"):
                return first.text or ""
    return str(response) if response else ""


async def _extract_text_from_streaming_response(response) -> str:
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


_SUGGESTION_SECTION_PATTERN = re.compile(
    r"(?im)^\s*(猜你想问|你可能想问|你还可以问|后续问题|推荐问题|"
    r"可能的追问|你可以继续问)\s*[:：]?.*$",
)


def _strip_suggestion_sections(text: str) -> str:
    """Remove trailing suggestion prompts before task completion validation."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _SUGGESTION_SECTION_PATTERN.match(line):
            return "\n".join(lines[:index]).strip()
    return text.strip()


def _is_suggestion_only_continuation(
    reason: str,
    follow_up_prompt: str,
) -> bool:
    """Treat suggestion-generation continuations as completed tasks."""
    combined = f"{reason}\n{follow_up_prompt}".lower()
    suggestion_terms = (
        "猜你想问",
        "后续问题",
        "推荐问题",
        "继续提问",
        "追问",
        "suggestion",
        "follow-up question",
    )
    action_terms = (
        "生成",
        "补充",
        "提供",
        "展示",
        "添加",
        "列出",
        "引导",
        "recommend",
        "suggest",
    )
    return any(term in combined for term in suggestion_terms) and any(
        term in combined for term in action_terms
    )


def _parse_validation_result(text: str) -> PostTurnValidationResult:
    payload = text.strip()
    if not payload:
        return PostTurnValidationResult(
            completed=True,
            reason="empty validation response",
        )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", payload)
        if not match:
            logger.debug(
                "Failed to parse post-turn validation JSON: %s",
                payload[:160],
            )
            return PostTurnValidationResult(
                completed=True,
                reason="invalid validation json",
            )
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            logger.debug(
                "Failed to parse nested validation JSON: %s",
                payload[:160],
            )
            return PostTurnValidationResult(
                completed=True,
                reason="invalid nested validation json",
            )

    completed = bool(data.get("completed", True))
    reason = str(data.get("reason", "") or "").strip()
    follow_up_prompt = str(data.get("follow_up_prompt", "") or "").strip()

    if completed:
        follow_up_prompt = ""
    elif not follow_up_prompt:
        logger.debug("Validation marked incomplete without follow-up prompt")
        return PostTurnValidationResult(
            completed=True,
            reason=reason or "missing follow-up prompt",
        )
    elif _is_suggestion_only_continuation(reason, follow_up_prompt):
        logger.debug(
            "Validation marked incomplete only for follow-up suggestions; "
            "treating as completed",
        )
        return PostTurnValidationResult(
            completed=True,
            reason=reason or "suggestion-only continuation",
        )

    return PostTurnValidationResult(
        completed=completed,
        reason=reason,
        follow_up_prompt=follow_up_prompt,
    )


async def validate_task_completion(
    *,
    user_message: str,
    assistant_response: str,
    agent_id: str | None = None,
    timeout_seconds: float = 8.0,
    user_message_max_length: int = 300,
    assistant_response_max_length: int = 1200,
) -> PostTurnValidationResult:
    """Validate whether the latest turn has completed the user's task."""
    if not user_message or not assistant_response:
        return PostTurnValidationResult(
            completed=True,
            reason="missing validation input",
        )

    truncated_user = user_message[:user_message_max_length]
    cleaned_assistant_response = _strip_suggestion_sections(assistant_response)
    truncated_assistant = extract_key_content(
        cleaned_assistant_response,
        max_length=assistant_response_max_length,
    )

    prompt = _VALIDATION_PROMPT_TEMPLATE.format(
        user_message=truncated_user,
        assistant_response=truncated_assistant,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是严谨的任务完成校验器。" "只输出 JSON，不要输出解释。"
            ),
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    try:
        model, _formatter = create_model_and_formatter(agent_id=agent_id)
        response = await asyncio.wait_for(
            model(messages),
            timeout=timeout_seconds,
        )
        if hasattr(response, "__aiter__"):
            text = await _extract_text_from_streaming_response(response)
        else:
            text = _extract_text_from_response(response)
        return _parse_validation_result(text)
    except asyncio.TimeoutError:
        logger.debug(
            "Post-turn validation timed out after %s seconds",
            timeout_seconds,
        )
    except Exception as exc:
        logger.warning("Post-turn validation failed: %s", exc)

    return PostTurnValidationResult(
        completed=True,
        reason="validation fallback",
    )
