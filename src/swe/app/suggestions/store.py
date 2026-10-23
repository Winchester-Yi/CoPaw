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
from collections import OrderedDict
from typing import Dict, List, Optional, Any

# Each session stores only its latest independently owned suggestion batch.
_session_suggestions: Dict[str, List[Dict[str, Any]]] = {}
_lock = asyncio.Lock()
_MAX_AGE_SECONDS = 60
_MAX_SUGGESTIONS_PER_SESSION = 10
_MAX_ENTRY_BYTES = 16 * 1024
_MAX_SCOPE_ENTRIES = 200
_MAX_SCOPE_BYTES = 256 * 1024
_MAX_TOTAL_ENTRIES = 2_000
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_CLEANUP_BATCH = 256
_suggestion_order: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_suggestion_scopes: Dict[str, OrderedDict[str, None]] = {}
_suggestion_scope_bytes: Dict[str, int] = {}
_suggestion_bytes = 0
_suggestion_expired = 0
_suggestion_evicted = 0
_suggestion_rejected = 0

_qa_content_store: Dict[str, Dict[str, Any]] = {}
_QA_MAX_AGE_SECONDS = 120
_QA_MAX_ENTRY_BYTES = 64 * 1024
_QA_MAX_CHAT_ENTRIES = 50
_QA_MAX_SCOPE_ENTRIES = 500
_QA_MAX_SCOPE_BYTES = 1024 * 1024
_QA_MAX_TOTAL_ENTRIES = 5_000
_QA_MAX_TOTAL_BYTES = 6 * 1024 * 1024
_qa_order: OrderedDict[tuple[str, str], QAContentEntry] = OrderedDict()
_qa_scopes: Dict[str, OrderedDict[tuple[str, str], None]] = {}
_qa_scope_bytes: Dict[str, int] = {}
_qa_bytes = 0
_qa_expired = 0
_qa_evicted = 0
_qa_rejected = 0


@dataclass
class QAContentEntry:
    """A complete Q&A pair with write-time expiry and budget accounting."""

    user_message: str
    user_message_hash: str
    assistant_response: str
    ts: float
    scope_id: str
    written_at: float = 0
    expires_at: float = 0
    payload_bytes: int = 0


