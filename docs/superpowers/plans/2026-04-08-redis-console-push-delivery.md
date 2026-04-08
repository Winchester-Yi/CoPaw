# Redis Console Push Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move console push delivery from process-local memory to Redis so push messages are shared across backend instances while staying isolated by tenant and session.

**Architecture:** Keep the existing `append(session_id, text, *, sticky=False, tenant_id=None)` and `take(session_id, tenant_id=None)` API stable for callers, but replace the in-memory implementation in `src/swe/app/console_push_store.py` with a Redis-backed store object plus thin module-level wrappers. Require `session_id` on `/api/console/push-messages`, and pass `tenant_id` explicitly from proactive console sends so every writer hits the same shared Redis queue.

**Tech Stack:** Python, FastAPI, `redis.asyncio`, `fakeredis`, pytest

---

## File Map

- `pyproject.toml`
  - Add `redis` runtime dependency and `fakeredis` dev dependency.
- `src/swe/app/console_push_store.py`
  - Replace the dict-based store with a Redis-backed implementation.
  - Keep the public async wrappers `append(...)`, `take(...)`, `get_recent(...)`, `clear_tenant(...)`, and `get_stats()`.
- `src/swe/app/routers/console.py`
  - Require `session_id` and remove tenant-wide draining from `/console/push-messages`.
- `src/swe/app/channels/console/channel.py`
  - Pass the bound workspace tenant into proactive push writes.
- `tests/unit/app/test_console_push_store.py`
  - Add Redis-backed store contract tests.
- `tests/unit/routers/test_console_tenant_isolation.py`
  - Keep `session_id is required` coverage and assert tenant/session forwarding to the store.
- `tests/unit/app/test_console_channel_push.py`
  - New regression tests for `ConsoleChannel.send(...)` and `send_content_parts(...)`.
- `tests/unit/app/test_tenant_cron_manager_push.py`
  - Keep cron compatibility pinned to the unchanged append contract.

## Task 1: Add Dependencies And Define The Store Contract

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/unit/app/test_console_push_store.py`

- [ ] **Step 1: Rewrite the store tests around the Redis contract**

Replace the current in-memory-only tests in `tests/unit/app/test_console_push_store.py` with async tests that target a new `RedisConsolePushStore` class.

```python
# tests/unit/app/test_console_push_store.py
# -*- coding: utf-8 -*-
import importlib

import fakeredis.aioredis
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fake_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


async def test_take_reads_message_written_by_separate_store_instance(fake_client):
    from swe.app.console_push_store import RedisConsolePushStore

    writer = RedisConsolePushStore(fake_client, key_prefix="test:console-push")
    reader = RedisConsolePushStore(fake_client, key_prefix="test:console-push")

    await writer.append("session-a", "hello", tenant_id="tenant-a")
    messages = await reader.take("session-a", tenant_id="tenant-a")

    assert len(messages) == 1
    assert messages[0]["text"] == "hello"
    assert messages[0]["sticky"] is False
    assert messages[0]["id"]


async def test_take_is_isolated_by_tenant_and_session(fake_client):
    from swe.app.console_push_store import RedisConsolePushStore

    store = RedisConsolePushStore(fake_client, key_prefix="test:console-push")

    await store.append("session-a", "tenant-a only", tenant_id="tenant-a")
    await store.append("session-b", "same tenant other session", tenant_id="tenant-a")
    await store.append("session-a", "tenant-b only", tenant_id="tenant-b")

    assert [m["text"] for m in await store.take("session-a", tenant_id="tenant-a")] == [
        "tenant-a only",
    ]
    assert [m["text"] for m in await store.take("session-b", tenant_id="tenant-a")] == [
        "same tenant other session",
    ]
    assert [m["text"] for m in await store.take("session-a", tenant_id="tenant-b")] == [
        "tenant-b only",
    ]


