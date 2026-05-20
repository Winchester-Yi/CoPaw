# -*- coding: utf-8 -*-
"""建议存储模块 - 存储生成后的猜你想问建议供前端轮询获取.

基于 session_id 存储，前端在主响应完成后轮询获取建议。
建议有过期时间，自动清理。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

# Per-session suggestion storage: session_id -> list of suggestions
_session_suggestions: Dict[str, List[Dict[str, Any]]] = {}
_lock = asyncio.Lock()
_MAX_AGE_SECONDS = 60  # 建议有效期60秒
_MAX_SUGGESTIONS_PER_SESSION = 10  # 每个session最多存储的建议数

# Q&A 内容存储：chat_id -> {user_message_hash: QAContentEntry}
_qa_content_store: Dict[str, Dict[str, Any]] = {}
_QA_MAX_AGE_SECONDS = 120  # Q&A 内容有效期120秒


@dataclass
class QAContentEntry:
    """Q&A 内容条目."""

    user_message: str  # 提取后的用户问题
    user_message_hash: str  # 用户问题的 hash
    assistant_response: str  # 提取后的助手回答
    ts: float  # 存储时间戳
    scope_id: str  # 运行时隔离范围


def _resolve_scope_key(tenant_id: Optional[str] = None) -> str:
    """解析瞬时建议存储使用的隔离键。"""
    try:
        from swe.config.context import (
            get_current_scope_id,
            resolve_scope_preferred_tenant_id,
        )

        scope_key = resolve_scope_preferred_tenant_id(
            tenant_id,
            None,
            get_current_scope_id(),
        )
        if scope_key is not None:
            return scope_key
    except Exception:
        pass
    return tenant_id or "default"


def _compose_scope_key(scope_id: str, raw_key: str) -> str:
    """组合 scope 与业务键，避免不同 scope 下发生碰撞。"""
    return f"{scope_id}::{raw_key}"


async def store_suggestions(
    session_id: str,
    suggestions: List[str],
    tenant_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> None:
    """存储建议列表到 session_id 对应的存储中.

    Args:
        session_id: Session identifier.
        suggestions: 建议问题列表.
        tenant_id: Tenant identifier for isolation.
        turn_id: 当前回答轮次标识，用于避免同一 session 连续轮次串建议。
    """
    if not session_id or not suggestions:
        return
    scope_key = _resolve_scope_key(tenant_id)
    scoped_session_id = _compose_scope_key(scope_key, session_id)

    async with _lock:
        current_entries = _session_suggestions.get(scoped_session_id, [])
        _prune_expired(current_entries)
        suggestion_entry = {
            "id": str(uuid.uuid4()),
            "suggestions": suggestions,
            "ts": time.time(),
            "session_id": session_id,
            "tenant_id": scope_key,
            "turn_id": turn_id,
        }

        if turn_id:
            existing = [
                item
                for item in current_entries
                if item.get("turn_id") != turn_id
            ]
            existing.append(suggestion_entry)
            _session_suggestions[scoped_session_id] = existing[
                -_MAX_SUGGESTIONS_PER_SESSION:
            ]
        else:
            _session_suggestions[scoped_session_id] = [suggestion_entry]


async def take_suggestions(
    session_id: str,
    tenant_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """获取并移除 session_id 对应的所有建议.

    Args:
        session_id: Session identifier.
        tenant_id: Tenant identifier for isolation.
        turn_id: 指定回答轮次时只消费该轮建议。

    Returns:
        建议列表，每个建议包含 id 和 suggestions 字段。
    """
    if not session_id:
        return []
    scope_key = _resolve_scope_key(tenant_id)
    scoped_session_id = _compose_scope_key(scope_key, session_id)

    async with _lock:
        suggestions = _session_suggestions.get(scoped_session_id, [])
        _prune_expired(suggestions)

        if turn_id:
            matched = [
                item for item in suggestions if item.get("turn_id") == turn_id
            ]
            remaining = [
                item for item in suggestions if item.get("turn_id") != turn_id
            ]
            if remaining:
                _session_suggestions[scoped_session_id] = remaining
            elif scoped_session_id in _session_suggestions:
                del _session_suggestions[scoped_session_id]
            return _strip_ts(matched)

        # 未指定 turn_id 时保持历史行为：消费该 session 下全部建议。
        if scoped_session_id in _session_suggestions:
            del _session_suggestions[scoped_session_id]

        return _strip_ts(suggestions)


async def peek_suggestions(
    session_id: str,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """查看但不移除 session_id 对应的建议（用于检查是否有建议）.

    Args:
        session_id: Session identifier.
        tenant_id: Tenant identifier for isolation.

    Returns:
        建议列表（不移除）。
    """
    if not session_id:
        return []
    scope_key = _resolve_scope_key(tenant_id)
    scoped_session_id = _compose_scope_key(scope_key, session_id)

    async with _lock:
        suggestions = _session_suggestions.get(scoped_session_id, [])
        _prune_expired(suggestions)
        return _strip_ts(suggestions)


def _prune_expired(
    suggestions: List[Dict[str, Any]],
    max_age_seconds: int = _MAX_AGE_SECONDS,
) -> List[Dict[str, Any]]:
    """清理过期建议（就地清理）."""
    cutoff = time.time() - max_age_seconds
    suggestions[:] = [s for s in suggestions if s.get("ts", 0) >= cutoff]
    return suggestions


def _strip_ts(suggestions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """移除内部时间戳字段."""
    stripped = []
    for suggestion in suggestions:
        item = {
            "id": suggestion["id"],
            "suggestions": suggestion["suggestions"],
        }
        turn_id = suggestion.get("turn_id")
        if turn_id:
            item["turn_id"] = turn_id
        stripped.append(item)
    return stripped


def get_stats() -> Dict[str, Any]:
    """获取存储统计信息."""
    return {
        "session_count": len(_session_suggestions),
        "sessions": {
            session_id: len(suggestions)
            for session_id, suggestions in _session_suggestions.items()
        },
    }


def _hash_user_message(user_message: str) -> str:
    """生成稳定的问题匹配键，和前端查询时的原始问题保持可匹配。"""
    normalized = user_message.strip().lower()[:200]
    return hashlib.md5(
        normalized.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def _prune_expired_qa_content(
    entries: Dict[str, Any],
    max_age_seconds: int = _QA_MAX_AGE_SECONDS,
) -> Dict[str, Any]:
    cutoff = time.time() - max_age_seconds
    return {
        key: value
        for key, value in entries.items()
        if getattr(value, "ts", 0) >= cutoff
    }


async def store_qa_content(
    chat_id: str,
    user_message: str,
    assistant_response: str,
    tenant_id: Optional[str] = None,
    max_age_seconds: int = _QA_MAX_AGE_SECONDS,
) -> None:
    """Store extracted Q&A content keyed by chat_id and user message hash."""
    if not chat_id or not user_message or not assistant_response:
        return

    scope_key = _resolve_scope_key(tenant_id)
    scoped_chat_id = _compose_scope_key(scope_key, chat_id)
    user_message_hash = _hash_user_message(user_message)
    entry = QAContentEntry(
        user_message=user_message,
        user_message_hash=user_message_hash,
        assistant_response=assistant_response,
        ts=time.time(),
        scope_id=scope_key,
    )

    async with _lock:
        existing = _qa_content_store.get(scoped_chat_id, {})
        existing = _prune_expired_qa_content(existing, max_age_seconds)
        existing[user_message_hash] = entry
        _qa_content_store[scoped_chat_id] = existing


async def get_qa_content(
    chat_id: str,
    user_message: str,
    tenant_id: Optional[str] = None,
    max_age_seconds: int = _QA_MAX_AGE_SECONDS,
) -> Optional[Dict[str, str]]:
    """Get extracted Q&A content for a chat_id + user message pair."""
    if not chat_id or not user_message:
        return None

    scope_key = _resolve_scope_key(tenant_id)
    scoped_chat_id = _compose_scope_key(scope_key, chat_id)
    user_message_hash = _hash_user_message(user_message)

    async with _lock:
        entries = _qa_content_store.get(scoped_chat_id, {})
        entries = _prune_expired_qa_content(entries, max_age_seconds)
        if entries:
            _qa_content_store[scoped_chat_id] = entries
        elif scoped_chat_id in _qa_content_store:
            del _qa_content_store[scoped_chat_id]

        entry = entries.get(user_message_hash)
        if entry is None or entry.scope_id != scope_key:
            return None

        return {
            "user_message": entry.user_message,
            "assistant_response": entry.assistant_response,
        }
