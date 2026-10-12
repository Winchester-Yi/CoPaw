# -*- coding: utf-8 -*-
"""会话标题生成服务。

调用外部标题生成 API，根据用户首个问题生成简短标题。
未配置环境变量时优雅降级，不发起请求。
"""

from __future__ import annotations

import logging
import os

import httpx
from ..token_diagnostics.runtime import external_call

logger = logging.getLogger(__name__)

_TITLE_API_URL = os.environ.get("TITLE_API_URL", "")
_TITLE_API_KEY = os.environ.get("TITLE_API_KEY", "")
_TITLE_TIMEOUT = 30
_MAX_TITLE_LENGTH = 15


async def generate_title(question: str) -> str | None:
    """异步调用外部标题生成 API。

    Args:
        question: 用户首个问题

    Returns:
        生成的标题（不超过 _MAX_TITLE_LENGTH 字），失败或未配置时返回 None
    """
    if not _TITLE_API_URL:
        return None

    if not question or not question.strip():
        return None

    try:
        with external_call("title"):
            return await _request_title(question)
    except Exception:
        logger.warning("标题生成失败", exc_info=True)
        return None


async def _request_title(question):
    async with httpx.AsyncClient(timeout=_TITLE_TIMEOUT) as client:
        resp = await client.post(
            _TITLE_API_URL,
            headers={
                "API-Key": _TITLE_API_KEY,
                "Content-Type": "application/json",
            },
            json={"inputParams": {"question": question.strip()}},
        )
        resp.raise_for_status()
        data = resp.json()
        title = data.get("body", {}).get("output", {}).get("res", "")
        if title and title.strip():
            return title.strip()[:_MAX_TITLE_LENGTH]
        return None
