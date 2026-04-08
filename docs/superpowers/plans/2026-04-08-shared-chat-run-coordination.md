# Shared Chat Run Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make interactive chat runs discoverable, stoppable, and status-readable from any backend instance by moving run ownership and cancellation semantics into Redis-backed shared coordination.

**Architecture:** Add a Redis-backed coordinator that owns run leases, heartbeats, and cancel flags, while `TaskTracker` keeps only pod-local queues and event buffers. `TaskTracker` must claim a shared lease before starting a run, refresh that lease while the run is alive, honor shared cancel signals, and expose shared owner/status lookups to the console and chat APIs.

**Tech Stack:** Python 3.10+, asyncio, FastAPI, `redis.asyncio`, pytest, pytest-asyncio

---

## File Structure

- Modify: `pyproject.toml` - add the Redis client dependency.
- Modify: `src/swe/constant.py` - add shared run coordination environment settings.
- Create: `src/swe/app/runner/shared_run_coordinator.py` - Redis lease/cancel primitives, owner identity helpers, and coordination errors.
- Modify: `src/swe/app/runner/task_tracker.py` - claim shared ownership before start, heartbeat while running, watch shared cancel signals, expose shared status/owner lookups, and close the coordinator client.
- Modify: `src/swe/app/workspace/workspace.py` - build a namespaced `TaskTracker` per workspace and close it during shutdown.
- Modify: `src/swe/app/routers/console.py` - use shared owner discovery for reconnect and shared cancellation for `/console/chat/stop`.
- Modify: `src/swe/app/runner/api.py` - use shared status for chat list/detail responses and return HTTP 503 when shared coordination is unavailable.
- Modify: `src/swe/app/channels/base.py` - handle “run already owned by another instance” without crashing queued channel consumers.
- Create: `tests/unit/runner/test_shared_run_coordinator.py` - lease, cancel, and expiry tests.
- Create: `tests/unit/runner/test_task_tracker_shared_coordination.py` - cross-instance status, stop, and duplicate-start tests.
- Create: `tests/unit/routers/test_console_chat_run_coordination.py` - reconnect and stop router tests.
- Create: `tests/unit/runner/test_chat_api_shared_status.py` - `/api/chats` and `/api/chats/{chat_id}` shared-status tests.

## Implementation Notes

- Shared state is authoritative for `running` vs `idle`. Do not silently fall back to local-only truth when Redis is unavailable.
- Use one Redis lease key and one Redis cancel key per `chat_id`, namespaced by workspace tenant/agent.
- Keep streaming execution local to the owner pod. A reconnect request served by a non-owner pod should discover the owner and return a deterministic conflict instead of pretending the run does not exist.
- Keep method names consistent across all tasks:
  - `RedisSharedRunCoordinator.start_run`
  - `RedisSharedRunCoordinator.refresh_run`
  - `RedisSharedRunCoordinator.get_run`
  - `RedisSharedRunCoordinator.request_cancel`
  - `RedisSharedRunCoordinator.clear_run`
  - `TaskTracker.get_owner`
  - `TaskTracker.get_status`
  - `TaskTracker.request_stop`
  - `TaskTracker.aclose`

---

### Task 1: Add Shared Redis Coordination Primitives

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/swe/constant.py`
- Create: `src/swe/app/runner/shared_run_coordinator.py`
- Test: `tests/unit/runner/test_shared_run_coordinator.py`

- [ ] **Step 1: Write the failing coordinator tests**

```python
# tests/unit/runner/test_shared_run_coordinator.py
import json
import time

import pytest


