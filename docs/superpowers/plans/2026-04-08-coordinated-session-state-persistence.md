# Coordinated Session State Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace file-only session persistence with authoritative checkpoint metadata so cross-instance session writes do not silently overwrite newer state.

**Architecture:** Keep raw session payloads as immutable JSON blobs under the workspace `sessions/` directory, but move "which checkpoint is current" facts into a MySQL-backed compare-and-set repository keyed by `tenant_id`, `user_id`, and `session_id`. Preserve the public `SafeJSONSession` method surface so runner, command dispatch, and chat APIs keep calling the same methods while the implementation switches to metadata-driven load/save and lazy legacy import.

**Tech Stack:** Python, aiofiles, aiomysql, FastAPI, pytest, pytest-asyncio

---

## File Structure / Responsibility Map

- `pyproject.toml`
  - Add the async MySQL client dependency used by the new checkpoint repository.
- `src/swe/app/runner/session_models.py`
  - Define `SessionCheckpointKey`, `SessionCheckpointRecord`, and `SessionCheckpointConflictError`.
- `src/swe/app/runner/repo/session_checkpoint_base.py`
  - Define the repository contract for authoritative checkpoint metadata.
- `src/swe/app/runner/repo/session_checkpoint_mysql.py`
  - Implement MySQL-backed checkpoint reads and compare-and-set writes.
- `src/swe/app/runner/repo/__init__.py`
  - Export the new repository types.
- `src/swe/app/runner/session.py`
  - Refactor `SafeJSONSession` to use versioned checkpoint blobs plus authoritative metadata, while keeping legacy filename sanitization and method names intact.
- `src/swe/app/runner/runner.py`
  - Accept `tenant_id` and an injected checkpoint repository, then construct `SafeJSONSession` with those collaborators.
- `src/swe/app/workspace/workspace.py`
  - Register a dedicated session-persistence service before `runner_start` and pass `tenant_id` into `AgentRunner`.
- `src/swe/app/workspace/service_factories.py`
  - Build the checkpoint repository from `SWE_MYSQL_DSN`, inject it into the runner, and close it on shutdown.
- `tests/unit/app/runner/test_session_checkpoint_contract.py`
  - Verify the new checkpoint models and conflict error contract.
- `tests/unit/app/runner/test_session_checkpoint_mysql.py`
  - Verify MySQL compare-and-set semantics with stubbed DB calls.
- `tests/unit/app/runner/test_session.py`
  - Verify metadata-driven load/save, stale-writer conflicts, blob cleanup, and legacy import.
- `tests/unit/app/runner/test_runner_session_wiring.py`
  - Verify workspace and runner dependency wiring.

---

### Task 1: Define the checkpoint metadata contract

**Files:**
- Create: `src/swe/app/runner/session_models.py`
- Create: `src/swe/app/runner/repo/session_checkpoint_base.py`
- Modify: `src/swe/app/runner/repo/__init__.py`
- Test: `tests/unit/app/runner/test_session_checkpoint_contract.py`

- [ ] **Step 1: Write the failing contract test**

```python
# tests/unit/app/runner/test_session_checkpoint_contract.py
from swe.app.runner.session_models import (
    SessionCheckpointConflictError,
    SessionCheckpointKey,
    SessionCheckpointRecord,
)


def test_session_checkpoint_conflict_error_keeps_identity_and_versions():
    key = SessionCheckpointKey(
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="console:user-1",
    )

    error = SessionCheckpointConflictError(
        key=key,
        expected_version=2,
        actual_version=3,
    )

    assert error.key == key
    assert error.expected_version == 2
    assert error.actual_version == 3
    assert "tenant-a" in str(error)
    assert "expected=2" in str(error)
    assert "actual=3" in str(error)


def test_session_checkpoint_record_is_immutable():
    record = SessionCheckpointRecord(
        key=SessionCheckpointKey(
            tenant_id="tenant-a",
            user_id="user-1",
            session_id="console:user-1",
        ),
        version=4,
        blob_path="/tmp/checkpoints/user-1_console--user-1.v4.json",
        payload_sha256="a" * 64,
    )

    assert record.version == 4
    assert record.blob_path.endswith(".v4.json")
    assert len(record.payload_sha256) == 64
```

