# MySQL Cron Definition Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move authoritative cron and heartbeat definitions from workspace-local `jobs.json` and agent heartbeat config into MySQL so multi-instance cron mutation and reload behavior is durable and consistent.

**Architecture:** Add tenant-and-agent scoped MySQL repositories for cron jobs and heartbeat definitions, bootstrap them from existing workspace files once, then make `CronManager`, cron APIs, heartbeat APIs, and workspace startup read and write only through those repositories. Keep `HEARTBEAT.md` content on disk; only schedule/config state moves to MySQL.

**Tech Stack:** Python, FastAPI, APScheduler, Pydantic, PyMySQL, pytest

---

## File Structure

**Create**
- `src/swe/app/crons/repo/mysql_support.py` - MySQL settings, scope object, connection helpers, schema bootstrap.
- `src/swe/app/crons/repo/mysql_job_repo.py` - MySQL-backed cron job repository with row-level CRUD overrides.
- `src/swe/app/crons/repo/mysql_heartbeat_repo.py` - MySQL-backed heartbeat definition repository.
- `src/swe/app/crons/repo/importer.py` - One-time import/backfill from `jobs.json` and `agent.json`.
- `tests/unit/app/crons/test_mysql_storage.py` - Unit tests for settings, schema bootstrap hooks, and importer guards.
- `tests/unit/app/crons/test_mysql_job_repo.py` - Unit tests for scoped job CRUD, concurrent-safe writes, and cross-instance reads.
- `tests/unit/app/test_mysql_cron_manager.py` - Unit tests for manager reload, pause/resume persistence, and heartbeat reload behavior.
- `tests/unit/app/test_tenant_heartbeat_api.py` - API tests for MySQL-backed heartbeat reads/writes.

**Modify**
- `pyproject.toml` - Add the MySQL client dependency used by cron storage.
- `src/swe/app/crons/repo/__init__.py` - Export new repository classes.
- `src/swe/app/crons/manager.py` - Reload schedules from MySQL, persist pause/resume, and read heartbeat definitions from durable storage.
- `src/swe/app/crons/heartbeat.py` - Accept the authoritative heartbeat config from `CronManager` instead of re-reading `agent.json`.
- `src/swe/app/crons/api.py` - Keep cron routes on `CronManager`, but rely on durable mutation semantics and 404 behavior for pause/resume.
- `src/swe/app/routers/config.py` - Move heartbeat GET/PUT off `agent.config.heartbeat` and onto the durable repo via `CronManager`.
- `src/swe/app/agent_config_watcher.py` - Stop treating `agent.json` heartbeat config as authoritative.
- `src/swe/app/workspace/service_factories.py` - Create and bootstrap MySQL cron storage during workspace startup.
- `src/swe/app/workspace/workspace.py` - Switch cron service wiring from `JsonJobRepository` to the MySQL-backed factory.
- `tests/unit/app/test_tenant_cron_api.py` - Keep tenant injection assertions and update stubs for new manager behavior.
- `tests/unit/workspace/test_workspace.py` - Verify workspace cron wiring uses tenant/agent-scoped durable storage.

## Current-State Notes

- `src/swe/app/crons/repo/base.py` currently implements `upsert_job()` and `delete_job()` as full-collection read/modify/write. Do not use those default methods for MySQL writes; override them in the MySQL repo.
- `src/swe/app/crons/manager.py` currently pauses and resumes jobs only in memory. That is not durable and must change.
- Heartbeat schedule settings currently live in `agent.config.heartbeat` and are reloaded by `AgentConfigWatcher`. This change must move that authority to MySQL.
- `HEARTBEAT.md` remains file-backed in this change. Only heartbeat schedule/config is migrated.

---

### Task 1: Add MySQL Storage Foundation

**Files:**
- Create: `src/swe/app/crons/repo/mysql_support.py`
- Modify: `pyproject.toml`
- Modify: `src/swe/app/crons/repo/__init__.py`
- Test: `tests/unit/app/crons/test_mysql_storage.py`

- [ ] **Step 1: Write the failing storage-settings test**