class FakeRedis:
    def __init__(self):
        self._values = {}
        self._expires_at = {}
        self.now = time.time()

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _purge(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and expires_at <= self.now:
            self._values.pop(key, None)
            self._expires_at.pop(key, None)

    async def ping(self):
        return True

    async def set(self, key, value, ex=None, nx=False):
        self._purge(key)
        if nx and key in self._values:
            return False
        self._values[key] = value
        if ex is not None:
            self._expires_at[key] = self.now + ex
        return True

    async def get(self, key):
        self._purge(key)
        return self._values.get(key)

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            self._purge(key)
            if key in self._values:
                deleted += 1
                self._values.pop(key, None)
                self._expires_at.pop(key, None)
        return deleted

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_start_run_persists_owner_and_status():
    from swe.app.runner.shared_run_coordinator import (
        RedisSharedRunCoordinator,
    )

    redis = FakeRedis()
    coordinator = RedisSharedRunCoordinator(
        redis_client=redis,
        namespace="tenant-a:agent-a",
        lease_ttl_seconds=30,
        cancel_ttl_seconds=60,
    )

    lease = await coordinator.start_run("chat-1", "pod-a:123")
    observed = await coordinator.get_run("chat-1")

    assert lease.owner_instance_id == "pod-a:123"
    assert observed is not None
    assert observed.owner_instance_id == "pod-a:123"
    assert observed.status == "running"
    assert observed.cancel_requested is False


@pytest.mark.asyncio
async def test_start_run_raises_when_another_owner_is_active():
    from swe.app.runner.shared_run_coordinator import (
        RedisSharedRunCoordinator,
        RunOwnedByAnotherInstanceError,
    )

    redis = FakeRedis()
    coordinator = RedisSharedRunCoordinator(
        redis_client=redis,
        namespace="tenant-a:agent-a",
        lease_ttl_seconds=30,
        cancel_ttl_seconds=60,
    )

    await coordinator.start_run("chat-1", "pod-a:123")

    with pytest.raises(RunOwnedByAnotherInstanceError) as exc:
        await coordinator.start_run("chat-1", "pod-b:456")

    assert exc.value.run_key == "chat-1"
    assert exc.value.owner_instance_id == "pod-a:123"


@pytest.mark.asyncio
async def test_request_cancel_marks_active_run():
    from swe.app.runner.shared_run_coordinator import (
        RedisSharedRunCoordinator,
    )

    redis = FakeRedis()
    coordinator = RedisSharedRunCoordinator(
        redis_client=redis,
        namespace="tenant-a:agent-a",
        lease_ttl_seconds=30,
        cancel_ttl_seconds=60,
    )

    await coordinator.start_run("chat-1", "pod-a:123")
    stopped = await coordinator.request_cancel("chat-1")
    observed = await coordinator.get_run("chat-1")

    assert stopped is True
    assert observed is not None
    assert observed.cancel_requested is True


@pytest.mark.asyncio
async def test_expired_run_reads_as_missing():
    from swe.app.runner.shared_run_coordinator import (
        RedisSharedRunCoordinator,
    )

    redis = FakeRedis()
    coordinator = RedisSharedRunCoordinator(
        redis_client=redis,
        namespace="tenant-a:agent-a",
        lease_ttl_seconds=5,
        cancel_ttl_seconds=60,
    )

    await coordinator.start_run("chat-1", "pod-a:123")
    redis.advance(6)

    assert await coordinator.get_run("chat-1") is None
```

- [ ] **Step 2: Run the tests and verify they fail before implementation**

Run:

```bash
venv/bin/python -m pytest tests/unit/runner/test_shared_run_coordinator.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'swe.app.runner.shared_run_coordinator'`.

- [ ] **Step 3: Add the dependency and coordination settings**

```toml
# pyproject.toml
dependencies = [
    "agentscope==1.0.18",
    "agentscope-runtime==1.1.3",
    "httpx>=0.27.0",
    "redis>=5.2.1",
    "packaging>=24.0",
```

```python
# src/swe/constant.py
SHARED_RUN_REDIS_URL = EnvVarLoader.get_str(
    "SWE_SHARED_RUN_REDIS_URL",
    "redis://127.0.0.1:6379/0",
)

SHARED_RUN_LEASE_TTL_SECONDS = EnvVarLoader.get_int(
    "SWE_SHARED_RUN_LEASE_TTL_SECONDS",
    30,
    min_value=5,
)

SHARED_RUN_HEARTBEAT_SECONDS = EnvVarLoader.get_float(
    "SWE_SHARED_RUN_HEARTBEAT_SECONDS",
    10.0,
    min_value=1.0,
)

SHARED_RUN_CANCEL_TTL_SECONDS = EnvVarLoader.get_int(
    "SWE_SHARED_RUN_CANCEL_TTL_SECONDS",
    60,
    min_value=5,
)
```

- [ ] **Step 4: Implement the Redis-backed coordinator**

```python
# src/swe/app/runner/shared_run_coordinator.py
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass

from redis import asyncio as redis_asyncio

from ...constant import (
    SHARED_RUN_CANCEL_TTL_SECONDS,
    SHARED_RUN_HEARTBEAT_SECONDS,
    SHARED_RUN_LEASE_TTL_SECONDS,
    SHARED_RUN_REDIS_URL,
)


@dataclass(frozen=True)
class SharedRunLease:
    run_key: str
    owner_instance_id: str
    status: str
    started_at: float
    heartbeat_at: float
    cancel_requested: bool = False


class SharedRunCoordinationError(RuntimeError):
    pass


class RunOwnedByAnotherInstanceError(SharedRunCoordinationError):
    def __init__(self, run_key: str, owner_instance_id: str):
        self.run_key = run_key
        self.owner_instance_id = owner_instance_id
        super().__init__(
            f"Run '{run_key}' is already owned by '{owner_instance_id}'",
        )


def build_runtime_instance_id() -> str:
    hostname = os.environ.get("HOSTNAME") or socket.gethostname()
    return f"{hostname}:{os.getpid()}"


class RedisSharedRunCoordinator:
    def __init__(
        self,
        *,
        namespace: str,
        redis_url: str = SHARED_RUN_REDIS_URL,
        lease_ttl_seconds: int = SHARED_RUN_LEASE_TTL_SECONDS,
        heartbeat_seconds: float = SHARED_RUN_HEARTBEAT_SECONDS,
        cancel_ttl_seconds: int = SHARED_RUN_CANCEL_TTL_SECONDS,
        redis_client=None,
    ) -> None:
        self.namespace = namespace
        self.lease_ttl_seconds = lease_ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.cancel_ttl_seconds = cancel_ttl_seconds
        self._redis = redis_client or redis_asyncio.from_url(
            redis_url,
            decode_responses=True,
        )

    async def _call(self, awaitable):
        try:
            return await awaitable
        except Exception as exc:
            raise SharedRunCoordinationError(
                "shared run coordination unavailable",
            ) from exc

    def _lease_key(self, run_key: str) -> str:
        return f"{self.namespace}:chat-run:{run_key}"

    def _cancel_key(self, run_key: str) -> str:
        return f"{self.namespace}:chat-run-cancel:{run_key}"

    async def start_run(
        self,
        run_key: str,
        owner_instance_id: str,
    ) -> SharedRunLease:
        await self._call(self._redis.ping())
        now = time.time()
        payload = json.dumps(
            {
                "run_key": run_key,
                "owner_instance_id": owner_instance_id,
                "status": "running",
                "started_at": now,
                "heartbeat_at": now,
            },
        )
        created = await self._call(
            self._redis.set(
                self._lease_key(run_key),
                payload,
                ex=self.lease_ttl_seconds,
                nx=True,
            ),
        )
        if not created:
            current = await self.get_run(run_key)
            if current is not None:
                raise RunOwnedByAnotherInstanceError(
                    run_key,
                    current.owner_instance_id,
                )
            return await self.start_run(run_key, owner_instance_id)
        return SharedRunLease(
            run_key=run_key,
            owner_instance_id=owner_instance_id,
            status="running",
            started_at=now,
            heartbeat_at=now,
        )

    async def refresh_run(
        self,
        run_key: str,
        owner_instance_id: str,
    ) -> SharedRunLease | None:
        current = await self.get_run(run_key)
        if current is None or current.owner_instance_id != owner_instance_id:
            return None
        payload = json.dumps(
            {
                "run_key": current.run_key,
                "owner_instance_id": current.owner_instance_id,
                "status": "running",
                "started_at": current.started_at,
                "heartbeat_at": time.time(),
            },
        )
        await self._call(
            self._redis.set(
                self._lease_key(run_key),
                payload,
                ex=self.lease_ttl_seconds,
            ),
        )
        return await self.get_run(run_key)

    async def get_run(self, run_key: str) -> SharedRunLease | None:
        raw = await self._call(self._redis.get(self._lease_key(run_key)))
        if raw is None:
            return None
        payload = json.loads(raw)
        cancel_requested = await self._call(
            self._redis.get(self._cancel_key(run_key)),
        )
        return SharedRunLease(
            run_key=payload["run_key"],
            owner_instance_id=payload["owner_instance_id"],
            status=payload["status"],
            started_at=payload["started_at"],
            heartbeat_at=payload["heartbeat_at"],
            cancel_requested=cancel_requested is not None,
        )

    async def request_cancel(self, run_key: str) -> bool:
        lease = await self.get_run(run_key)
        if lease is None:
            return False
        await self._call(
            self._redis.set(
                self._cancel_key(run_key),
                "1",
                ex=self.cancel_ttl_seconds,
            ),
        )
        return True

    async def clear_run(self, run_key: str, owner_instance_id: str) -> None:
        lease = await self.get_run(run_key)
        if lease is None or lease.owner_instance_id != owner_instance_id:
            return
        await self._call(
            self._redis.delete(
                self._lease_key(run_key),
                self._cancel_key(run_key),
            ),
        )

    async def close(self) -> None:
        await self._call(self._redis.aclose())
```

- [ ] **Step 5: Run the coordinator tests and confirm they pass**

Run:

```bash
venv/bin/python -m pytest tests/unit/runner/test_shared_run_coordinator.py -v
```

Expected: PASS with `4 passed`.

- [ ] **Step 6: Commit the primitive layer**

```bash
git add pyproject.toml src/swe/constant.py src/swe/app/runner/shared_run_coordinator.py tests/unit/runner/test_shared_run_coordinator.py
git commit -m "feat(runner): add shared chat run coordinator"
```

---

### Task 2: Wire Shared Ownership and Cancellation into TaskTracker

**Files:**
- Modify: `src/swe/app/runner/task_tracker.py`
- Modify: `src/swe/app/workspace/workspace.py`
- Test: `tests/unit/runner/test_task_tracker_shared_coordination.py`

- [ ] **Step 1: Write the failing TaskTracker tests**

```python
# tests/unit/runner/test_task_tracker_shared_coordination.py
import asyncio

import pytest

from swe.app.runner.shared_run_coordinator import (
    RedisSharedRunCoordinator,
    RunOwnedByAnotherInstanceError,
)

from tests.unit.runner.test_shared_run_coordinator import FakeRedis


async def _slow_stream(_payload):
    while True:
        await asyncio.sleep(0.05)
        yield "data: {\"chunk\": \"tick\"}\n\n"


@pytest.mark.asyncio
async def test_non_owner_reads_running_status_from_shared_lease():
    from swe.app.runner.task_tracker import TaskTracker

    redis = FakeRedis()
    coordinator = RedisSharedRunCoordinator(
        redis_client=redis,
        namespace="tenant-a:agent-a",
        lease_ttl_seconds=30,
        heartbeat_seconds=0.01,
        cancel_ttl_seconds=60,
    )
    owner = TaskTracker(
        coordinator=coordinator,
        instance_id="pod-a:123",
        heartbeat_seconds=0.01,
    )
    observer = TaskTracker(
        coordinator=coordinator,
        instance_id="pod-b:456",
        heartbeat_seconds=0.01,
    )

    queue, is_new = await owner.attach_or_start("chat-1", {}, _slow_stream)
    assert is_new is True
    assert queue is not None

    await asyncio.sleep(0.02)

    assert await observer.get_status("chat-1") == "running"
    assert await observer.get_owner("chat-1") == "pod-a:123"

    await owner.request_stop("chat-1")
    await asyncio.sleep(0.05)
    assert await observer.get_status("chat-1") == "idle"


@pytest.mark.asyncio
async def test_non_owner_stop_cancels_owner_run():
    from swe.app.runner.task_tracker import TaskTracker

    redis = FakeRedis()
    coordinator = RedisSharedRunCoordinator(
        redis_client=redis,
        namespace="tenant-a:agent-a",
        lease_ttl_seconds=30,
        heartbeat_seconds=0.01,
        cancel_ttl_seconds=60,
    )
    owner = TaskTracker(
        coordinator=coordinator,
        instance_id="pod-a:123",
        heartbeat_seconds=0.01,
    )
    observer = TaskTracker(
        coordinator=coordinator,
        instance_id="pod-b:456",
        heartbeat_seconds=0.01,
    )

    await owner.attach_or_start("chat-1", {}, _slow_stream)
    await asyncio.sleep(0.02)

    assert await observer.request_stop("chat-1") is True

    await asyncio.sleep(0.05)
    assert await owner.get_status("chat-1") == "idle"


@pytest.mark.asyncio
async def test_second_tracker_cannot_start_duplicate_run():
    from swe.app.runner.task_tracker import TaskTracker

    redis = FakeRedis()
    coordinator = RedisSharedRunCoordinator(
        redis_client=redis,
        namespace="tenant-a:agent-a",
        lease_ttl_seconds=30,
        heartbeat_seconds=0.01,
        cancel_ttl_seconds=60,
    )
    owner = TaskTracker(
        coordinator=coordinator,
        instance_id="pod-a:123",
        heartbeat_seconds=0.01,
    )
    observer = TaskTracker(
        coordinator=coordinator,
        instance_id="pod-b:456",
        heartbeat_seconds=0.01,
    )

    await owner.attach_or_start("chat-1", {}, _slow_stream)
    await asyncio.sleep(0.02)

    with pytest.raises(RunOwnedByAnotherInstanceError):
        await observer.attach_or_start("chat-1", {}, _slow_stream)
```

- [ ] **Step 2: Run the TaskTracker tests and verify they fail**

Run:

```bash
venv/bin/python -m pytest tests/unit/runner/test_task_tracker_shared_coordination.py -v
```

Expected: FAIL because `TaskTracker` does not yet accept `coordinator` / `instance_id`, and shared status still reads only local memory.

- [ ] **Step 3: Update `TaskTracker` to use shared leases and cancel signals**

```python
# src/swe/app/runner/task_tracker.py
from .shared_run_coordinator import (
    RedisSharedRunCoordinator,
    RunOwnedByAnotherInstanceError,
    build_runtime_instance_id,
)


@dataclass
class _RunState:
    task: asyncio.Future
    queues: list[asyncio.Queue] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)
    heartbeat_task: asyncio.Task | None = None
    cancel_watch_task: asyncio.Task | None = None


class TaskTracker:
    def __init__(
        self,
        *,
        coordinator=None,
        instance_id: str | None = None,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._coordinator = coordinator or RedisSharedRunCoordinator(
            namespace="default:default",
        )
        self._instance_id = instance_id or build_runtime_instance_id()
        self._heartbeat_seconds = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else self._coordinator.heartbeat_seconds
        )

    async def get_owner(self, run_key: str) -> str | None:
        lease = await self._coordinator.get_run(run_key)
        if lease is None:
            return None
        return lease.owner_instance_id

    async def get_status(self, run_key: str) -> str:
        lease = await self._coordinator.get_run(run_key)
        return "idle" if lease is None else "running"

    async def request_stop(self, run_key: str) -> bool:
        stopped = await self._coordinator.request_cancel(run_key)
        if not stopped:
            return False
        async with self._lock:
            state = self._runs.get(run_key)
            if state is not None and not state.task.done():
                state.task.cancel()
        return True

    async def attach_or_start(self, run_key: str, payload, stream_fn):
        async with self._lock:
            state = self._runs.get(run_key)
            if state is not None and not state.task.done():
                q = asyncio.Queue()
                for sse in state.buffer:
                    q.put_nowait(sse)
                state.queues.append(q)
                return q, False

        await self._coordinator.start_run(run_key, self._instance_id)

        async with self._lock:
            my_queue = asyncio.Queue()
            run = _RunState(
                task=asyncio.Future(),
                queues=[my_queue],
                buffer=[],
            )
            self._runs[run_key] = run

        async def _heartbeat_loop():
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                refreshed = await self._coordinator.refresh_run(
                    run_key,
                    self._instance_id,
                )
                if refreshed is None:
                    return

        async def _cancel_watch_loop():
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                lease = await self._coordinator.get_run(run_key)
                if lease is None:
                    return
                if lease.cancel_requested:
                    async with self._lock:
                        local_state = self._runs.get(run_key)
                        if local_state is not None and not local_state.task.done():
                            local_state.task.cancel()
                    return

        async def _producer():
            try:
                async for sse in stream_fn(payload):
                    async with self._lock:
                        run.buffer.append(sse)
                        for q in run.queues:
                            q.put_nowait(sse)
            except asyncio.CancelledError:
                pass
            finally:
                if run.heartbeat_task is not None:
                    run.heartbeat_task.cancel()
                if run.cancel_watch_task is not None:
                    run.cancel_watch_task.cancel()
                await self._coordinator.clear_run(run_key, self._instance_id)
                async with self._lock:
                    for q in run.queues:
                        q.put_nowait(_SENTINEL)
                    self._runs.pop(run_key, None)

        run.heartbeat_task = asyncio.create_task(_heartbeat_loop())
        run.cancel_watch_task = asyncio.create_task(_cancel_watch_loop())
        run.task = asyncio.create_task(_producer())
        return my_queue, True

    async def aclose(self) -> None:
        await self._coordinator.close()
```

- [ ] **Step 4: Namespace the tracker per workspace and close it on shutdown**

```python
# src/swe/app/workspace/workspace.py
from ..runner.shared_run_coordinator import RedisSharedRunCoordinator
from ..runner.task_tracker import TaskTracker


class Workspace:
    def __init__(self, agent_id: str, workspace_dir: str, tenant_id=None):
        self.agent_id = agent_id
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.tenant_id = tenant_id
        namespace = f"{tenant_id or 'default'}:{agent_id}"
        self._task_tracker = TaskTracker(
            coordinator=RedisSharedRunCoordinator(namespace=namespace),
        )

    async def stop(self, final: bool = True):
        if not self._started:
            return
        await self._service_manager.stop_all(final=final)
        await self._task_tracker.aclose()
        self._started = False
```

- [ ] **Step 5: Run the TaskTracker tests and confirm they pass**

Run:

```bash
venv/bin/python -m pytest tests/unit/runner/test_task_tracker_shared_coordination.py -v
```

Expected: PASS with `3 passed`.

- [ ] **Step 6: Commit the TaskTracker integration**

```bash
git add src/swe/app/runner/task_tracker.py src/swe/app/workspace/workspace.py tests/unit/runner/test_task_tracker_shared_coordination.py
git commit -m "feat(runner): track chat runs through shared leases"
```

---

### Task 3: Update Console Stop/Reconnect and Chat Status APIs

**Files:**
- Modify: `src/swe/app/routers/console.py`
- Modify: `src/swe/app/runner/api.py`
- Test: `tests/unit/routers/test_console_chat_run_coordination.py`
- Test: `tests/unit/runner/test_chat_api_shared_status.py`

- [ ] **Step 1: Write the failing router and API tests**

```python
# tests/unit/routers/test_console_chat_run_coordination.py
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.app.routers.console import router as console_router


class FakeTracker:
    def __init__(self):
        self.owner = "pod-a:123"
        self.stopped = False

    async def attach(self, _run_key):
        return None

    async def get_owner(self, _run_key):
        return self.owner

    async def request_stop(self, _run_key):
        self.stopped = True
        return True


class FakeChatManager:
    async def get_or_create_chat(self, *_args, **_kwargs):
        return SimpleNamespace(id="chat-1")


class FakeConsoleChannel:
    channel = "console"

    def resolve_session_id(self, sender_id, channel_meta):
        return channel_meta["session_id"]

    async def stream_one(self, payload):
        yield payload


class FakeChannelManager:
    async def get_channel(self, _name):
        return FakeConsoleChannel()


@pytest.fixture
def console_client(monkeypatch):
    tracker = FakeTracker()
    workspace = SimpleNamespace(
        task_tracker=tracker,
        chat_manager=FakeChatManager(),
        channel_manager=FakeChannelManager(),
    )

    async def _get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        "swe.app.routers.console.get_agent_for_request",
        _get_agent_for_request,
    )

    app = FastAPI()
    app.include_router(console_router, prefix="/api")
    return TestClient(app)