- [ ] **Step 2: Run the contract test and confirm it fails**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session_checkpoint_contract.py -v
```

Expected:
- FAIL with `ModuleNotFoundError` for `swe.app.runner.session_models`

- [ ] **Step 3: Add the new checkpoint models and repository interface**

```python
# src/swe/app/runner/session_models.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionCheckpointKey:
    tenant_id: str
    user_id: str
    session_id: str


@dataclass(frozen=True)
class SessionCheckpointRecord:
    key: SessionCheckpointKey
    version: int
    blob_path: str
    payload_sha256: str


class SessionCheckpointConflictError(RuntimeError):
    def __init__(
        self,
        *,
        key: SessionCheckpointKey,
        expected_version: int | None,
        actual_version: int | None,
    ) -> None:
        self.key = key
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            "Session checkpoint conflict for "
            f"{key.tenant_id}/{key.user_id}/{key.session_id}: "
            f"expected={expected_version}, actual={actual_version}"
        )
```

```python
# src/swe/app/runner/repo/session_checkpoint_base.py
from __future__ import annotations

from abc import ABC, abstractmethod

from ..session_models import SessionCheckpointKey, SessionCheckpointRecord


class BaseSessionCheckpointRepository(ABC):
    @abstractmethod
    async def get_latest(
        self,
        key: SessionCheckpointKey,
    ) -> SessionCheckpointRecord | None:
        raise NotImplementedError

    @abstractmethod
    async def write_checkpoint(
        self,
        *,
        key: SessionCheckpointKey,
        expected_version: int | None,
        blob_path: str,
        payload_sha256: str,
    ) -> SessionCheckpointRecord:
        raise NotImplementedError

    async def close(self) -> None:
        return None
```

- [ ] **Step 4: Export the new repository symbols**

```python
# src/swe/app/runner/repo/__init__.py
from .base import BaseChatRepository
from .json_repo import JsonChatRepository
from .session_checkpoint_base import BaseSessionCheckpointRepository

__all__ = [
    "BaseChatRepository",
    "JsonChatRepository",
    "BaseSessionCheckpointRepository",
]
```

- [ ] **Step 5: Run the contract test again**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session_checkpoint_contract.py -v
```

Expected:
- PASS for both checkpoint contract tests

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/swe/app/runner/session_models.py src/swe/app/runner/repo/session_checkpoint_base.py src/swe/app/runner/repo/__init__.py tests/unit/app/runner/test_session_checkpoint_contract.py
git commit -m "feat(session): add checkpoint metadata contract"
```

---

### Task 2: Add the MySQL-backed authoritative checkpoint repository

**Files:**
- Modify: `pyproject.toml`
- Create: `src/swe/app/runner/repo/session_checkpoint_mysql.py`
- Modify: `src/swe/app/runner/repo/__init__.py`
- Test: `tests/unit/app/runner/test_session_checkpoint_mysql.py`

- [ ] **Step 1: Write the failing repository tests**

```python
# tests/unit/app/runner/test_session_checkpoint_mysql.py
import pytest

from swe.app.runner.session_models import SessionCheckpointKey
from swe.app.runner.repo.session_checkpoint_mysql import (
    MySQLSessionCheckpointRepository,
)


@pytest.mark.asyncio
async def test_first_write_returns_version_one(monkeypatch):
    repo = MySQLSessionCheckpointRepository(pool=object())
    key = SessionCheckpointKey(
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="console:user-1",
    )

    monkeypatch.setattr(repo, "_fetch_latest_row", lambda key: None)
    monkeypatch.setattr(repo, "_insert_first_row", lambda **kwargs: 1)

    record = await repo.write_checkpoint(
        key=key,
        expected_version=None,
        blob_path="/tmp/blob.v1.json",
        payload_sha256="b" * 64,
    )

    assert record.version == 1


@pytest.mark.asyncio
async def test_stale_write_raises_conflict(monkeypatch):
    repo = MySQLSessionCheckpointRepository(pool=object())
    key = SessionCheckpointKey(
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="console:user-1",
    )

    monkeypatch.setattr(
        repo,
        "_fetch_latest_row",
        lambda key: {
            "tenant_id": "tenant-a",
            "user_id": "user-1",
            "session_id": "console:user-1",
            "version": 3,
            "blob_path": "/tmp/blob.v3.json",
            "payload_sha256": "c" * 64,
        },
    )

    with pytest.raises(Exception):
        await repo.write_checkpoint(
            key=key,
            expected_version=2,
            blob_path="/tmp/blob.v4.json",
            payload_sha256="d" * 64,
        )