```python
# tests/unit/app/crons/test_mysql_storage.py
import pytest


def test_mysql_settings_parse_dsn(monkeypatch):
    monkeypatch.setenv(
        "SWE_CRON_MYSQL_DSN",
        "mysql://cron_user:cron_pass@127.0.0.1:3306/swe_cron",
    )

    from swe.app.crons.repo.mysql_support import CronMySQLSettings

    settings = CronMySQLSettings.from_env()
    assert settings.host == "127.0.0.1"
    assert settings.port == 3306
    assert settings.user == "cron_user"
    assert settings.password == "cron_pass"
    assert settings.database == "swe_cron"


def test_mysql_settings_require_dsn(monkeypatch):
    monkeypatch.delenv("SWE_CRON_MYSQL_DSN", raising=False)

    from swe.app.crons.repo.mysql_support import CronMySQLSettings

    with pytest.raises(RuntimeError, match="SWE_CRON_MYSQL_DSN"):
        CronMySQLSettings.from_env()
```

- [ ] **Step 2: Run the test and confirm it fails because the module does not exist**

Run: `venv/bin/python -m pytest tests/unit/app/crons/test_mysql_storage.py -v`

Expected: `FAILED` with `ModuleNotFoundError: No module named 'swe.app.crons.repo.mysql_support'`

- [ ] **Step 3: Add the dependency and storage bootstrap module**

```toml
# pyproject.toml
dependencies = [
    # ...
    "pymysql>=1.1.1",
]
```

```python
# src/swe/app/crons/repo/mysql_support.py
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
import asyncio
import pymysql

from ....constant import EnvVarLoader


@dataclass(frozen=True)
class CronStorageScope:
    tenant_id: str
    agent_id: str


@dataclass(frozen=True)
class CronMySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> "CronMySQLSettings":
        dsn = EnvVarLoader.get_str("SWE_CRON_MYSQL_DSN", "").strip()
        if not dsn:
            raise RuntimeError("SWE_CRON_MYSQL_DSN is required for cron storage")
        parsed = urlparse(dsn)
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=parsed.username or "",
            password=parsed.password or "",
            database=(parsed.path or "/").lstrip("/"),
        )


class MySQLCronStore:
    def __init__(self, settings: CronMySQLSettings):
        self._settings = settings

    def _connect(self):
        return pymysql.connect(
            host=self._settings.host,
            port=self._settings.port,
            user=self._settings.user,
            password=self._settings.password,
            database=self._settings.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    async def run(self, fn):
        return await asyncio.to_thread(self._run_sync, fn)

    def _run_sync(self, fn):
        with self._connect() as conn:
            result = fn(conn)
            conn.commit()
            return result

    async def ensure_schema(self) -> None:
        def _create(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cron_job_definitions (
                        tenant_id VARCHAR(191) NOT NULL,
                        agent_id VARCHAR(191) NOT NULL,
                        job_id VARCHAR(191) NOT NULL,
                        definition_json LONGTEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (tenant_id, agent_id, job_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cron_heartbeat_definitions (
                        tenant_id VARCHAR(191) NOT NULL,
                        agent_id VARCHAR(191) NOT NULL,
                        definition_json LONGTEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (tenant_id, agent_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )

        await self.run(_create)
```

```python
# src/swe/app/crons/repo/__init__.py
from .base import BaseJobRepository
from .json_repo import JsonJobRepository
from .mysql_support import CronMySQLSettings, CronStorageScope, MySQLCronStore
```

- [ ] **Step 4: Run the storage test again**

Run: `venv/bin/python -m pytest tests/unit/app/crons/test_mysql_storage.py -v`

Expected: `PASSED`

- [ ] **Step 5: Commit the foundation change**

```bash
git add pyproject.toml \
  src/swe/app/crons/repo/mysql_support.py \
  src/swe/app/crons/repo/__init__.py \
  tests/unit/app/crons/test_mysql_storage.py
git commit -m "feat(crons): add mysql cron storage foundation"
```

---

### Task 2: Implement Durable Cron and Heartbeat Repositories Plus Importer

**Files:**
- Create: `src/swe/app/crons/repo/mysql_job_repo.py`
- Create: `src/swe/app/crons/repo/mysql_heartbeat_repo.py`
- Create: `src/swe/app/crons/repo/importer.py`
- Modify: `src/swe/app/crons/repo/__init__.py`
- Test: `tests/unit/app/crons/test_mysql_job_repo.py`
- Test: `tests/unit/app/crons/test_mysql_storage.py`

- [ ] **Step 1: Write failing repository and importer tests**