def _resolve_scope_key(tenant_id: Optional[str] = None) -> str:
    """解析瞬时建议存储使用的隔离键。"""
    try:
        from swe.config.context import (
            get_current_scope_id,
            get_current_source_id,
            resolve_request_effective_tenant_id,
        )

        scope_key = resolve_request_effective_tenant_id(
            tenant_id,
            get_current_source_id(),
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


def _entry_size(strings: List[str], limit: int) -> Optional[int]:
    """Reject giant inputs before allocating an encoded copy."""
    if sum(map(len, strings)) > limit:
        return None
    size = sum(len(value.encode("utf-8")) for value in strings)
    return size if size <= limit else None


def _remove_suggestion_locked(key: str, reason: str = "") -> None:
    global _suggestion_bytes, _suggestion_expired, _suggestion_evicted
    entry = _suggestion_order.pop(key, None)
    if entry is None:
        return
    del _session_suggestions[key]
    scope = entry["tenant_id"]
    _suggestion_scopes[scope].pop(key)
    _suggestion_bytes -= entry["_bytes"]
    _suggestion_scope_bytes[scope] -= entry["_bytes"]
    if not _suggestion_scopes[scope]:
        del _suggestion_scopes[scope]
        del _suggestion_scope_bytes[scope]
    if reason == "expired":
        _suggestion_expired += 1
    elif reason == "evicted":
        _suggestion_evicted += 1


def _prune_suggestions_locked() -> None:
    now = time.monotonic()
    while _suggestion_order:
        key, entry = next(iter(_suggestion_order.items()))
        if now < entry["_expires_at"]:
            break
        _remove_suggestion_locked(key, "expired")


async def store_suggestions(
    session_id: str,
    suggestions: List[str],
    tenant_id: Optional[str] = None,
) -> None:
    """Replace the latest batch, retaining at most ten complete suggestions."""
    global _suggestion_bytes, _suggestion_rejected
    if not session_id or not suggestions:
        return
    values = list(suggestions[:_MAX_SUGGESTIONS_PER_SESSION])
    limit = min(_MAX_ENTRY_BYTES, _MAX_SCOPE_BYTES, _MAX_TOTAL_BYTES)
    if sum(map(len, [tenant_id or "", session_id] + values)) > limit:
        async with _lock:
            _suggestion_rejected += 1
        return
    scope = _resolve_scope_key(tenant_id)
    # Bound key allocation before constructing the composite string.
    entry_id = str(uuid.uuid4())
    if _entry_size([scope, session_id, entry_id] + values, limit) is None:
        async with _lock:
            _suggestion_rejected += 1
        return
    key = _compose_scope_key(scope, session_id)
    size = _entry_size([key, scope, session_id, entry_id] + values, limit)
    async with _lock:
        if size is None:
            _suggestion_rejected += 1
            return
        _prune_suggestions_locked()
        _remove_suggestion_locked(key)
        while _suggestion_scopes.get(scope) and (
            len(_suggestion_scopes[scope]) >= _MAX_SCOPE_ENTRIES
            or _suggestion_scope_bytes[scope] + size > _MAX_SCOPE_BYTES
        ):
            _remove_suggestion_locked(
                next(iter(_suggestion_scopes[scope])),
                "evicted",
            )
        while _suggestion_order and (
            len(_suggestion_order) >= _MAX_TOTAL_ENTRIES
            or _suggestion_bytes + size > _MAX_TOTAL_BYTES
        ):
            _remove_suggestion_locked(next(iter(_suggestion_order)), "evicted")
        now = time.monotonic()
        entry = {
            "id": entry_id,
            "suggestions": values,
            "session_id": session_id,
            "tenant_id": scope,
            "ts": time.time(),
            "_written_at": now,
            "_expires_at": now + _MAX_AGE_SECONDS,
            "_bytes": size,
        }
        _session_suggestions[key] = [entry]
        _suggestion_order[key] = entry
        _suggestion_scopes.setdefault(scope, OrderedDict())[key] = None
        _suggestion_scope_bytes[scope] = (
            _suggestion_scope_bytes.get(scope, 0) + size
        )
        _suggestion_bytes += size


async def _read_suggestions(
    session_id: str,
    tenant_id: Optional[str],
    consume: bool,
) -> List[Dict[str, Any]]:
    if not session_id:
        return []
    key = _compose_scope_key(_resolve_scope_key(tenant_id), session_id)
    async with _lock:
        entries = _session_suggestions.get(key, [])
        valid = _prune_expired(entries)
        if entries and not valid:
            _remove_suggestion_locked(key, "expired")
        elif consume:
            _remove_suggestion_locked(key)
        return _strip_ts(valid)


async def take_suggestions(
    session_id: str,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Consume the latest batch only if it is still valid."""
    return await _read_suggestions(session_id, tenant_id, True)


async def peek_suggestions(
    session_id: str,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read without renewing the write-time expiry."""
    return await _read_suggestions(session_id, tenant_id, False)


def _prune_expired(
    suggestions: List[Dict[str, Any]],
    max_age_seconds: int = _MAX_AGE_SECONDS,
) -> List[Dict[str, Any]]:
    now = time.monotonic()
    return [
        entry
        for entry in suggestions
        if now
        < min(
            entry["_expires_at"],
            entry["_written_at"] + max_age_seconds,
        )
    ]


def _strip_ts(suggestions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"id": entry["id"], "suggestions": list(entry["suggestions"])}
        for entry in suggestions
    ]


def _hash_user_message(user_message: str) -> str:
    """Preserve the existing normalized first-200-character matching key."""
    normalized = user_message.strip().lower()[:200]
    return hashlib.md5(
        normalized.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def _qa_is_expired(entry: QAContentEntry, now: float, age: float) -> bool:
    return now >= min(entry.expires_at, entry.written_at + age)


def _remove_qa_locked(key: tuple[str, str], reason: str = "") -> None:
    global _qa_bytes, _qa_expired, _qa_evicted
    entry = _qa_order.pop(key, None)
    if entry is None:
        return
    chat, question_hash = key
    del _qa_content_store[chat][question_hash]
    if not _qa_content_store[chat]:
        del _qa_content_store[chat]
    scope = entry.scope_id
    _qa_scopes[scope].pop(key)
    _qa_bytes -= entry.payload_bytes
    _qa_scope_bytes[scope] -= entry.payload_bytes
    if not _qa_scopes[scope]:
        del _qa_scopes[scope]
        del _qa_scope_bytes[scope]
    if reason == "expired":
        _qa_expired += 1
    elif reason == "evicted":
        _qa_evicted += 1


def _prune_qa_locked() -> None:
    now = time.monotonic()
    for key, entry in list(_qa_order.items()):
        if now >= entry.expires_at:
            _remove_qa_locked(key, "expired")


async def store_qa_content(
    chat_id: str,
    user_message: str,
    assistant_response: str,
    tenant_id: Optional[str] = None,
    max_age_seconds: int = _QA_MAX_AGE_SECONDS,
) -> None:
    """Best-effort auxiliary cache; oversized pairs are skipped in full."""
    global _qa_bytes, _qa_rejected
    if not chat_id or not user_message or not assistant_response:
        return
    limit = min(_QA_MAX_ENTRY_BYTES, _QA_MAX_SCOPE_BYTES, _QA_MAX_TOTAL_BYTES)
    if (
        sum(
            map(
                len,
                [tenant_id or "", chat_id, user_message, assistant_response],
            ),
        )
        > limit
    ):
        async with _lock:
            _qa_rejected += 1
        return
    scope = _resolve_scope_key(tenant_id)
    if (
        _entry_size([scope, chat_id, user_message, assistant_response], limit)
        is None
    ):
        async with _lock:
            _qa_rejected += 1
        return
    chat = _compose_scope_key(scope, chat_id)
    question_hash = _hash_user_message(user_message)
    key = (chat, question_hash)
    size = _entry_size(
        [scope, chat, question_hash, user_message, assistant_response],
        limit,
    )
    async with _lock:
        if size is None:
            _qa_rejected += 1
            return
        _prune_qa_locked()
        _remove_qa_locked(key)
        while len(_qa_content_store.get(chat, {})) >= _QA_MAX_CHAT_ENTRIES:
            _remove_qa_locked(
                (chat, next(iter(_qa_content_store[chat]))),
                "evicted",
            )
        while _qa_scopes.get(scope) and (
            len(_qa_scopes[scope]) >= _QA_MAX_SCOPE_ENTRIES
            or _qa_scope_bytes[scope] + size > _QA_MAX_SCOPE_BYTES
        ):
            _remove_qa_locked(next(iter(_qa_scopes[scope])), "evicted")
        while _qa_order and (
            len(_qa_order) >= _QA_MAX_TOTAL_ENTRIES
            or _qa_bytes + size > _QA_MAX_TOTAL_BYTES
        ):
            _remove_qa_locked(next(iter(_qa_order)), "evicted")
        now = time.monotonic()
        entry = QAContentEntry(
            user_message,
            question_hash,
            assistant_response,
            time.time(),
            scope,
            now,
            now + max_age_seconds,
            size,
        )
        _qa_content_store.setdefault(chat, {})[question_hash] = entry
        _qa_order[key] = entry
        _qa_scopes.setdefault(scope, OrderedDict())[key] = None
        _qa_scope_bytes[scope] = _qa_scope_bytes.get(scope, 0) + size
        _qa_bytes += size


async def get_qa_content(
    chat_id: str,
    user_message: str,
    tenant_id: Optional[str] = None,
    max_age_seconds: int = _QA_MAX_AGE_SECONDS,
) -> Optional[Dict[str, str]]:
    """Read a matching pair, allowing only a shorter effective lifetime."""
    if not chat_id or not user_message:
        return None
    scope = _resolve_scope_key(tenant_id)
    chat = _compose_scope_key(scope, chat_id)
    question_hash = _hash_user_message(user_message)
    async with _lock:
        now = time.monotonic()
        for stored_hash, entry in list(
            _qa_content_store.get(chat, {}).items(),
        ):
            if _qa_is_expired(entry, now, max_age_seconds):
                _remove_qa_locked((chat, stored_hash), "expired")
        entry = _qa_content_store.get(chat, {}).get(question_hash)
        if entry is None or entry.scope_id != scope:
            return None
        return {
            "user_message": entry.user_message,
            "assistant_response": entry.assistant_response,
        }


async def prune_expired() -> None:
    """Clean both caches with at most 256 entries per lock segment."""
    suggestions = list(_suggestion_order.items())
    pairs = list(_qa_order.items())
    for start in range(0, len(suggestions), _CLEANUP_BATCH):
        async with _lock:
            now = time.monotonic()
            for key, entry in suggestions[start : start + _CLEANUP_BATCH]:
                if (
                    _suggestion_order.get(key) is entry
                    and now >= entry["_expires_at"]
                ):
                    _remove_suggestion_locked(key, "expired")
        await asyncio.sleep(0)
    for start in range(0, len(pairs), _CLEANUP_BATCH):
        async with _lock:
            now = time.monotonic()
            for qa_key, qa_entry in pairs[start : start + _CLEANUP_BATCH]:
                if (
                    _qa_order.get(qa_key) is qa_entry
                    and now >= qa_entry.expires_at
                ):
                    _remove_qa_locked(qa_key, "expired")
        await asyncio.sleep(0)


async def reset() -> None:
    """Clear all data, bounded indexes and counters after producers stop."""
    global _suggestion_bytes, _suggestion_expired, _suggestion_evicted, _suggestion_rejected
    global _qa_bytes, _qa_expired, _qa_evicted, _qa_rejected
    async with _lock:
        for container in (
            _session_suggestions,
            _suggestion_order,
            _suggestion_scopes,
            _suggestion_scope_bytes,
            _qa_content_store,
            _qa_order,
            _qa_scopes,
            _qa_scope_bytes,
        ):
            container.clear()
        _suggestion_bytes = _suggestion_expired = _suggestion_evicted = (
            _suggestion_rejected
        ) = 0
        _qa_bytes = _qa_expired = _qa_evicted = _qa_rejected = 0


def get_stats() -> Dict[str, Any]:
    """Keep existing stats and expose only aggregates for new metrics."""
    qa = {
        "entries": len(_qa_order),
        "scopes": len(_qa_scopes),
        "payload_bytes": _qa_bytes,
        "expired_total": _qa_expired,
        "evicted_total": _qa_evicted,
        "rejected_total": _qa_rejected,
    }
    return {
        "session_count": len(_session_suggestions),
        "sessions": {
            key: len(values) for key, values in _session_suggestions.items()
        },
        "entries": len(_suggestion_order),
        "scopes": len(_suggestion_scopes),
        "payload_bytes": _suggestion_bytes,
        "expired_total": _suggestion_expired,
        "evicted_total": _suggestion_evicted,
        "rejected_total": _suggestion_rejected,
        "qa": qa,
    }