```

- [ ] **Step 2: Run the repository tests and confirm they fail**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session_checkpoint_mysql.py -v
```

Expected:
- FAIL with `ModuleNotFoundError` for `session_checkpoint_mysql`

- [ ] **Step 3: Add the async MySQL dependency**

```toml
# pyproject.toml
dependencies = [
    # existing dependencies...
    "aiofiles>=24.1.0",
    "aiomysql>=0.2.0",
    "paho-mqtt>=2.0.0",
]
```

- [ ] **Step 4: Implement the compare-and-set repository**

```python
# src/swe/app/runner/repo/session_checkpoint_mysql.py
from __future__ import annotations

from ..session_models import (
    SessionCheckpointConflictError,
    SessionCheckpointKey,
    SessionCheckpointRecord,
)
from .session_checkpoint_base import BaseSessionCheckpointRepository


class MySQLSessionCheckpointRepository(BaseSessionCheckpointRepository):
    def __init__(self, pool) -> None:
        self._pool = pool

    async def get_latest(
        self,
        key: SessionCheckpointKey,
    ) -> SessionCheckpointRecord | None:
        row = await self._fetch_latest_row(key)
        if row is None:
            return None
        return SessionCheckpointRecord(
            key=key,
            version=row["version"],
            blob_path=row["blob_path"],
            payload_sha256=row["payload_sha256"],
        )

    async def write_checkpoint(
        self,
        *,
        key: SessionCheckpointKey,
        expected_version: int | None,
        blob_path: str,
        payload_sha256: str,
    ) -> SessionCheckpointRecord:
        current = await self.get_latest(key)

        if current is None:
            if expected_version not in (None, 0):
                raise SessionCheckpointConflictError(
                    key=key,
                    expected_version=expected_version,
                    actual_version=None,
                )
            version = await self._insert_first_row(
                key=key,
                blob_path=blob_path,
                payload_sha256=payload_sha256,
            )
            return SessionCheckpointRecord(
                key=key,
                version=version,
                blob_path=blob_path,
                payload_sha256=payload_sha256,
            )

        if current.version != expected_version:
            raise SessionCheckpointConflictError(
                key=key,
                expected_version=expected_version,
                actual_version=current.version,
            )

        version = await self._update_existing_row(
            key=key,
            previous_version=current.version,
            blob_path=blob_path,
            payload_sha256=payload_sha256,
        )
        return SessionCheckpointRecord(
            key=key,
            version=version,
            blob_path=blob_path,
            payload_sha256=payload_sha256,
        )
```

- [ ] **Step 5: Export the MySQL repository**

```python
# src/swe/app/runner/repo/__init__.py
from .session_checkpoint_base import BaseSessionCheckpointRepository
from .session_checkpoint_mysql import MySQLSessionCheckpointRepository

__all__ = [
    "BaseChatRepository",
    "JsonChatRepository",
    "BaseSessionCheckpointRepository",
    "MySQLSessionCheckpointRepository",
]
```

- [ ] **Step 6: Run the repository tests again**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session_checkpoint_mysql.py -v
```

Expected:
- PASS for initial write and stale-write conflict behavior

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/swe/app/runner/repo/session_checkpoint_mysql.py src/swe/app/runner/repo/__init__.py tests/unit/app/runner/test_session_checkpoint_mysql.py
git commit -m "feat(session): add mysql checkpoint repository"
```

---

### Task 3: Refactor `SafeJSONSession` to use authoritative checkpoint metadata

**Files:**
- Modify: `src/swe/app/runner/session.py`
- Test: `tests/unit/app/runner/test_session.py`

- [ ] **Step 1: Write the failing coordinated-session tests**