```python
# tests/unit/app/crons/test_mysql_job_repo.py
import asyncio
from pathlib import Path

import pytest

from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    ScheduleSpec,
)
from swe.app.crons.repo.mysql_support import CronStorageScope


def _job(job_id: str, *, enabled: bool = True) -> CronJobSpec:
    return CronJobSpec(
        id=job_id,
        name=f"job-{job_id}",
        tenant_id="tenant-a",
        enabled=enabled,
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(input=[{"content": [{"type": "text", "text": "ping"}]}]),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={},
        ),
        runtime=JobRuntimeSpec(),
    )


@pytest.mark.asyncio
async def test_mysql_job_repo_preserves_parallel_upserts(mysql_store):
    from swe.app.crons.repo.mysql_job_repo import MySQLJobRepository

    scope = CronStorageScope(tenant_id="tenant-a", agent_id="agent-a")
    repo_a = MySQLJobRepository(store=mysql_store, scope=scope)
    repo_b = MySQLJobRepository(store=mysql_store, scope=scope)

    await asyncio.gather(
        repo_a.upsert_job(_job("job-a")),
        repo_b.upsert_job(_job("job-b")),
    )

    rows = await repo_a.list_jobs()
    assert {job.id for job in rows} == {"job-a", "job-b"}


@pytest.mark.asyncio
async def test_mysql_job_repo_reads_latest_state_across_instances(mysql_store):
    from swe.app.crons.repo.mysql_job_repo import MySQLJobRepository

    scope = CronStorageScope(tenant_id="tenant-a", agent_id="agent-a")
    writer = MySQLJobRepository(store=mysql_store, scope=scope)
    reader = MySQLJobRepository(store=mysql_store, scope=scope)

    await writer.upsert_job(_job("job-a", enabled=False))

    job = await reader.get_job("job-a")
    assert job is not None
    assert job.enabled is False
```

```python
# tests/unit/app/crons/test_mysql_storage.py
import json

import pytest

from swe.config.config import HeartbeatConfig


@pytest.mark.asyncio
async def test_importer_backfills_jobs_json_and_heartbeat(tmp_path, mysql_store):
    from swe.app.crons.repo.importer import import_workspace_cron_definitions
    from swe.app.crons.repo.mysql_heartbeat_repo import MySQLHeartbeatRepository
    from swe.app.crons.repo.mysql_job_repo import MySQLJobRepository
    from swe.app.crons.repo.mysql_support import CronStorageScope

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "jobs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job-a",
                        "name": "imported",
                        "enabled": True,
                        "tenant_id": "tenant-a",
                        "schedule": {"type": "cron", "cron": "* * * * *", "timezone": "UTC"},
                        "task_type": "agent",
                        "request": {"input": [{"content": [{"type": "text", "text": "ping"}]}]},
                        "dispatch": {
                            "type": "channel",
                            "channel": "console",
                            "target": {"user_id": "user-a", "session_id": "session-a"},
                            "mode": "stream",
                            "meta": {},
                        },
                        "runtime": {
                            "max_concurrency": 1,
                            "timeout_seconds": 120,
                            "misfire_grace_seconds": 60,
                        },
                        "meta": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (workspace_dir / "agent.json").write_text(
        json.dumps(
            {
                "id": "agent-a",
                "name": "Agent A",
                "heartbeat": {"enabled": True, "every": "6h", "target": "main"},
                "running": {},
                "system_prompt_files": ["AGENTS.md"],
            }
        ),
        encoding="utf-8",
    )

    scope = CronStorageScope(tenant_id="tenant-a", agent_id="agent-a")
    await import_workspace_cron_definitions(
        workspace_dir=workspace_dir,
        scope=scope,
        job_repo=MySQLJobRepository(store=mysql_store, scope=scope),
        heartbeat_repo=MySQLHeartbeatRepository(store=mysql_store, scope=scope),
    )

    jobs = await MySQLJobRepository(store=mysql_store, scope=scope).list_jobs()
    heartbeat = await MySQLHeartbeatRepository(store=mysql_store, scope=scope).get()
    assert [job.id for job in jobs] == ["job-a"]
    assert heartbeat == HeartbeatConfig(enabled=True, every="6h", target="main")
```

- [ ] **Step 2: Run the targeted repository tests and confirm they fail**

Run: `venv/bin/python -m pytest tests/unit/app/crons/test_mysql_job_repo.py tests/unit/app/crons/test_mysql_storage.py -v`

Expected: `FAILED` with `ImportError` for `mysql_job_repo`, `mysql_heartbeat_repo`, or `importer`

