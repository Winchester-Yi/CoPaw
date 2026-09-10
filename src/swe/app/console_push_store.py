# -*- coding: utf-8 -*-
"""In-memory store for console channel push messages (e.g. cron text).

Tenant-scoped: messages are isolated by tenant_id. Each tenant has
separate message storage to prevent cross-tenant data leakage.

Bounded: at most _MAX_MESSAGES kept per tenant; messages older than
_MAX_AGE_SECONDS are dropped when reading.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional

# Per-tenant message storage: tenant_id -> list of messages
# Each tenant's messages are stored separately for isolation
_tenant_messages: Dict[str, List[Dict[str, Any]]] = {}
_lock = asyncio.Lock()
_MAX_AGE_SECONDS = 60
_MAX_MESSAGES = 500


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
    """Get or create message store for tenant.

    Args:
        tenant_id: Tenant identifier. If None, uses "default".

    Returns:
        Message list for the tenant.
    """
    tenant_id = _resolve_store_key(tenant_id)

    if tenant_id not in _tenant_messages:
        _tenant_messages[tenant_id] = []

    return _tenant_messages[tenant_id]


async def append(
    session_id: str,
    text: str,
    *,
    sticky: bool = False,
    tenant_id: Optional[str] = None,
    delivery_key: str = "",
) -> None:
    """Append a message (bounded: oldest dropped if over _MAX_MESSAGES).

    Args:
        session_id: Session identifier.
        text: Message text.
        sticky: Whether message is sticky.
        tenant_id: Tenant identifier for isolation. If None, uses "default".
    """
    if not session_id or not text:
        return

    async with _lock:
        store_key = _resolve_store_key(tenant_id)
        msg_list = _get_tenant_store(store_key)

        if delivery_key and any(
            message.get("session_id") == session_id
            and message.get("delivery_key") == delivery_key
            for message in msg_list
        ):
            return

        msg_list.append(
            {
                "id": str(uuid.uuid4()),
                "text": text,
                "sticky": sticky,
                "ts": time.time(),
                "session_id": session_id,
                "tenant_id": store_key,
                "delivery_key": delivery_key,
            },
        )

        # Enforce max messages limit per tenant
        if len(msg_list) > _MAX_MESSAGES:
            msg_list.sort(key=lambda m: m["ts"])
            del msg_list[: len(msg_list) - _MAX_MESSAGES]


async def take(
    session_id: str,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return and remove all messages for the session.

    Args:
        session_id: Session identifier.
        tenant_id: Tenant identifier for isolation. If None, uses "default".

    Returns:
        List of messages for the session.
    """
    if not session_id:
        return []

    async with _lock:
        store_key = _resolve_store_key(tenant_id)
        msg_list = _get_tenant_store(store_key)
        _prune_expired_locked(msg_list, _MAX_AGE_SECONDS)

        out = []
        remaining = []
        for msg in msg_list:
            if msg.get("session_id") == session_id:
                out.append(msg)
            else:
                remaining.append(msg)

        # Update the tenant store with remaining messages
        _tenant_messages[store_key] = remaining

        return _strip_ts(out)


async def take_all(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return and remove all non-expired messages from the store.

    Args:
        tenant_id: Tenant identifier for isolation. If None, uses "default".

    Returns:
        List of all messages for the tenant.
    """
    async with _lock:
        msg_list = _get_tenant_store(tenant_id)
        _prune_expired_locked(msg_list, _MAX_AGE_SECONDS)

        out = list(msg_list)
        msg_list.clear()

        return _strip_ts(out)


def _strip_ts(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip internal timestamp from messages."""
    return [
        {
            "id": m["id"],
            "text": m["text"],
            "sticky": bool(m.get("sticky", False)),
        }
        for m in msgs
    ]


def _prune_expired_locked(
    msg_list: List[Dict[str, Any]],
    max_age_seconds: int,
) -> None:
    """Drop expired messages in-place. Caller must hold _lock."""
    cutoff = time.time() - max_age_seconds
    msg_list[:] = [m for m in msg_list if m["ts"] >= cutoff]


async def get_recent(
    max_age_seconds: int = _MAX_AGE_SECONDS,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return recent messages (not consumed) for tenant.

    Args:
        max_age_seconds: Maximum age of messages to return.
        tenant_id: Tenant identifier for isolation. If None, uses "default".

    Returns:
        List of recent messages for the tenant.
    """
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")

    async with _lock:
        msg_list = _get_tenant_store(tenant_id)
        _prune_expired_locked(msg_list, max_age_seconds)
        return _strip_ts(msg_list)


async def clear_tenant(tenant_id: Optional[str] = None) -> None:
    """Clear all messages for a tenant.

    Args:
        tenant_id: Tenant identifier. If None, clears "default" tenant.
    """
    async with _lock:
        for store_key in _iter_matching_store_keys(tenant_id):
            if store_key in _tenant_messages:
                del _tenant_messages[store_key]


def get_stats() -> Dict[str, Any]:
    """Get store statistics.

    Returns:
        Dictionary with tenant count and message counts per tenant.
    """
    return {
        "tenant_count": len(_tenant_messages),
        "tenants": {
            tenant_id: len(msgs)
            for tenant_id, msgs in _tenant_messages.items()
        },
    }
