# -*- coding: utf-8 -*-
"""In-memory store for console channel push messages (e.g. cron text).

Tenant-scoped: messages are isolated by tenant_id. Each tenant has
separate message storage to prevent cross-tenant data leakage.

Bounded by entry count and saved UTF-8 data, with write-time monotonic
expiry. Process-local delivery remains best effort.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

# Per-tenant message storage: tenant_id -> list of messages
# Each tenant's messages are stored separately for isolation
_tenant_messages: Dict[str, List[Dict[str, Any]]] = {}
_lock = asyncio.Lock()
_MAX_AGE_SECONDS = 60
_MAX_MESSAGES = 500
_MAX_TOTAL_MESSAGES = 10_000
_MAX_SCOPE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_ENTRY_BYTES = 256 * 1024
_CLEANUP_BATCH = 256
_write_order: OrderedDict[str, tuple[str, Dict[str, Any]]] = OrderedDict()
_scope_bytes: Dict[str, int] = {}
_payload_bytes = 0
_expired_total = 0
_evicted_total = 0
_rejected_total = 0


def _resolve_store_key(tenant_id: Optional[str] = None) -> str:
    """Resolve the isolation key for transient console messages."""
    try:
        from swe.config.context import (
            canonicalize_scope_id,
            decode_scope_id,
            get_current_scope_id,
            get_current_source_id,
            get_current_tenant_id,
            resolve_request_effective_tenant_id,
        )

        if tenant_id is None:
            store_key = resolve_request_effective_tenant_id(
                get_current_tenant_id(),
                get_current_source_id(),
                get_current_scope_id(),
            )
        else:
            try:
                decode_scope_id(tenant_id)
                store_key = canonicalize_scope_id(tenant_id)
            except ValueError:
                if tenant_id.startswith("default_"):
                    store_key = tenant_id
                else:
                    store_key = resolve_request_effective_tenant_id(
                        tenant_id,
                        get_current_source_id(),
                        get_current_scope_id(),
                    )
        if store_key is not None:
            return store_key
    except Exception:
        pass
    return tenant_id or "default"


def _iter_matching_store_keys(tenant_id: Optional[str]) -> List[str]:
    """Return store keys that belong to the requested tenant/scope."""
    if tenant_id is None:
        try:
            from swe.config.context import (
                get_current_scope_id,
                get_current_source_id,
                get_current_tenant_id,
                resolve_request_effective_tenant_id,
            )

            effective_tenant_id = resolve_request_effective_tenant_id(
                get_current_tenant_id(),
                get_current_source_id(),
                get_current_scope_id(),
            )
            keys = [
                key
                for key in (
                    effective_tenant_id,
                    get_current_tenant_id(),
                    "default",
                )
                if key
            ]
            return list(dict.fromkeys(keys))
        except Exception:
            return ["default"]

    try:
        from swe.config.context import (
            get_current_scope_id,
            get_current_source_id,
            resolve_request_effective_tenant_id,
            resolve_runtime_tenant_id,
        )

        canonical_tenant_id = resolve_runtime_tenant_id(tenant_id, None)
        request_effective_tenant_id = resolve_request_effective_tenant_id(
            tenant_id,
            get_current_source_id(),
            get_current_scope_id(),
        )
    except Exception:
        canonical_tenant_id = tenant_id
        request_effective_tenant_id = tenant_id

    keys = {
        key
        for key in (
            canonical_tenant_id or tenant_id,
            request_effective_tenant_id,
            tenant_id,
        )
        if key
    }
    try:
        from swe.config.context import decode_scope_id

        for store_key in _tenant_messages:
            try:
                logical_tenant_id, _source_id = decode_scope_id(store_key)
            except ValueError:
                continue
            if logical_tenant_id == tenant_id:
                keys.add(store_key)
    except Exception:
        pass
    return list(keys)


def _get_tenant_store(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read a scope without allocating an empty store."""
    return _tenant_messages.get(_resolve_store_key(tenant_id), [])


def _entry_size(strings: List[str], limit: int) -> Optional[int]:
    # UTF-8 cannot use fewer bytes than characters. Reject giant inputs before
    # encoding, then encode only bounded strings.
    if sum(map(len, strings)) > limit:
        return None
    size = sum(len(value.encode("utf-8")) for value in strings)
    return size if size <= limit else None


def _remove_locked(message_id: str, reason: str = "") -> None:
    global _payload_bytes, _expired_total, _evicted_total
    item = _write_order.pop(message_id, None)
    if item is None:
        return
    scope, message = item
    messages = _tenant_messages[scope]
    messages.remove(message)
    size = message["_bytes"]
    _payload_bytes -= size
    _scope_bytes[scope] -= size
    if not messages:
        del _tenant_messages[scope]
        del _scope_bytes[scope]
    if reason == "expired":
        _expired_total += 1
    elif reason == "evicted":
        _evicted_total += 1


def _is_expired(message: Dict[str, Any], now: float, age: float) -> bool:
    return now >= min(message["_expires_at"], message["_written_at"] + age)


def _prune_scope_locked(scope: str, age: float = _MAX_AGE_SECONDS) -> None:
    now = time.monotonic()
    for message in list(_tenant_messages.get(scope, [])):
        if _is_expired(message, now, age):
            _remove_locked(message["id"], "expired")


def _prune_all_locked() -> None:
    now = time.monotonic()
    while _write_order:
        message_id, (_, message) = next(iter(_write_order.items()))
        if not _is_expired(message, now, _MAX_AGE_SECONDS):
            break
        _remove_locked(message_id, "expired")