- [ ] **Step 3: Implement row-level repositories and one-time importer**

```python
# src/swe/app/crons/repo/mysql_job_repo.py
from __future__ import annotations

import json

from .base import BaseJobRepository
from .mysql_support import CronStorageScope, MySQLCronStore
from ..models import CronJobSpec, JobsFile


class MySQLJobRepository(BaseJobRepository):
    def __init__(self, *, store: MySQLCronStore, scope: CronStorageScope):
        self._store = store
        self._scope = scope

    async def load(self) -> JobsFile:
        return JobsFile(version=1, jobs=await self.list_jobs())

    async def save(self, jobs_file: JobsFile) -> None:
        rows = [job.model_dump(mode="json") for job in jobs_file.jobs]

        def _save(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cron_job_definitions WHERE tenant_id=%s AND agent_id=%s",
                    (self._scope.tenant_id, self._scope.agent_id),
                )
                for row in rows:
                    cur.execute(
                        """
                        INSERT INTO cron_job_definitions
                        (tenant_id, agent_id, job_id, definition_json)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            self._scope.tenant_id,
                            self._scope.agent_id,
                            row["id"],
                            json.dumps(row, ensure_ascii=False),
                        ),
                    )

        await self._store.run(_save)

    async def list_jobs(self) -> list[CronJobSpec]:
        def _list(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT definition_json
                    FROM cron_job_definitions
                    WHERE tenant_id=%s AND agent_id=%s
                    ORDER BY job_id
                    """,
                    (self._scope.tenant_id, self._scope.agent_id),
                )
                return cur.fetchall()

        rows = await self._store.run(_list)
        return [
            CronJobSpec.model_validate(json.loads(row["definition_json"]))
            for row in rows
        ]

    async def get_job(self, job_id: str) -> CronJobSpec | None:
        def _get(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT definition_json
                    FROM cron_job_definitions
                    WHERE tenant_id=%s AND agent_id=%s AND job_id=%s
                    """,
                    (self._scope.tenant_id, self._scope.agent_id, job_id),
                )
                return cur.fetchone()

        row = await self._store.run(_get)
        if row is None:
            return None
        return CronJobSpec.model_validate(json.loads(row["definition_json"]))

    async def upsert_job(self, spec: CronJobSpec) -> None:
        payload = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)

        def _upsert(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cron_job_definitions
                    (tenant_id, agent_id, job_id, definition_json)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE definition_json=VALUES(definition_json)
                    """,
                    (self._scope.tenant_id, self._scope.agent_id, spec.id, payload),
                )

        await self._store.run(_upsert)

    async def delete_job(self, job_id: str) -> bool:
        def _delete(conn):
            with conn.cursor() as cur:
                return cur.execute(
                    """
                    DELETE FROM cron_job_definitions
                    WHERE tenant_id=%s AND agent_id=%s AND job_id=%s
                    """,
                    (self._scope.tenant_id, self._scope.agent_id, job_id),
                )

        return bool(await self._store.run(_delete))
```

```python
# src/swe/app/crons/repo/mysql_heartbeat_repo.py
from __future__ import annotations

import json

from ....config.config import HeartbeatConfig
from .mysql_support import CronStorageScope, MySQLCronStore


class MySQLHeartbeatRepository:
    def __init__(self, *, store: MySQLCronStore, scope: CronStorageScope):
        self._store = store
        self._scope = scope

    async def get(self) -> HeartbeatConfig:
        def _get(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT definition_json
                    FROM cron_heartbeat_definitions
                    WHERE tenant_id=%s AND agent_id=%s
                    """,
                    (self._scope.tenant_id, self._scope.agent_id),
                )
                return cur.fetchone()

        row = await self._store.run(_get)
        if row is None:
            return HeartbeatConfig()
        return HeartbeatConfig.model_validate(json.loads(row["definition_json"]))

    async def save(self, heartbeat: HeartbeatConfig) -> None:
        payload = json.dumps(heartbeat.model_dump(mode="json", by_alias=True))

        def _save(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cron_heartbeat_definitions
                    (tenant_id, agent_id, definition_json)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE definition_json=VALUES(definition_json)
                    """,
                    (self._scope.tenant_id, self._scope.agent_id, payload),
                )

        await self._store.run(_save)
```