```python
# tests/unit/app/runner/test_session.py
import json
import pytest

from swe.app.runner.session import SafeJSONSession
from swe.app.runner.session_models import (
    SessionCheckpointConflictError,
    SessionCheckpointKey,
    SessionCheckpointRecord,
)
from swe.app.runner.repo.session_checkpoint_base import (
    BaseSessionCheckpointRepository,
)


class InMemoryCheckpointRepo(BaseSessionCheckpointRepository):
    def __init__(self) -> None:
        self._records = {}

    async def get_latest(self, key):
        return self._records.get((key.tenant_id, key.user_id, key.session_id))

    async def write_checkpoint(
        self,
        *,
        key,
        expected_version,
        blob_path,
        payload_sha256,
    ):
        current = await self.get_latest(key)
        if current is None:
            if expected_version not in (None, 0):
                raise SessionCheckpointConflictError(
                    key=key,
                    expected_version=expected_version,
                    actual_version=None,
                )
            record = SessionCheckpointRecord(
                key=key,
                version=1,
                blob_path=blob_path,
                payload_sha256=payload_sha256,
            )
            self._records[(key.tenant_id, key.user_id, key.session_id)] = record
            return record
        if current.version != expected_version:
            raise SessionCheckpointConflictError(
                key=key,
                expected_version=expected_version,
                actual_version=current.version,
            )
        record = SessionCheckpointRecord(
            key=key,
            version=current.version + 1,
            blob_path=blob_path,
            payload_sha256=payload_sha256,
        )
        self._records[(key.tenant_id, key.user_id, key.session_id)] = record
        return record


@pytest.mark.asyncio
async def test_get_session_state_dict_prefers_metadata_selected_blob(tmp_path):
    repo = InMemoryCheckpointRepo()
    session = SafeJSONSession(
        save_dir=str(tmp_path),
        tenant_id="tenant-a",
        checkpoint_repo=repo,
    )

    await session.update_session_state(
        session_id="console:user-1",
        user_id="user-1",
        key="agent.memory.turn",
        value=2,
    )

    state = await session.get_session_state_dict(
        session_id="console:user-1",
        user_id="user-1",
    )

    assert state["agent"]["memory"]["turn"] == 2


@pytest.mark.asyncio
async def test_stale_writer_raises_conflict(tmp_path):
    repo = InMemoryCheckpointRepo()
    first = SafeJSONSession(str(tmp_path), tenant_id="tenant-a", checkpoint_repo=repo)
    second = SafeJSONSession(str(tmp_path), tenant_id="tenant-a", checkpoint_repo=repo)

    await first.get_session_state_dict("console:user-1", "user-1")
    await second.get_session_state_dict("console:user-1", "user-1")
    await first.update_session_state(
        session_id="console:user-1",
        user_id="user-1",
        key="agent.memory.turn",
        value=1,
    )

    with pytest.raises(SessionCheckpointConflictError):
        await second.update_session_state(
            session_id="console:user-1",
            user_id="user-1",
            key="agent.memory.turn",
            value=2,
        )


@pytest.mark.asyncio
async def test_legacy_file_is_imported_when_metadata_is_missing(tmp_path):
    repo = InMemoryCheckpointRepo()
    session = SafeJSONSession(
        save_dir=str(tmp_path),
        tenant_id="tenant-a",
        checkpoint_repo=repo,
    )

    legacy_path = tmp_path / "user-1_console--user-1.json"
    legacy_path.write_text(
        json.dumps({"agent": {"memory": {"turn": 7}}}),
        encoding="utf-8",
    )

    state = await session.get_session_state_dict(
        session_id="console:user-1",
        user_id="user-1",
    )

    assert state["agent"]["memory"]["turn"] == 7
    latest = await repo.get_latest(
        SessionCheckpointKey(
            tenant_id="tenant-a",
            user_id="user-1",
            session_id="console:user-1",
        )
    )
    assert latest is not None
    assert latest.version == 1
```

- [ ] **Step 2: Run the session tests and confirm they fail**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session.py -v
```

Expected:
- FAIL because `SafeJSONSession` does not accept `tenant_id` or `checkpoint_repo`

- [ ] **Step 3: Refactor `SafeJSONSession` without changing its public method names**

```python
# src/swe/app/runner/session.py
class SafeJSONSession(SessionBase):
    def __init__(
        self,
        save_dir: str = "./",
        *,
        tenant_id: str = "default",
        checkpoint_repo=None,
    ) -> None:
        self.save_dir = save_dir
        self.tenant_id = tenant_id
        self._checkpoint_repo = checkpoint_repo
        self._version_cache: dict[tuple[str, str], int] = {}

    def _checkpoint_key(self, session_id: str, user_id: str):
        return SessionCheckpointKey(
            tenant_id=self.tenant_id,
            user_id=user_id,
            session_id=session_id,
        )

    def _get_checkpoint_blob_path(
        self,
        session_id: str,
        user_id: str,
        version: int,
    ) -> str:
        safe_sid = sanitize_filename(session_id)
        safe_uid = sanitize_filename(user_id) if user_id else ""
        filename = f"{safe_uid}_{safe_sid}.v{version}.json"
        return os.path.join(self.save_dir, filename)