async def append(
    session_id: str,
    text: str,
    *,
    sticky: bool = False,
    tenant_id: Optional[str] = None,
    delivery_key: str = "",
) -> bool:
    """Keep a bounded complete message, or report rejection without truncation."""
    global _payload_bytes, _rejected_total
    if not session_id or not text:
        return False
    limit = min(_MAX_ENTRY_BYTES, _MAX_SCOPE_BYTES, _MAX_TOTAL_BYTES)
    if (
        sum(map(len, [tenant_id or "", session_id, text, delivery_key]))
        > limit
    ):
        async with _lock:
            _rejected_total += 1
        return False
    scope = _resolve_store_key(tenant_id)
    message_id = str(uuid.uuid4())
    size = _entry_size(
        [scope, session_id, text, delivery_key, message_id],
        limit,
    )
    async with _lock:
        if size is None:
            _rejected_total += 1
            return False
        _prune_all_locked()
        messages = _tenant_messages.get(scope, [])
        if delivery_key and any(
            message["session_id"] == session_id
            and message["delivery_key"] == delivery_key
            for message in messages
        ):
            return True
        while _tenant_messages.get(scope) and (
            len(_tenant_messages[scope]) >= _MAX_MESSAGES
            or _scope_bytes[scope] + size > _MAX_SCOPE_BYTES
        ):
            _remove_locked(_tenant_messages[scope][0]["id"], "evicted")
        while _write_order and (
            len(_write_order) >= _MAX_TOTAL_MESSAGES
            or _payload_bytes + size > _MAX_TOTAL_BYTES
        ):
            _remove_locked(next(iter(_write_order)), "evicted")
        now = time.monotonic()
        message = {
            "id": message_id,
            "text": text,
            "sticky": sticky,
            "ts": time.time(),
            "session_id": session_id,
            "tenant_id": scope,
            "delivery_key": delivery_key,
            "_written_at": now,
            "_expires_at": now + _MAX_AGE_SECONDS,
            "_bytes": size,
        }
        _tenant_messages.setdefault(scope, []).append(message)
        _write_order[message_id] = (scope, message)
        _scope_bytes[scope] = _scope_bytes.get(scope, 0) + size
        _payload_bytes += size
        return True


async def take(
    session_id: str,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Consume only the requested session's unexpired messages."""
    if not session_id:
        return []
    async with _lock:
        scope = _resolve_store_key(tenant_id)
        _prune_scope_locked(scope)
        out = [
            message
            for message in _tenant_messages.get(scope, [])
            if message["session_id"] == session_id
        ]
        for message in out:
            _remove_locked(message["id"])
        return _strip_ts(out)


async def take_all(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Consume all unexpired messages in the resolved scope."""
    async with _lock:
        scope = _resolve_store_key(tenant_id)
        _prune_scope_locked(scope)
        out = list(_tenant_messages.get(scope, []))
        for message in out:
            _remove_locked(message["id"])
        return _strip_ts(out)


def _strip_ts(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return public message data."""
    return [
        {
            "id": m["id"],
            "text": m["text"],
            "sticky": bool(m.get("sticky", False)),
        }
        for m in msgs
    ]


async def get_recent(
    max_age_seconds: int = _MAX_AGE_SECONDS,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read without renewing TTL; callers may request a shorter window."""
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    async with _lock:
        scope = _resolve_store_key(tenant_id)
        _prune_scope_locked(scope, max_age_seconds)
        return _strip_ts(_tenant_messages.get(scope, []))


async def clear_tenant(tenant_id: Optional[str] = None) -> None:
    """Clear the same logical tenant/scope aliases as the original store."""
    async with _lock:
        for scope in _iter_matching_store_keys(tenant_id):
            for message in list(_tenant_messages.get(scope, [])):
                _remove_locked(message["id"])


async def prune_expired() -> None:
    """Physically reclaim expired data in bounded lock segments."""
    # Snapshot identities without awaiting; concurrent writes can happen only
    # at the yields below. Verify identity again inside each lock segment.
    snapshot = list(_write_order.items())
    for start in range(0, len(snapshot), _CLEANUP_BATCH):
        async with _lock:
            now = time.monotonic()
            for message_id, item in snapshot[start : start + _CLEANUP_BATCH]:
                current = _write_order.get(message_id)
                if current is item and _is_expired(
                    item[1],
                    now,
                    _MAX_AGE_SECONDS,
                ):
                    _remove_locked(message_id, "expired")
        await asyncio.sleep(0)


async def reset() -> None:
    """Reset process-local data and counters after producers have stopped."""
    global _payload_bytes, _expired_total, _evicted_total, _rejected_total
    async with _lock:
        _tenant_messages.clear()
        _write_order.clear()
        _scope_bytes.clear()
        _payload_bytes = _expired_total = _evicted_total = _rejected_total = 0


def get_stats() -> Dict[str, Any]:
    """Report bounded business-data bytes, not Python heap or RSS."""
    return {
        "tenant_count": len(_tenant_messages),
        "tenants": {
            scope: len(messages)
            for scope, messages in _tenant_messages.items()
        },
        "entries": len(_write_order),
        "scopes": len(_tenant_messages),
        "payload_bytes": _payload_bytes,
        "expired_total": _expired_total,
        "evicted_total": _evicted_total,
        "rejected_total": _rejected_total,
    }