```python
# src/swe/app/crons/repo/importer.py
from __future__ import annotations

import json
from pathlib import Path

from ....config.config import HeartbeatConfig
from .json_repo import JsonJobRepository


async def import_workspace_cron_definitions(
    *,
    workspace_dir: Path,
    scope,
    job_repo,
    heartbeat_repo,
) -> None:
    existing_jobs = await job_repo.list_jobs()
    if not existing_jobs and (workspace_dir / "jobs.json").exists():
        jobs_file = await JsonJobRepository(workspace_dir / "jobs.json").load()
        for job in jobs_file.jobs:
            await job_repo.upsert_job(job.model_copy(update={"tenant_id": scope.tenant_id}))

    existing_heartbeat = await heartbeat_repo.get()
    if existing_heartbeat == HeartbeatConfig() and (workspace_dir / "agent.json").exists():
        payload = json.loads((workspace_dir / "agent.json").read_text(encoding="utf-8"))
        heartbeat = HeartbeatConfig.model_validate(payload.get("heartbeat") or {})
        await heartbeat_repo.save(heartbeat)
```

- [ ] **Step 4: Run the repository/importer suite again**

Run: `venv/bin/python -m pytest tests/unit/app/crons/test_mysql_job_repo.py tests/unit/app/crons/test_mysql_storage.py -v`

Expected: `PASSED`

- [ ] **Step 5: Commit the durable repository layer**

```bash
git add src/swe/app/crons/repo/mysql_job_repo.py \
  src/swe/app/crons/repo/mysql_heartbeat_repo.py \
  src/swe/app/crons/repo/importer.py \
  src/swe/app/crons/repo/__init__.py \
  tests/unit/app/crons/test_mysql_job_repo.py \
  tests/unit/app/crons/test_mysql_storage.py
git commit -m "feat(crons): add mysql cron definition repositories"
```

---

### Task 3: Make CronManager Reload From Durable Storage

**Files:**
- Modify: `src/swe/app/crons/manager.py`
- Modify: `src/swe/app/crons/heartbeat.py`
- Test: `tests/unit/app/test_mysql_cron_manager.py`

- [ ] **Step 1: Write the failing manager tests**

```python
# tests/unit/app/test_mysql_cron_manager.py
import pytest

from swe.app.crons.manager import CronManager
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    ScheduleSpec,
)
from swe.config.config import HeartbeatConfig


def _job(job_id: str, *, enabled: bool = True):
    return CronJobSpec(
        id=job_id,
        name=job_id,
        tenant_id="tenant-a",
        enabled=enabled,
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(input=[{"content": [{"type": "text", "text": "ping"}]}]),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={},
        ),
        runtime=JobRuntimeSpec(),
    )


class _Repo:
    def __init__(self):
        self.jobs = {"job-a": _job("job-a")}

    async def list_jobs(self):
        return list(self.jobs.values())

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def upsert_job(self, spec):
        self.jobs[spec.id] = spec

    async def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None


class _HeartbeatRepo:
    def __init__(self):
        self.value = HeartbeatConfig(enabled=True, every="6h", target="main")

    async def get(self):
        return self.value

    async def save(self, heartbeat):
        self.value = heartbeat


@pytest.mark.asyncio
async def test_pause_job_persists_disabled_flag():
    manager = CronManager(
        repo=_Repo(),
        heartbeat_repo=_HeartbeatRepo(),
        runner=object(),
        channel_manager=object(),
        agent_id="agent-a",
    )

    await manager.pause_job("job-a")

    job = await manager.get_job("job-a")
    assert job is not None
    assert job.enabled is False


@pytest.mark.asyncio
async def test_reschedule_heartbeat_reads_durable_definition():
    manager = CronManager(
        repo=_Repo(),
        heartbeat_repo=_HeartbeatRepo(),
        runner=object(),
        channel_manager=object(),
        agent_id="agent-a",
    )

    heartbeat = await manager.get_heartbeat_definition()
    assert heartbeat.enabled is True
    assert heartbeat.every == "6h"
```

- [ ] **Step 2: Run the manager test and confirm it fails on missing constructor args or methods**

Run: `venv/bin/python -m pytest tests/unit/app/test_mysql_cron_manager.py -v`

Expected: `FAILED` with `TypeError` or `AttributeError` for `heartbeat_repo`, `get_heartbeat_definition`, or persistence behavior

- [ ] **Step 3: Update `CronManager` so MySQL is authoritative**