def test_reconnect_returns_409_when_run_is_owned_elsewhere(console_client):
    response = console_client.post(
        "/api/console/chat",
        json={
            "channel": "console",
            "user_id": "alice",
            "session_id": "console:alice",
            "reconnect": True,
            "input": [{"content": [{"text": "hello"}]}],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "chat_running_on_another_instance"
    )
    assert response.json()["detail"]["owner_instance_id"] == "pod-a:123"


def test_stop_returns_shared_cancellation_result(console_client):
    response = console_client.post(
        "/api/console/chat/stop",
        params={"chat_id": "chat-1"},
    )

    assert response.status_code == 200
    assert response.json() == {"stopped": True}
```

```python
# tests/unit/runner/test_chat_api_shared_status.py
from types import SimpleNamespace

import pytest

from swe.app.runner.api import get_chat, list_chats
from swe.app.runner.models import ChatSpec


class FakeChatManager:
    def __init__(self, chat_spec):
        self.chat_spec = chat_spec

    async def list_chats(self, **_kwargs):
        return [self.chat_spec]

    async def get_chat(self, chat_id):
        return self.chat_spec if chat_id == self.chat_spec.id else None


class FakeSession:
    async def get_session_state_dict(self, *_args, **_kwargs):
        return {}


class FakeTracker:
    async def get_status(self, _chat_id):
        return "running"


@pytest.mark.asyncio
async def test_list_chats_reads_shared_running_status():
    chat = ChatSpec(
        id="chat-1",
        name="Test",
        session_id="console:alice",
        user_id="alice",
        channel="console",
    )
    workspace = SimpleNamespace(task_tracker=FakeTracker())

    result = await list_chats(
        mgr=FakeChatManager(chat),
        workspace=workspace,
    )

    assert result[0].status == "running"


@pytest.mark.asyncio
async def test_get_chat_reads_shared_running_status():
    chat = ChatSpec(
        id="chat-1",
        name="Test",
        session_id="console:alice",
        user_id="alice",
        channel="console",
    )
    workspace = SimpleNamespace(task_tracker=FakeTracker())
    session = FakeSession()

    result = await get_chat(
        chat_id="chat-1",
        mgr=FakeChatManager(chat),
        session=session,
        workspace=workspace,
    )

    assert result.status == "running"
```

- [ ] **Step 2: Run the router/API tests and verify they fail**

Run:

```bash
venv/bin/python -m pytest tests/unit/routers/test_console_chat_run_coordination.py tests/unit/runner/test_chat_api_shared_status.py -v
```

Expected: FAIL because reconnect currently returns `404`, and no router/API path distinguishes remote ownership from a truly idle run.

- [ ] **Step 3: Update the console router for shared reconnect discovery and deterministic stop**

```python
# src/swe/app/routers/console.py
    if is_reconnect:
        queue = await tracker.attach(chat.id)
        if queue is None:
            owner_instance_id = await tracker.get_owner(chat.id)
            if owner_instance_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "chat_running_on_another_instance",
                        "chat_id": chat.id,
                        "owner_instance_id": owner_instance_id,
                    },
                )
            raise HTTPException(
                status_code=404,
                detail="No running chat for this session",
            )
    else:
        queue, _ = await tracker.attach_or_start(
            chat.id,
            native_payload,
            console_channel.stream_one,
        )