```

```python
    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
        allow_not_exist: bool = True,
    ) -> dict:
        key = self._checkpoint_key(session_id, user_id)
        if self._checkpoint_repo is not None:
            record = await self._checkpoint_repo.get_latest(key)
            if record is None:
                record = await self._import_legacy_file_if_present(
                    session_id=session_id,
                    user_id=user_id,
                )
            if record is not None:
                self._version_cache[(session_id, user_id)] = record.version
                return await self._read_state_file(record.blob_path)
        return await self._read_legacy_state_dict(
            session_id=session_id,
            user_id=user_id,
            allow_not_exist=allow_not_exist,
        )
```

```python
    async def _write_authoritative_state(
        self,
        *,
        session_id: str,
        user_id: str,
        state_dicts: dict,
    ) -> None:
        expected_version = self._version_cache.get((session_id, user_id))
        next_version = 1 if expected_version is None else expected_version + 1
        blob_path = self._get_checkpoint_blob_path(
            session_id,
            user_id,
            next_version,
        )
        payload = json.dumps(state_dicts, ensure_ascii=False)
        with open(blob_path, "w", encoding="utf-8") as f:
            f.write(payload)
        payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        try:
            record = await self._checkpoint_repo.write_checkpoint(
                key=self._checkpoint_key(session_id, user_id),
                expected_version=expected_version,
                blob_path=blob_path,
                payload_sha256=payload_sha256,
            )
        except Exception:
            if os.path.exists(blob_path):
                os.remove(blob_path)
            raise
        self._version_cache[(session_id, user_id)] = record.version
```

- [ ] **Step 4: Keep the legacy compatibility path explicit**

```python
    async def _import_legacy_file_if_present(
        self,
        *,
        session_id: str,
        user_id: str,
    ):
        legacy_path = self._get_save_path(session_id, user_id=user_id)
        if not os.path.exists(legacy_path):
            return None
        state_dicts = await self._read_state_file(legacy_path)
        await self._write_authoritative_state(
            session_id=session_id,
            user_id=user_id,
            state_dicts=state_dicts,
        )
        return await self._checkpoint_repo.get_latest(
            self._checkpoint_key(session_id, user_id)
        )
```

- [ ] **Step 5: Run the coordinated-session tests again**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session.py -v
```

Expected:
- PASS for metadata-driven read, stale-writer rejection, and legacy import

- [ ] **Step 6: Commit**

```bash
git add src/swe/app/runner/session.py tests/unit/app/runner/test_session.py
git commit -m "feat(session): coordinate session state through checkpoint metadata"
```

---

### Task 4: Wire the repository into workspace startup and runner construction

**Files:**
- Modify: `src/swe/app/runner/runner.py`
- Modify: `src/swe/app/workspace/workspace.py`
- Modify: `src/swe/app/workspace/service_factories.py`
- Test: `tests/unit/app/runner/test_runner_session_wiring.py`

- [ ] **Step 1: Write the failing wiring tests**

```python
# tests/unit/app/runner/test_runner_session_wiring.py
from unittest.mock import Mock

import pytest

from swe.app.runner.runner import AgentRunner
from swe.app.workspace.service_factories import (
    create_session_persistence_service,
)


@pytest.mark.asyncio
async def test_runner_init_handler_uses_injected_checkpoint_repo(tmp_path):
    runner = AgentRunner(
        agent_id="default",
        tenant_id="tenant-a",
        workspace_dir=tmp_path,
        task_tracker=Mock(),
    )
    repo = Mock()
    runner.set_session_checkpoint_repository(repo)

    await runner.init_handler()

    assert runner.session.tenant_id == "tenant-a"
    assert runner.session._checkpoint_repo is repo


@pytest.mark.asyncio
async def test_service_factory_injects_repo_when_mysql_dsn_is_present(
    monkeypatch,
    tmp_path,
):
    ws = Mock()
    ws.agent_id = "default"
    ws.tenant_id = "tenant-a"
    ws.workspace_dir = tmp_path
    ws._service_manager = Mock()
    ws._service_manager.services = {"runner": Mock()}

    monkeypatch.setenv("SWE_MYSQL_DSN", "mysql://user:pass@127.0.0.1:3306/swe")

    await create_session_persistence_service(ws, None)

    ws._service_manager.services["runner"].set_session_checkpoint_repository.assert_called_once()
```