async def test_append_trims_to_max_messages(fake_client):
    from swe.app.console_push_store import RedisConsolePushStore

    store = RedisConsolePushStore(
        fake_client,
        key_prefix="test:console-push",
        max_messages=2,
    )

    await store.append("session-a", "one", tenant_id="tenant-a")
    await store.append("session-a", "two", tenant_id="tenant-a")
    await store.append("session-a", "three", tenant_id="tenant-a")

    assert [m["text"] for m in await store.take("session-a", tenant_id="tenant-a")] == [
        "two",
        "three",
    ]


def test_default_store_requires_redis_url(monkeypatch):
    module = importlib.import_module("swe.app.console_push_store")
    monkeypatch.delenv("SWE_CONSOLE_PUSH_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(module, "_DEFAULT_STORE", None, raising=False)

    with pytest.raises(RuntimeError, match="Console push delivery requires Redis"):
        module._get_default_store()
```

- [ ] **Step 2: Run the rewritten tests before adding dependencies**

Run:

```bash
venv/bin/python -m pytest tests/unit/app/test_console_push_store.py -v
```

Expected:

- FAIL with `ModuleNotFoundError: No module named 'fakeredis'`

- [ ] **Step 3: Add the dependencies**

Update `pyproject.toml` with these exact additions:

```toml
[project]
dependencies = [
    "agentscope==1.0.18",
    "agentscope-runtime==1.1.3",
    "httpx>=0.27.0",
    "packaging>=24.0",
    "redis>=5.0.0",
    "discord-py>=2.3",
    "dingtalk-stream>=0.24.3",
    "uvicorn>=0.40.0",
]
```

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.3.5",
    "pytest-asyncio>=0.23.0",
    "pre-commit>=4.2.0",
    "pytest-cov>=6.2.1",
    "hypothesis>=6.0.0",
    "fakeredis>=2.23.0",
]
```

- [ ] **Step 4: Install and rerun the failing tests**

Run:

```bash
venv/bin/pip install -e '.[dev]'
venv/bin/python -m pytest tests/unit/app/test_console_push_store.py -v
```

Expected:

- `pip` installs `redis` and `fakeredis`
- pytest still FAILS, now because `RedisConsolePushStore` does not exist yet

- [ ] **Step 5: Commit the dependency and failing-test baseline**

```bash
git add pyproject.toml tests/unit/app/test_console_push_store.py
git commit -m "test(app): define redis console push store contract"
```

## Task 2: Replace The In-Memory Store With Redis

**Files:**
- Modify: `src/swe/app/console_push_store.py:1-205`
- Modify: `tests/unit/app/test_console_push_store.py`

- [ ] **Step 1: Implement the Redis-backed store object**

Replace the dict-based implementation in `src/swe/app/console_push_store.py` with a Redis-backed class and lazy singleton wrappers. Use one key per tenant session: `swe:console-push:{tenant_id}:{session_id}`.

```python
# src/swe/app/console_push_store.py
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from redis.asyncio import Redis, from_url

_DEFAULT_TENANT = "default"
_MAX_AGE_SECONDS = 60
_MAX_MESSAGES = 500
_KEY_PREFIX = "swe:console-push"
_DEFAULT_STORE: "RedisConsolePushStore | None" = None


def _normalize_tenant(tenant_id: Optional[str]) -> str:
    return tenant_id or _DEFAULT_TENANT


def _get_redis_url() -> str:
    for env_name in ("SWE_CONSOLE_PUSH_REDIS_URL", "REDIS_URL"):
        value = os.getenv(env_name)
        if value:
            return value
    raise RuntimeError(
        "Console push delivery requires Redis. "
        "Set SWE_CONSOLE_PUSH_REDIS_URL or REDIS_URL."
    )


class RedisConsolePushStore:
    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = _KEY_PREFIX,
        max_age_seconds: int = _MAX_AGE_SECONDS,
        max_messages: int = _MAX_MESSAGES,
    ):
        self._client = client
        self._key_prefix = key_prefix
        self._max_age_seconds = max_age_seconds
        self._max_messages = max_messages

    def _key(self, session_id: str, tenant_id: Optional[str] = None) -> str:
        tenant = _normalize_tenant(tenant_id)
        return f"{self._key_prefix}:{tenant}:{session_id}"

    async def append(
        self,
        session_id: str,
        text: str,
        *,
        sticky: bool = False,
        tenant_id: Optional[str] = None,
    ) -> None:
        if not session_id or not text:
            return

        ts = time.time()
        payload = json.dumps(
            {
                "id": str(uuid.uuid4()),
                "text": text,
                "sticky": sticky,
                "ts": ts,
            },
            separators=(",", ":"),
        )
        key = self._key(session_id, tenant_id)
        await self._client.zremrangebyscore(key, "-inf", ts - self._max_age_seconds)
        await self._client.zadd(key, {payload: ts})
        size = await self._client.zcard(key)
        if size > self._max_messages:
            await self._client.zremrangebyrank(key, 0, size - self._max_messages - 1)
        await self._client.expire(key, self._max_age_seconds)

    async def take(
        self,
        session_id: str,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not session_id:
            return []

        key = self._key(session_id, tenant_id)
        cutoff = time.time() - self._max_age_seconds
        await self._client.zremrangebyscore(key, "-inf", cutoff)
        rows = await self._client.zrange(key, 0, -1)
        await self._client.delete(key)
        return self._strip_ts([json.loads(row) for row in rows])

    async def clear_tenant(self, tenant_id: Optional[str] = None) -> None:
        tenant = _normalize_tenant(tenant_id)
        cursor = 0
        pattern = f"{self._key_prefix}:{tenant}:*"
        while True:
            cursor, keys = await self._client.scan(cursor=cursor, match=pattern)
            if keys:
                await self._client.delete(*keys)
            if cursor == 0:
                return

    @staticmethod
    def _strip_ts(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "id": item["id"],
                "text": item["text"],
                "sticky": bool(item.get("sticky", False)),
            }
            for item in msgs
        ]


def _get_default_store() -> RedisConsolePushStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = RedisConsolePushStore(
            from_url(_get_redis_url(), decode_responses=True),
        )
    return _DEFAULT_STORE


async def append(session_id: str, text: str, *, sticky: bool = False, tenant_id: Optional[str] = None) -> None:
    await _get_default_store().append(
        session_id,
        text,
        sticky=sticky,
        tenant_id=tenant_id,
    )


async def take(session_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return await _get_default_store().take(session_id, tenant_id=tenant_id)
```

- [ ] **Step 2: Restore the remaining helper functions used by existing tests**

Add `get_recent(...)`, `clear_tenant(...)`, and `get_stats()` back on top of the new store so the module surface stays compatible.

```python
async def get_recent(
    max_age_seconds: int = _MAX_AGE_SECONDS,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    tenant = _normalize_tenant(tenant_id)
    pattern = f"{_KEY_PREFIX}:{tenant}:*"
    out: List[Dict[str, Any]] = []
    cursor = 0
    cutoff = time.time() - max_age_seconds
    client = _get_default_store()._client
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=pattern)
        for key in keys:
            await client.zremrangebyscore(key, "-inf", cutoff)
            rows = await client.zrange(key, 0, -1)
            out.extend(json.loads(row) for row in rows)
        if cursor == 0:
            break
    out.sort(key=lambda item: item["ts"])
    return RedisConsolePushStore._strip_ts(out)


async def clear_tenant(tenant_id: Optional[str] = None) -> None:
    await _get_default_store().clear_tenant(tenant_id=tenant_id)


async def get_stats() -> Dict[str, Any]:
    tenant = _normalize_tenant(None)
    _ = tenant
    client = _get_default_store()._client
    cursor = 0
    totals: Dict[str, int] = {}
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=f"{_KEY_PREFIX}:*")
        for key in keys:
            remainder = key[len(f"{_KEY_PREFIX}:") :]
            tenant_id, _session_id = remainder.rsplit(":", 1)
            totals[tenant_id] = totals.get(tenant_id, 0) + await client.zcard(key)
        if cursor == 0:
            break
    return {
        "tenant_count": len(totals),
        "tenants": totals,
    }
```

- [ ] **Step 3: Run the store suite**

Run:

```bash
venv/bin/python -m pytest tests/unit/app/test_console_push_store.py -v
```

Expected:

- PASS for cross-instance delivery, isolation, trimming, and missing Redis configuration

- [ ] **Step 4: Commit the store migration**

```bash
git add src/swe/app/console_push_store.py tests/unit/app/test_console_push_store.py
git commit -m "feat(app): move console push delivery to redis"
```

## Task 3: Make Push Polling Strictly Session-Scoped

**Files:**
- Modify: `src/swe/app/routers/console.py:204-223`
- Modify: `tests/unit/routers/test_console_tenant_isolation.py`

- [ ] **Step 1: Add the router happy-path assertion**

Extend `tests/unit/routers/test_console_tenant_isolation.py` so the stubbed store records calls.

```python
take_calls = []


async def _take(session_id, tenant_id=None):
    take_calls.append(
        {
            "session_id": session_id,
            "tenant_id": tenant_id,
        },
    )
    return [
        {
            "id": "msg-1",
            "text": f"{tenant_id}:{session_id}",
            "sticky": False,
        },
    ]


console_push_store.take = _take
```

Add this test:

```python
def test_push_messages_api_reads_only_requested_tenant_session():
    take_calls.clear()

    response = client.get(
        "/api/console/push-messages",
        params={"session_id": "session-a"},
        headers={"X-Tenant-Id": "tenant-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "messages": [
            {
                "id": "msg-1",
                "text": "tenant-a:session-a",
                "sticky": False,
            },
        ],
    }
    assert take_calls == [
        {
            "session_id": "session-a",
            "tenant_id": "tenant-a",
        },
    ]
```

- [ ] **Step 2: Run the router test before changing the route**

Run:

```bash
venv/bin/python -m pytest tests/unit/routers/test_console_tenant_isolation.py -v
```

Expected:

- FAIL because the route still allows missing `session_id` and still branches to `take_all(...)`

- [ ] **Step 3: Tighten the route**

Replace the route body in `src/swe/app/routers/console.py` with this:

```python
@router.get("/push-messages")
async def get_push_messages(
    request: Request,
    session_id: str | None = Query(None, description="Session id"),
):
    from ..console_push_store import take

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    tenant_id = getattr(request.state, "tenant_id", None)
    messages = await take(session_id, tenant_id=tenant_id)
    return {"messages": messages}
```

- [ ] **Step 4: Re-run and commit**

Run:

```bash
venv/bin/python -m pytest tests/unit/routers/test_console_tenant_isolation.py -v
git add src/swe/app/routers/console.py tests/unit/routers/test_console_tenant_isolation.py
git commit -m "fix(console): require session scoped push polling"
```

Expected:

- pytest PASS
- commit succeeds

## Task 4: Wire Proactive Writers And Verify The Change

**Files:**
- Modify: `src/swe/app/channels/console/channel.py:525-558`
- Create: `tests/unit/app/test_console_channel_push.py`
- Modify: `tests/unit/app/test_tenant_cron_manager_push.py`

- [ ] **Step 1: Add focused console-channel regression tests**

Create `tests/unit/app/test_console_channel_push.py`:

```python
# tests/unit/app/test_console_channel_push.py
# -*- coding: utf-8 -*-
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_send_passes_workspace_tenant_id(monkeypatch, tmp_path):
    from swe.app.channels.console.channel import ConsoleChannel

    calls = []

    async def fake_append(session_id, text, *, sticky=False, tenant_id=None):
        calls.append(
            {
                "session_id": session_id,
                "text": text,
                "sticky": sticky,
                "tenant_id": tenant_id,
            },
        )

    monkeypatch.setattr("swe.app.channels.console.channel.push_store_append", fake_append)

    channel = ConsoleChannel(
        process=lambda request: None,
        enabled=True,
        bot_prefix="",
        workspace_dir=Path(tmp_path),
    )
    channel.set_workspace(SimpleNamespace(tenant_id="tenant-a", workspace_dir=Path(tmp_path)))

    await channel.send("user-a", "hello", meta={"session_id": "session-a"})

    assert calls == [
        {
            "session_id": "session-a",
            "text": "hello",
            "sticky": False,
            "tenant_id": "tenant-a",
        },
    ]


@pytest.mark.asyncio
async def test_send_content_parts_passes_workspace_tenant_id(monkeypatch, tmp_path):
    from swe.app.channels.console.channel import ConsoleChannel, ContentType

    calls = []

    async def fake_append(session_id, text, *, sticky=False, tenant_id=None):
        calls.append(
            {
                "session_id": session_id,
                "text": text,
                "sticky": sticky,
                "tenant_id": tenant_id,
            },
        )

    monkeypatch.setattr("swe.app.channels.console.channel.push_store_append", fake_append)

    channel = ConsoleChannel(
        process=lambda request: None,
        enabled=True,
        bot_prefix="BOT:",
        workspace_dir=Path(tmp_path),
    )
    channel.set_workspace(SimpleNamespace(tenant_id="tenant-a", workspace_dir=Path(tmp_path)))

    await channel.send_content_parts(
        "user-a",
        [SimpleNamespace(type=ContentType.TEXT, text="hello")],
        meta={"session_id": "session-a"},
    )

    assert calls == [
        {
            "session_id": "session-a",
            "text": "BOT:  hello",
            "sticky": False,
            "tenant_id": "tenant-a",
        },
    ]
```

- [ ] **Step 2: Tighten the cron regression assertion**

In `tests/unit/app/test_tenant_cron_manager_push.py`, replace the final assertions with this exact block:

```python
    assert push_calls == [
        {
            "session_id": "session-a",
            "text": "❌ Cron job [tenant cron] failed: boom",
            "sticky": False,
            "tenant_id": "tenant-a",
        },
    ]
```

- [ ] **Step 3: Update proactive console writes to pass tenant_id**

Patch `src/swe/app/channels/console/channel.py` in both send paths:

```python
        sid = (meta or {}).get("session_id")
        tenant_id = getattr(self._workspace, "tenant_id", None)
        if sid and text.strip():
            await push_store_append(
                sid,
                text.strip(),
                tenant_id=tenant_id,
            )
```

```python
        sid = (meta or {}).get("session_id")
        tenant_id = getattr(self._workspace, "tenant_id", None)
        if sid:
            body = self._parts_to_text(parts, meta)
            if body.strip():
                await push_store_append(
                    sid,
                    body.strip(),
                    tenant_id=tenant_id,
                )
```

- [ ] **Step 4: Run the focused verification batch and commit**

Run:

```bash
venv/bin/python -m pytest tests/unit/app/test_console_channel_push.py tests/unit/app/test_tenant_cron_manager_push.py -v
venv/bin/python -m pytest tests/unit/app/test_console_push_store.py tests/unit/routers/test_console_tenant_isolation.py tests/unit/app/test_console_channel_push.py tests/unit/app/test_tenant_cron_manager_push.py -v
git add src/swe/app/channels/console/channel.py tests/unit/app/test_console_channel_push.py tests/unit/app/test_tenant_cron_manager_push.py
git commit -m "fix(console): scope proactive push delivery by tenant"
```

Expected:

- all listed pytest commands PASS
- the final commit succeeds with only the planned files staged

## Spec Coverage

- Shared across backend instances
  - Covered by `test_take_reads_message_written_by_separate_store_instance`
- Tenant and session isolation
  - Covered by the store isolation test and the router forwarding test
- Expiry and bounded retention
  - Covered by the Redis store implementation and trimming test
- Writers publish to shared state
  - Covered by the console channel regression tests and existing cron regression

Plan complete and saved to `docs/superpowers/plans/2026-04-08-redis-console-push-delivery.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