@router.post("/chat/stop", status_code=200, summary="Stop running console chat")
async def post_console_chat_stop(request: Request, chat_id: str = Query(...)):
    workspace = await get_agent_for_request(request)
    stopped = await workspace.task_tracker.request_stop(chat_id)
    return {"stopped": stopped}
```

- [ ] **Step 4: Update chat list/detail APIs to treat shared coordination failures as explicit server errors**

```python
# src/swe/app/runner/api.py
from .shared_run_coordinator import SharedRunCoordinationError


async def _shared_chat_status(workspace, chat_id: str) -> str:
    try:
        return await workspace.task_tracker.get_status(chat_id)
    except SharedRunCoordinationError as exc:
        raise HTTPException(
            status_code=503,
            detail="shared run coordination unavailable",
        ) from exc


@router.get("", response_model=list[ChatSpec])
async def list_chats(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    chats = await mgr.list_chats(user_id=user_id, channel=channel)
    result = []
    for spec in chats:
        status = await _shared_chat_status(workspace, spec.id)
        result.append(spec.model_copy(update={"status": status}))
    return result


@router.get("/{chat_id}", response_model=ChatHistory)
async def get_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    status = await _shared_chat_status(workspace, chat_id)
    state = await session.get_session_state_dict(
        chat_spec.session_id,
        chat_spec.user_id,
    )
    if not state:
        return ChatHistory(messages=[], status=status)
    memory_state = state.get("agent", {}).get("memory", {})
    memory = InMemoryMemory()
    memory.load_state_dict(memory_state, strict=False)
    memories = await memory.get_memory(prepend_summary=False)
    messages = agentscope_msg_to_message(memories)
    return ChatHistory(messages=messages, status=status)
```

- [ ] **Step 5: Run the router/API tests and confirm they pass**

Run:

```bash
venv/bin/python -m pytest tests/unit/routers/test_console_chat_run_coordination.py tests/unit/runner/test_chat_api_shared_status.py -v
```

Expected: PASS with `4 passed`.

- [ ] **Step 6: Commit the API wiring**

```bash
git add src/swe/app/routers/console.py src/swe/app/runner/api.py tests/unit/routers/test_console_chat_run_coordination.py tests/unit/runner/test_chat_api_shared_status.py
git commit -m "feat(api): use shared chat run coordination"
```

---

### Task 4: Keep Channel Consumers Compatible and Run the Full Regression Slice

**Files:**
- Modify: `src/swe/app/channels/base.py`
- Test: `tests/unit/channels/test_base_channel_shared_run_coordination.py`

- [ ] **Step 1: Write the failing BaseChannel regression test**

```python
# tests/unit/channels/test_base_channel_shared_run_coordination.py
import pytest

from swe.app.runner.shared_run_coordinator import (
    RunOwnedByAnotherInstanceError,
)


class DummyChannel:
    channel = "console"

    async def _consume_with_tracker(self, request, payload):
        del request
        del payload


@pytest.mark.asyncio
async def test_remote_owner_conflict_is_logged_and_ignored():
    from swe.app.channels.base import BaseChannel

    async def _get_or_create_chat(*_args, **_kwargs):
        return type("Chat", (), {"id": "chat-1"})()

    async def _attach_or_start(*_args, **_kwargs):
        raise RunOwnedByAnotherInstanceError(
            "chat-1",
            "pod-a:123",
        )

    channel = BaseChannel.__new__(BaseChannel)
    channel.channel = "console"
    channel._workspace = type(
        "Workspace",
        (),
        {
            "chat_manager": type(
                "ChatManager",
                (),
                {
                    "get_or_create_chat": staticmethod(_get_or_create_chat),
                },
            )(),
            "task_tracker": type(
                "Tracker",
                (),
                {
                    "attach_or_start": staticmethod(_attach_or_start),
                },
            )(),
        },
    )()
    channel._extract_chat_name = lambda _payload: "Test"

    request = type(
        "Request",
        (),
        {"session_id": "console:alice", "user_id": "alice", "channel": "console"},
    )()

    await BaseChannel._consume_with_tracker(channel, request, {"content_parts": []})
```

- [ ] **Step 2: Run the BaseChannel test and verify it fails**

Run:

```bash
venv/bin/python -m pytest tests/unit/channels/test_base_channel_shared_run_coordination.py -v
```

Expected: FAIL because `_consume_with_tracker` currently lets the ownership-conflict exception escape.

- [ ] **Step 3: Catch shared ownership conflicts in `BaseChannel`**

```python
# src/swe/app/channels/base.py
from ..runner.shared_run_coordinator import RunOwnedByAnotherInstanceError


    async def _consume_with_tracker(self, request: "AgentRequest", payload: Any) -> None:
        ...
        try:
            queue, is_new = await self._workspace.task_tracker.attach_or_start(
                chat.id,
                payload,
                self._stream_with_tracker,
            )
        except RunOwnedByAnotherInstanceError as exc:
            logger.warning(
                "Message ignored because run is active on another instance: "
                "chat_id=%s owner_instance_id=%s",
                chat.id,
                exc.owner_instance_id,
            )
            return
```

- [ ] **Step 4: Run the focused regression slice**

Run:

```bash
venv/bin/python -m pytest tests/unit/runner/test_shared_run_coordinator.py tests/unit/runner/test_task_tracker_shared_coordination.py tests/unit/runner/test_chat_api_shared_status.py tests/unit/routers/test_console_chat_run_coordination.py tests/unit/channels/test_base_channel_shared_run_coordination.py -v
```

Expected: PASS with all targeted shared-coordination tests green.

- [ ] **Step 5: Commit the channel compatibility fix**

```bash
git add src/swe/app/channels/base.py tests/unit/channels/test_base_channel_shared_run_coordination.py
git commit -m "fix(channels): ignore remote chat run ownership conflicts"
```

---

## Self-Review

- Spec coverage:
  - Shared ownership across backend instances: Task 1 and Task 2.
  - Stop requests from any backend instance: Task 2 and Task 3.
  - Shared running-status APIs: Task 3.
  - Reconnect discovery from non-owner instance: Task 3.
  - Stale ownership expiry: Task 1.
- Placeholder scan:
  - No placeholder markers remain in the plan body.
- Type consistency:
  - Coordinator method names stay consistent: `start_run`, `refresh_run`, `get_run`, `request_cancel`, `clear_run`, `close`.
  - Tracker method names stay consistent: `get_owner`, `get_status`, `request_stop`, `attach_or_start`, `aclose`.

Plan complete and saved to `docs/superpowers/plans/2026-04-08-shared-chat-run-coordination.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