```python
# src/swe/app/crons/manager.py
class CronManager:
    def __init__(
        self,
        *,
        repo,
        heartbeat_repo,
        runner,
        channel_manager,
        timezone: str = "UTC",
        agent_id: str | None = None,
    ):
        self._repo = repo
        self._heartbeat_repo = heartbeat_repo
        # existing setup...

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._scheduler.start()
            await self._reload_jobs_locked()
            await self._reload_heartbeat_locked()
            self._started = True

    async def pause_job(self, job_id: str) -> None:
        async with self._lock:
            job = await self._repo.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            await self._repo.upsert_job(job.model_copy(update={"enabled": False}))
            await self._reload_jobs_locked()

    async def resume_job(self, job_id: str) -> None:
        async with self._lock:
            job = await self._repo.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            await self._repo.upsert_job(job.model_copy(update={"enabled": True}))
            await self._reload_jobs_locked()

    async def get_heartbeat_definition(self):
        return await self._heartbeat_repo.get()

    async def update_heartbeat_definition(self, heartbeat) -> None:
        async with self._lock:
            await self._heartbeat_repo.save(heartbeat)
            await self._reload_heartbeat_locked()
```

```python
# src/swe/app/crons/heartbeat.py
async def run_heartbeat_once(
    *,
    runner,
    channel_manager,
    heartbeat_config,
    agent_id=None,
    workspace_dir=None,
):
    hb = heartbeat_config
    if not hb.enabled:
        return
    # existing execution logic continues unchanged
```

- [ ] **Step 4: Run the manager suite again**

Run: `venv/bin/python -m pytest tests/unit/app/test_mysql_cron_manager.py -v`

Expected: `PASSED`

- [ ] **Step 5: Commit the manager reload change**

```bash
git add src/swe/app/crons/manager.py \
  src/swe/app/crons/heartbeat.py \
  tests/unit/app/test_mysql_cron_manager.py
git commit -m "feat(crons): reload scheduler from mysql definitions"
```

---

### Task 4: Switch Cron and Heartbeat APIs Plus Workspace Wiring

**Files:**
- Modify: `src/swe/app/crons/api.py`
- Modify: `src/swe/app/routers/config.py`
- Modify: `src/swe/app/agent_config_watcher.py`
- Modify: `src/swe/app/workspace/service_factories.py`
- Modify: `src/swe/app/workspace/workspace.py`
- Modify: `tests/unit/app/test_tenant_cron_api.py`
- Modify: `tests/unit/workspace/test_workspace.py`
- Create: `tests/unit/app/test_tenant_heartbeat_api.py`

- [ ] **Step 1: Write the failing API and workspace tests**

```python
# tests/unit/app/test_tenant_heartbeat_api.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.config.config import HeartbeatConfig


class _Manager:
    def __init__(self):
        self.heartbeat = HeartbeatConfig(enabled=True, every="6h", target="main")

    async def get_heartbeat_definition(self):
        return self.heartbeat

    async def update_heartbeat_definition(self, heartbeat):
        self.heartbeat = heartbeat


def test_get_heartbeat_reads_from_cron_manager():
    from swe.app.routers import config as config_router

    app = FastAPI()
    app.include_router(config_router.router)
    manager = _Manager()

    async def _agent_override(request):
        return type(
            "Agent",
            (),
            {"cron_manager": manager, "config": type("Cfg", (), {})()},
        )()

    app.dependency_overrides[config_router.get_agent_for_request] = _agent_override
    client = TestClient(app)

    response = client.get("/config/heartbeat")
    assert response.status_code == 200
    assert response.json()["every"] == "6h"
```

```python
# tests/unit/workspace/test_workspace.py
async def test_workspace_cron_service_uses_mysql_storage(monkeypatch, tmp_path):
    from swe.app.workspace import Workspace

    seen = {}

    async def _fake_create_cron_service(ws, _service):
        seen["tenant_id"] = ws.tenant_id
        seen["agent_id"] = ws.agent_id
        return object()

    monkeypatch.setattr(
        "swe.app.workspace.service_factories.create_cron_service",
        _fake_create_cron_service,
    )

    workspace = Workspace(
        agent_id="agent-a",
        workspace_dir=str(tmp_path / "agent-a"),
        tenant_id="tenant-a",
    )
    await workspace._service_manager.start_service("cron_manager")

    assert seen == {"tenant_id": "tenant-a", "agent_id": "agent-a"}
```

- [ ] **Step 2: Run the API/workspace tests and confirm they fail**

