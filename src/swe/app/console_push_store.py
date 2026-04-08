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


def _get_tenant_store(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get or create message store for tenant.

    Args:
        tenant_id: Tenant identifier. If None, uses "default".

    Returns:
        Message list for the tenant.
    """
    if tenant_id is None:
        tenant_id = "default"

    if tenant_id not in _tenant_messages:
        _tenant_messages[tenant_id] = []

    return _tenant_messages[tenant_id]


async def append(
    session_id: str,
    text: str,
    *,
    sticky: bool = False,
    tenant_id: Optional[str] = None,
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
        msg_list = _get_tenant_store(tenant_id)

        msg_list.append(
            {
                "id": str(uuid.uuid4()),
                "text": text,
                "sticky": sticky,
                "ts": time.time(),
                "session_id": session_id,
                "tenant_id": tenant_id or "default",
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
        msg_list = _get_tenant_store(tenant_id)
        _prune_expired_locked(msg_list, _MAX_AGE_SECONDS)

        out = []
        remaining = []
        for msg in msg_list:
            if msg.get("session_id") == session_id:
                out.append(msg)
            else:
                remaining.append(msg)

        # Update the tenant store with remaining messages
        if tenant_id is None:
            tenant_id = "default"
        _tenant_messages[tenant_id] = remaining

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
    if tenant_id is None:
        tenant_id = "default"

    async with _lock:
        if tenant_id in _tenant_messages:
            del _tenant_messages[tenant_id]


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