- [ ] **Step 2: Run the wiring tests and confirm they fail**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_runner_session_wiring.py -v
```

Expected:
- FAIL because `AgentRunner` does not accept `tenant_id`
- FAIL because `create_session_persistence_service` does not exist

- [ ] **Step 3: Extend `AgentRunner` for injected coordinated persistence**

```python
# src/swe/app/runner/runner.py
class AgentRunner(Runner):
    def __init__(
        self,
        agent_id: str = "default",
        tenant_id: str = "default",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
    ) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.workspace_dir = workspace_dir
        self._session_checkpoint_repository = None

    def set_session_checkpoint_repository(self, repository) -> None:
        self._session_checkpoint_repository = repository
```

```python
    async def init_handler(self, *args, **kwargs):
        session_dir = str(
            (self.workspace_dir if self.workspace_dir else WORKING_DIR)
            / "sessions"
        )
        self.session = SafeJSONSession(
            save_dir=session_dir,
            tenant_id=self.tenant_id,
            checkpoint_repo=self._session_checkpoint_repository,
        )
```

- [ ] **Step 4: Register and inject the new workspace service**

```python
# src/swe/app/workspace/workspace.py
sm.register(
    ServiceDescriptor(
        name="session_checkpoint_repository",
        service_class=None,
        post_init=create_session_persistence_service,
        stop_method="close",
        reusable=True,
        priority=20,
        concurrent_init=True,
    ),
)
```

```python
# src/swe/app/workspace/service_factories.py
async def create_session_persistence_service(ws: "Workspace", service):
    from urllib.parse import urlparse
    import os
    import aiomysql
    from ..runner.repo.session_checkpoint_mysql import (
        MySQLSessionCheckpointRepository,
    )

    if service is not None:
        repo = service
    else:
        dsn = os.getenv("SWE_MYSQL_DSN", "").strip()
        repo = None
        if dsn:
            parsed = urlparse(dsn)
            pool = await aiomysql.create_pool(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=parsed.username or "",
                password=parsed.password or "",
                db=parsed.path.lstrip("/"),
                autocommit=True,
                charset="utf8mb4",
            )
            repo = MySQLSessionCheckpointRepository(pool=pool)

    ws._service_manager.services["session_checkpoint_repository"] = repo
    ws._service_manager.services["runner"].set_session_checkpoint_repository(repo)
    return repo
```

- [ ] **Step 5: Run the wiring tests again**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_runner_session_wiring.py -v
```

Expected:
- PASS for runner construction and service-factory injection

- [ ] **Step 6: Run the focused regression suite**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session_checkpoint_contract.py tests/unit/app/runner/test_session_checkpoint_mysql.py tests/unit/app/runner/test_session.py tests/unit/app/runner/test_runner_session_wiring.py -v
```

Expected:
- PASS across all new session persistence tests

- [ ] **Step 7: Commit**

```bash
git add src/swe/app/runner/runner.py src/swe/app/workspace/workspace.py src/swe/app/workspace/service_factories.py tests/unit/app/runner/test_runner_session_wiring.py
git commit -m "feat(session): wire coordinated session persistence into workspace startup"
```

---

## Final Verification

- [ ] **Step 1: Run the complete focused test set**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/runner/test_session_checkpoint_contract.py tests/unit/app/runner/test_session_checkpoint_mysql.py tests/unit/app/runner/test_session.py tests/unit/app/runner/test_runner_session_wiring.py -v
```

Expected:
- PASS with no stale-writer overwrite regressions

- [ ] **Step 2: Run one existing workspace startup regression**

Run:
```bash
venv/bin/python -m pytest tests/unit/app/test_lazy_loading.py -k workspace_start -v
```

Expected:
- PASS, confirming runner/workspace boot still works after the new service registration

- [ ] **Step 3: Inspect for unresolved placeholders before handing off**

Run:
```bash
rg -n "TBD|TODO|implement later|fill in details" docs/superpowers/plans/2026-04-08-coordinated-session-state-persistence.md
```

Expected:
- No output

Plan complete and saved to `docs/superpowers/plans/2026-04-08-coordinated-session-state-persistence.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