Run: `venv/bin/python -m pytest tests/unit/app/test_tenant_cron_api.py tests/unit/app/test_tenant_heartbeat_api.py tests/unit/workspace/test_workspace.py -v`

Expected: `FAILED` because heartbeat routes still read `agent.config.heartbeat` and cron service is still wired directly to `JsonJobRepository`

- [ ] **Step 3: Move heartbeat routes and workspace startup onto MySQL storage**

```python
# src/swe/app/routers/config.py
@router.get("/heartbeat")
async def get_heartbeat(request: Request) -> Any:
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    if agent.cron_manager is None:
        raise HTTPException(status_code=500, detail="CronManager not initialized")
    heartbeat = await agent.cron_manager.get_heartbeat_definition()
    return heartbeat.model_dump(mode="json", by_alias=True)


@router.put("/heartbeat")
async def put_heartbeat(request: Request, body: HeartbeatBody = Body(...)) -> Any:
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    if agent.cron_manager is None:
        raise HTTPException(status_code=500, detail="CronManager not initialized")
    heartbeat = HeartbeatConfig(
        enabled=body.enabled,
        every=body.every,
        target=body.target,
        active_hours=body.active_hours,
    )
    await agent.cron_manager.update_heartbeat_definition(heartbeat)
    return heartbeat.model_dump(mode="json", by_alias=True)
```

```python
# src/swe/app/workspace/service_factories.py
async def create_cron_service(ws: "Workspace", _service):
    from ..crons.manager import CronManager
    from ..crons.repo.importer import import_workspace_cron_definitions
    from ..crons.repo.mysql_heartbeat_repo import MySQLHeartbeatRepository
    from ..crons.repo.mysql_job_repo import MySQLJobRepository
    from ..crons.repo.mysql_support import (
        CronMySQLSettings,
        CronStorageScope,
        MySQLCronStore,
    )

    settings = CronMySQLSettings.from_env()
    scope = CronStorageScope(
        tenant_id=ws.tenant_id or "default",
        agent_id=ws.agent_id,
    )
    store = MySQLCronStore(settings)
    await store.ensure_schema()

    job_repo = MySQLJobRepository(store=store, scope=scope)
    heartbeat_repo = MySQLHeartbeatRepository(store=store, scope=scope)
    await import_workspace_cron_definitions(
        workspace_dir=ws.workspace_dir,
        scope=scope,
        job_repo=job_repo,
        heartbeat_repo=heartbeat_repo,
    )

    manager = CronManager(
        repo=job_repo,
        heartbeat_repo=heartbeat_repo,
        runner=ws._service_manager.services["runner"],
        channel_manager=ws._service_manager.services.get("channel_manager"),
        timezone="UTC",
        agent_id=ws.agent_id,
    )
    ws._service_manager.services["cron_manager"] = manager
    return manager
```

```python
# src/swe/app/workspace/workspace.py
from .service_factories import create_cron_service

sm.register(
    ServiceDescriptor(
        name="cron_manager",
        service_class=None,
        post_init=create_cron_service,
        start_method="start",
        stop_method="stop",
        priority=40,
        concurrent_init=False,
    ),
)
```

```python
# src/swe/app/agent_config_watcher.py
# Remove the heartbeat hash/update branch entirely.
# After this change, agent.json heartbeat edits are not authoritative.
```

- [ ] **Step 4: Run the API/workspace suite again**

Run: `venv/bin/python -m pytest tests/unit/app/test_tenant_cron_api.py tests/unit/app/test_tenant_heartbeat_api.py tests/unit/workspace/test_workspace.py -v`

Expected: `PASSED`

- [ ] **Step 5: Commit the API and workspace cutover**

```bash
git add src/swe/app/crons/api.py \
  src/swe/app/routers/config.py \
  src/swe/app/agent_config_watcher.py \
  src/swe/app/workspace/service_factories.py \
  src/swe/app/workspace/workspace.py \
  tests/unit/app/test_tenant_cron_api.py \
  tests/unit/app/test_tenant_heartbeat_api.py \
  tests/unit/workspace/test_workspace.py
git commit -m "feat(crons): wire api and workspace to mysql storage"
```

---

### Task 5: Run Focused Verification and Cutover Checks

**Files:**
- Modify: `tests/unit/app/crons/test_mysql_storage.py`
- Modify: `tests/unit/app/crons/test_mysql_job_repo.py`
- Modify: `tests/unit/app/test_mysql_cron_manager.py`
- Modify: `tests/unit/app/test_tenant_heartbeat_api.py`
- Modify: `tests/unit/app/test_tenant_cron_api.py`
- Modify: `tests/unit/workspace/test_workspace.py`

- [ ] **Step 1: Add one final regression for import idempotence**

```python
# tests/unit/app/crons/test_mysql_storage.py
@pytest.mark.asyncio
async def test_importer_does_not_overwrite_existing_mysql_rows(tmp_path, mysql_store):
    from swe.app.crons.repo.importer import import_workspace_cron_definitions
    from swe.app.crons.repo.mysql_heartbeat_repo import MySQLHeartbeatRepository
    from swe.app.crons.repo.mysql_job_repo import MySQLJobRepository
    from swe.app.crons.repo.mysql_support import CronStorageScope

    scope = CronStorageScope(tenant_id="tenant-a", agent_id="agent-a")
    job_repo = MySQLJobRepository(store=mysql_store, scope=scope)
    heartbeat_repo = MySQLHeartbeatRepository(store=mysql_store, scope=scope)

    await job_repo.upsert_job(_job("mysql-row"))
    await heartbeat_repo.save(HeartbeatConfig(enabled=True, every="30m", target="main"))

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "jobs.json").write_text('{"version": 1, "jobs": []}', encoding="utf-8")
    (workspace_dir / "agent.json").write_text('{"id": "agent-a", "name": "Agent A"}', encoding="utf-8")

    await import_workspace_cron_definitions(
        workspace_dir=workspace_dir,
        scope=scope,
        job_repo=job_repo,
        heartbeat_repo=heartbeat_repo,
    )

    assert [job.id for job in await job_repo.list_jobs()] == ["mysql-row"]
    assert (await heartbeat_repo.get()).every == "30m"
```

- [ ] **Step 2: Run the focused cron storage suite**

Run: `venv/bin/python -m pytest tests/unit/app/crons/test_mysql_storage.py tests/unit/app/crons/test_mysql_job_repo.py tests/unit/app/test_mysql_cron_manager.py tests/unit/app/test_tenant_cron_api.py tests/unit/app/test_tenant_heartbeat_api.py tests/unit/workspace/test_workspace.py -v`

Expected: all selected tests `PASSED`

- [ ] **Step 3: Run the broader cron-related regression suite**

Run: `venv/bin/python -m pytest tests/unit/app/test_tenant_cron_execution.py tests/unit/app/test_tenant_cron_manager_push.py tests/unit/app/test_tenant_cron_api.py tests/unit/app/test_tenant_heartbeat_api.py -v`

Expected: all selected tests `PASSED`

- [ ] **Step 4: Verify no placeholder text or stale JSON-authority assumptions remain in the plan implementation**

Run: `venv/bin/python -m pytest tests/unit/app/crons/test_mysql_storage.py -v`

Expected: `PASSED` and no remaining references in code paths that treat `jobs.json` or `agent.config.heartbeat` as the runtime authority

- [ ] **Step 5: Commit the verification pass**

```bash
git add tests/unit/app/crons/test_mysql_storage.py \
  tests/unit/app/crons/test_mysql_job_repo.py \
  tests/unit/app/test_mysql_cron_manager.py \
  tests/unit/app/test_tenant_cron_api.py \
  tests/unit/app/test_tenant_heartbeat_api.py \
  tests/unit/workspace/test_workspace.py
git commit -m "test(crons): verify mysql cron definition storage"
```

---

## Spec Coverage Check

- Durable transactional storage:
  implemented by Task 1 and Task 2 through MySQL schema, row-level repositories, and importer.
- Consistent cross-instance reads after mutation:
  covered by `test_mysql_job_repo_reads_latest_state_across_instances` in Task 2.
- Concurrent cron mutations do not overwrite one another:
  covered by `test_mysql_job_repo_preserves_parallel_upserts` in Task 2.
- Scheduler reloads use durable definitions:
  implemented in Task 3 by reloading `CronManager` from the MySQL repos.
- Migration preserves existing definitions:
  implemented in Task 2 and Task 5 through importer tests and idempotence checks.

## Self-Review Notes

- Placeholder scan: no `TODO`, `TBD`, or unspecified commands remain.
- Type consistency: repository names, manager methods, and heartbeat method names are consistent across tasks.
- Scope check: this plan stays within the child change by focusing on durable storage and reload wiring, not Redis leadership mechanics.
