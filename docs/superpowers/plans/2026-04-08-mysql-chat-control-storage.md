# MySQL Chat Control Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `chats.json` as the authoritative chat metadata store with tenant/agent-scoped MySQL-backed repositories, persist durable interactive run facts, and preserve existing JSON chat visibility through import-on-read during rollout.

**Architecture:** Keep the current `ChatManager` and `TaskTracker` entry points, but swap the storage layer underneath them. Add a small async SQLAlchemy control-store module, make MySQL the only writer, wrap it in a migration-aware repository that can import `chats.json` once, and extend the runtime path so terminal run outcomes are stored separately from in-memory coordination.

**Tech Stack:** Python, FastAPI, Pydantic, SQLAlchemy 2 async engine, `asyncmy`, `aiosqlite` for tests, pytest, pytest-asyncio

---

## File Structure / Responsibility Map

- `pyproject.toml:7-43`
  - Add the database dependencies needed by the new control-store layer.
- `src/swe/constant.py:11-120`
  - Add env-driven settings for the chat control database DSN and parity-check flag.
- `src/swe/app/persistence/__init__.py`
  - Export the shared control-store helpers.
- `src/swe/app/persistence/mysql.py`
  - Own async engine/session creation and schema bootstrap for the chat control store.
- `src/swe/app/runner/repo/base.py:1-145`
  - Refactor the chat repository interface to entity operations instead of file-wide `load`/`save`.
- `src/swe/app/runner/repo/json_repo.py:1-70`
  - Keep JSON loading as a fallback/import source, not an authoritative writer.
- `src/swe/app/runner/repo/mysql_schema.py`
  - Define SQLAlchemy tables for `chat_specs` and `chat_runs`.
- `src/swe/app/runner/repo/mysql_chat_repo.py`
  - Implement tenant/agent-scoped authoritative chat metadata reads and writes.
- `src/swe/app/runner/repo/migrating_repo.py`
  - Import legacy `chats.json` rows into MySQL when the authoritative store is empty and optionally parity-check JSON vs MySQL.
- `src/swe/app/runner/run_models.py`
  - Define durable run record models and runtime context passed into the tracker.
- `src/swe/app/runner/repo/run_base.py`
  - Define the durable run record repository interface.
- `src/swe/app/runner/repo/mysql_run_repo.py`
  - Implement durable run storage in MySQL.
- `src/swe/app/runner/manager.py:17-242`
  - Keep chat CRUD behavior, add run-record methods, and expose a small API for the tracker and router.
- `src/swe/app/runner/task_tracker.py:1-230`
  - Keep in-memory coordination, but persist run lifecycle facts on start/success/failure/cancel.
- `src/swe/app/workspace/service_factories.py:36-62`
  - Instantiate the migration-aware MySQL repositories and bind them to the workspace `ChatManager`.
- `src/swe/app/channels/base.py:375-430`
  - Pass durable run context into `TaskTracker.attach_or_start` for channel-originated chats.
- `src/swe/app/routers/console.py:68-166`
  - Pass durable run context into the tracker for console chats.
- `src/swe/app/runner/api.py:64-240`
  - Keep existing chat CRUD endpoints unchanged at the contract level and add a new endpoint to query durable run facts.
- `src/swe/app/runner/repo/__init__.py`
  - Export the new repositories.
- `src/swe/app/runner/__init__.py`
  - Export the new run models and repository types.
- `tests/unit/runner/test_chat_repository_contract.py`
  - Lock the repository contract before adding MySQL.
- `tests/unit/runner/test_mysql_chat_repository.py`
  - Verify tenant/agent scoping, cross-instance visibility, and JSON import behavior.
- `tests/unit/runner/test_task_tracker_run_persistence.py`
  - Verify completed and failed runs remain queryable after in-memory tracking ends.
- `tests/unit/workspace/test_chat_service_factory.py`
  - Verify workspace wiring uses the migration-aware MySQL repositories.
- `tests/unit/routers/test_chat_runs_api.py`
  - Verify the durable run-record API shape and 404 behavior.

---

### Task 1: Freeze the repository contract and configuration surface

**Files:**
- Modify: `pyproject.toml:7-43`
- Modify: `src/swe/constant.py:11-120`
- Modify: `src/swe/app/runner/repo/base.py:1-145`
- Modify: `src/swe/app/runner/repo/json_repo.py:1-70`
- Create: `tests/unit/runner/test_chat_repository_contract.py`

- [ ] **Step 1: Write the failing repository contract test**

```python
# tests/unit/runner/test_chat_repository_contract.py
import pytest

from swe.app.runner.models import ChatSpec
from swe.app.runner.repo.json_repo import JsonChatRepository


@pytest.mark.asyncio
async def test_json_repo_implements_entity_contract(tmp_path):
    repo = JsonChatRepository(tmp_path / "chats.json")
    spec = ChatSpec(
        id="chat-1",
        name="Alpha",
        session_id="console:alice",
        user_id="alice",
        channel="console",
    )

    await repo.upsert_chat(spec)

    fetched = await repo.get_chat_by_session(
        "console:alice",
        "alice",
        "console",
    )
    assert fetched is not None
    assert fetched.id == "chat-1"
```

- [ ] **Step 2: Run the test to capture the interface gap**

Run: `venv/bin/python -m pytest tests/unit/runner/test_chat_repository_contract.py -v`

Expected:
- FAIL with `AttributeError: 'JsonChatRepository' object has no attribute 'get_chat_by_session'`

- [ ] **Step 3: Refactor the repository interface and add DB settings**

```python
# src/swe/constant.py
MYSQL_CHAT_CONTROL_DSN = EnvVarLoader.get_str(
    "SWE_MYSQL_CHAT_CONTROL_DSN",
    "",
)
MYSQL_CHAT_CONTROL_PARITY_CHECK = EnvVarLoader.get_bool(
    "SWE_MYSQL_CHAT_CONTROL_PARITY_CHECK",
    False,
)
```

```toml
# pyproject.toml
dependencies = [
    "agentscope==1.0.18",
    "agentscope-runtime==1.1.3",
    "httpx>=0.27.0",
    "sqlalchemy>=2.0.39,<3",
    "asyncmy>=0.2.10",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3.5",
    "pytest-asyncio>=0.23.0",
    "pre-commit>=4.2.0",
    "pytest-cov>=6.2.1",
    "hypothesis>=6.0.0",
    "aiosqlite>=0.20.0",
]
```

```python
# src/swe/app/runner/repo/base.py
class BaseChatRepository(ABC):
    @abstractmethod
    async def list_chats(self) -> list[ChatSpec]:
        raise NotImplementedError

    @abstractmethod
    async def get_chat(self, chat_id: str) -> Optional[ChatSpec]:
        raise NotImplementedError

    @abstractmethod
    async def get_chat_by_session(
        self,
        session_id: str,
        user_id: str,
        channel: str = DEFAULT_CHANNEL,
    ) -> Optional[ChatSpec]:
        raise NotImplementedError

    @abstractmethod
    async def upsert_chat(self, spec: ChatSpec) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_chats(self, chat_ids: list[str]) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def filter_chats(
        self,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> list[ChatSpec]:
        raise NotImplementedError
```

```python
# src/swe/app/runner/repo/json_repo.py
async def get_chat_by_session(
    self,
    session_id: str,
    user_id: str,
    channel: str = DEFAULT_CHANNEL,
) -> Optional[ChatSpec]:
    cf = await self.load()
    for chat in cf.chats:
        if (
            chat.session_id == session_id
            and chat.user_id == user_id
            and chat.channel == channel
        ):
            return chat
    return None
```

- [ ] **Step 4: Re-run the contract test**

Run: `venv/bin/python -m pytest tests/unit/runner/test_chat_repository_contract.py -v`

Expected:
- PASS with `1 passed`

- [ ] **Step 5: Commit the contract-only change**

```bash
git add pyproject.toml src/swe/constant.py src/swe/app/runner/repo/base.py src/swe/app/runner/repo/json_repo.py tests/unit/runner/test_chat_repository_contract.py
git commit -m "feat(chat): freeze chat repository contract"
```

---

### Task 2: Add the authoritative MySQL chat repository and JSON import wrapper

**Files:**
- Create: `src/swe/app/persistence/__init__.py`
- Create: `src/swe/app/persistence/mysql.py`
- Create: `src/swe/app/runner/repo/mysql_schema.py`
- Create: `src/swe/app/runner/repo/mysql_chat_repo.py`
- Create: `src/swe/app/runner/repo/migrating_repo.py`
- Modify: `src/swe/app/runner/repo/__init__.py`
- Create: `tests/unit/runner/test_mysql_chat_repository.py`

- [ ] **Step 1: Write the failing repository behavior tests**

```python
# tests/unit/runner/test_mysql_chat_repository.py
import asyncio

import pytest

from swe.app.runner.models import ChatSpec


@pytest.mark.asyncio
async def test_mysql_repo_is_scoped_by_tenant_and_agent(tmp_path):
    from swe.app.persistence.mysql import create_control_store_engine
    from swe.app.runner.repo.mysql_chat_repo import MysqlChatRepository

    engine = create_control_store_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}",
    )
    repo_a = MysqlChatRepository(engine, tenant_id="tenant-a", agent_id="a1")
    repo_b = MysqlChatRepository(engine, tenant_id="tenant-b", agent_id="a1")

    await repo_a.upsert_chat(
        ChatSpec(
            id="chat-a",
            name="Tenant A",
            session_id="console:alice",
            user_id="alice",
            channel="console",
        ),
    )

    assert await repo_a.get_chat("chat-a") is not None
    assert await repo_b.get_chat("chat-a") is None


@pytest.mark.asyncio
async def test_mysql_repo_keeps_concurrent_creates_from_two_instances(tmp_path):
    from swe.app.persistence.mysql import create_control_store_engine
    from swe.app.runner.repo.mysql_chat_repo import MysqlChatRepository

    engine = create_control_store_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}",
    )
    repo_a = MysqlChatRepository(engine, tenant_id="tenant-a", agent_id="a1")
    repo_b = MysqlChatRepository(engine, tenant_id="tenant-a", agent_id="a1")

    await asyncio.gather(
        repo_a.upsert_chat(
            ChatSpec(
                id="chat-1",
                name="One",
                session_id="console:one",
                user_id="one",
                channel="console",
            ),
        ),
        repo_b.upsert_chat(
            ChatSpec(
                id="chat-2",
                name="Two",
                session_id="console:two",
                user_id="two",
                channel="console",
            ),
        ),
    )

    chats = await repo_a.list_chats()
    assert {chat.id for chat in chats} == {"chat-1", "chat-2"}
```

- [ ] **Step 2: Run the repository tests**

Run: `venv/bin/python -m pytest tests/unit/runner/test_mysql_chat_repository.py -v`

Expected:
- FAIL with `ModuleNotFoundError: No module named 'swe.app.persistence.mysql'`

- [ ] **Step 3: Add the shared control-store engine and schema**

```python
# src/swe/app/persistence/mysql.py
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def create_control_store_engine(dsn: str | None = None) -> AsyncEngine:
    url = dsn or MYSQL_CHAT_CONTROL_DSN
    if not url:
        raise RuntimeError("SWE_MYSQL_CHAT_CONTROL_DSN is not configured")
    return create_async_engine(url, future=True, pool_pre_ping=True)


def create_control_store_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
```

```python
# src/swe/app/runner/repo/mysql_schema.py
metadata = MetaData()

chat_specs = Table(
    "chat_specs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("agent_id", String(128), nullable=False),
    Column("name", String(255), nullable=False),
    Column("session_id", String(255), nullable=False),
    Column("user_id", String(255), nullable=False),
    Column("channel", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("meta", JSON, nullable=False),
    UniqueConstraint(
        "tenant_id",
        "agent_id",
        "session_id",
        "user_id",
        "channel",
        name="uq_chat_specs_session_scope",
    ),
    Index("ix_chat_specs_scope", "tenant_id", "agent_id", "updated_at"),
)
```

- [ ] **Step 4: Implement the authoritative repo and the migration wrapper**

```python
# src/swe/app/runner/repo/mysql_chat_repo.py
class MysqlChatRepository(BaseChatRepository):
    def __init__(self, engine, *, tenant_id: str, agent_id: str):
        self._engine = engine
        self._tenant_id = tenant_id
        self._agent_id = agent_id

    async def filter_chats(self, user_id=None, channel=None) -> list[ChatSpec]:
        stmt = select(chat_specs).where(
            chat_specs.c.tenant_id == self._tenant_id,
            chat_specs.c.agent_id == self._agent_id,
        )
        if user_id is not None:
            stmt = stmt.where(chat_specs.c.user_id == user_id)
        if channel is not None:
            stmt = stmt.where(chat_specs.c.channel == channel)
        stmt = stmt.order_by(chat_specs.c.updated_at.desc())
        return await self._fetch_many(stmt)

    async def get_chat_by_session(self, session_id, user_id, channel=DEFAULT_CHANNEL):
        stmt = select(chat_specs).where(
            chat_specs.c.tenant_id == self._tenant_id,
            chat_specs.c.agent_id == self._agent_id,
            chat_specs.c.session_id == session_id,
            chat_specs.c.user_id == user_id,
            chat_specs.c.channel == channel,
        )
        return await self._fetch_one(stmt)

    async def upsert_chat(self, spec: ChatSpec) -> None:
        # Use dialect-specific upsert so separate backend instances do not
        # overwrite one another by round-tripping the whole collection.
        payload = {
            "id": spec.id,
            "tenant_id": self._tenant_id,
            "agent_id": self._agent_id,
            "name": spec.name,
            "session_id": spec.session_id,
            "user_id": spec.user_id,
            "channel": spec.channel,
            "created_at": spec.created_at,
            "updated_at": spec.updated_at,
            "meta": spec.meta,
        }
        await self._execute_upsert(payload)
```

```python
# src/swe/app/runner/repo/migrating_repo.py
class MigratingChatRepository(BaseChatRepository):
    def __init__(self, primary, fallback, *, parity_check: bool = False):
        self._primary = primary
        self._fallback = fallback
        self._parity_check = parity_check
        self._import_lock = asyncio.Lock()

    async def _ensure_primary_seeded(self) -> None:
        if await self._primary.list_chats():
            return
        async with self._import_lock:
            if await self._primary.list_chats():
                return
            for chat in await self._fallback.list_chats():
                await self._primary.upsert_chat(chat)

    async def list_chats(self) -> list[ChatSpec]:
        await self._ensure_primary_seeded()
        return await self._primary.list_chats()
```

- [ ] **Step 5: Re-run the repository tests**

Run: `venv/bin/python -m pytest tests/unit/runner/test_mysql_chat_repository.py -v`

Expected:
- PASS with `2 passed`

- [ ] **Step 6: Commit the storage layer**

```bash
git add src/swe/app/persistence/__init__.py src/swe/app/persistence/mysql.py src/swe/app/runner/repo/mysql_schema.py src/swe/app/runner/repo/mysql_chat_repo.py src/swe/app/runner/repo/migrating_repo.py src/swe/app/runner/repo/__init__.py tests/unit/runner/test_mysql_chat_repository.py
git commit -m "feat(chat): add mysql-backed chat repository"
```

---

### Task 3: Persist durable run facts from the runtime path

**Files:**
- Create: `src/swe/app/runner/run_models.py`
- Create: `src/swe/app/runner/repo/run_base.py`
- Create: `src/swe/app/runner/repo/mysql_run_repo.py`
- Modify: `src/swe/app/runner/repo/mysql_schema.py`
- Modify: `src/swe/app/runner/manager.py:17-242`
- Modify: `src/swe/app/runner/task_tracker.py:1-230`
- Create: `tests/unit/runner/test_task_tracker_run_persistence.py`

- [ ] **Step 1: Write the failing run-persistence tests**

```python
# tests/unit/runner/test_task_tracker_run_persistence.py
import pytest

from swe.app.runner.run_models import ChatRunContext
from swe.app.runner.task_tracker import TaskTracker


@pytest.mark.asyncio
async def test_completed_run_remains_queryable_after_tracker_cleanup(tmp_path):
    from swe.app.persistence.mysql import create_control_store_engine
    from swe.app.runner.manager import ChatManager
    from swe.app.runner.models import ChatSpec
    from swe.app.runner.repo.mysql_chat_repo import MysqlChatRepository
    from swe.app.runner.repo.mysql_run_repo import MysqlChatRunRepository

    engine = create_control_store_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}",
    )
    manager = ChatManager(
        repo=MysqlChatRepository(engine, tenant_id="tenant-a", agent_id="a1"),
        run_repo=MysqlChatRunRepository(
            engine,
            tenant_id="tenant-a",
            agent_id="a1",
        ),
    )
    tracker = TaskTracker()
    tracker.bind_chat_manager(manager)

    chat = ChatSpec(
        id="chat-1",
        name="Alpha",
        session_id="console:alice",
        user_id="alice",
        channel="console",
    )
    await manager.create_chat(chat)

    async def stream_fn(_payload):
        yield "data: {\"message\": \"ok\"}\n\n"

    queue, _ = await tracker.attach_or_start(
        chat.id,
        {"payload": "ignored"},
        stream_fn,
        run_context=ChatRunContext.from_chat(chat),
    )

    async for _ in tracker.stream_from_queue(queue, chat.id):
        pass

    assert await tracker.get_status(chat.id) == "idle"
    runs = await manager.list_runs(chat.id, limit=10)
    assert len(runs) == 1
    assert runs[0].status == "completed"


@pytest.mark.asyncio
async def test_failed_run_persists_failure_result(tmp_path):
    from swe.app.persistence.mysql import create_control_store_engine
    from swe.app.runner.manager import ChatManager
    from swe.app.runner.models import ChatSpec
    from swe.app.runner.repo.mysql_chat_repo import MysqlChatRepository
    from swe.app.runner.repo.mysql_run_repo import MysqlChatRunRepository

    engine = create_control_store_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}",
    )
    manager = ChatManager(
        repo=MysqlChatRepository(engine, tenant_id="tenant-a", agent_id="a1"),
        run_repo=MysqlChatRunRepository(
            engine,
            tenant_id="tenant-a",
            agent_id="a1",
        ),
    )
    tracker = TaskTracker()
    tracker.bind_chat_manager(manager)

    chat = ChatSpec(
        id="chat-2",
        name="Beta",
        session_id="console:bob",
        user_id="bob",
        channel="console",
    )
    await manager.create_chat(chat)

    async def stream_fn(_payload):
        raise RuntimeError("boom")
        yield "data: {}\n\n"

    queue, _ = await tracker.attach_or_start(
        chat.id,
        {"payload": "ignored"},
        stream_fn,
        run_context=ChatRunContext.from_chat(chat),
    )

    async for _ in tracker.stream_from_queue(queue, chat.id):
        pass

    runs = await manager.list_runs(chat.id, limit=10)
    assert runs[0].status == "failed"
    assert "boom" in runs[0].error
```

- [ ] **Step 2: Run the failing tests**

Run: `venv/bin/python -m pytest tests/unit/runner/test_task_tracker_run_persistence.py -v`

Expected:
- FAIL with `ModuleNotFoundError: No module named 'swe.app.runner.run_models'`

- [ ] **Step 3: Add run models and the MySQL run repository**

```python
# src/swe/app/runner/run_models.py
class ChatRunRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    chat_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    session_id: str
    user_id: str
    channel: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    error: str | None = None


class ChatRunContext(BaseModel):
    chat_id: str
    session_id: str
    user_id: str
    channel: str

    @classmethod
    def from_chat(cls, chat: ChatSpec) -> "ChatRunContext":
        return cls(
            chat_id=chat.id,
            session_id=chat.session_id,
            user_id=chat.user_id,
            channel=chat.channel,
        )
```

```python
# src/swe/app/runner/repo/run_base.py
class BaseChatRunRepository(ABC):
    @abstractmethod
    async def create_run(self, record: ChatRunRecord) -> ChatRunRecord:
        raise NotImplementedError

    @abstractmethod
    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_runs(self, chat_id: str, *, limit: int) -> list[ChatRunRecord]:
        raise NotImplementedError
```

- [ ] **Step 4: Extend `ChatManager` and `TaskTracker` to write lifecycle facts**

```python
# src/swe/app/runner/manager.py
class ChatManager:
    def __init__(self, *, repo: BaseChatRepository, run_repo: BaseChatRunRepository):
        self._repo = repo
        self._run_repo = run_repo
        self._lock = asyncio.Lock()

    async def start_run(self, context: ChatRunContext) -> ChatRunRecord:
        record = ChatRunRecord(**context.model_dump(), status="running")
        return await self._run_repo.create_run(record)

    async def finish_run(self, run_id: str, *, status: str, error: str | None = None) -> None:
        await self._run_repo.finish_run(run_id, status=status, error=error)

    async def list_runs(self, chat_id: str, limit: int = 20) -> list[ChatRunRecord]:
        return await self._run_repo.list_runs(chat_id, limit=limit)
```

```python
# src/swe/app/runner/task_tracker.py
class TaskTracker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._chat_manager = None

    def bind_chat_manager(self, chat_manager) -> None:
        self._chat_manager = chat_manager

    async def attach_or_start(self, run_key, payload, stream_fn, run_context=None):
        async with self._lock:
            state = self._runs.get(run_key)
            if state is not None and not state.task.done():
                q = asyncio.Queue()
                for sse in state.buffer:
                    q.put_nowait(sse)
                state.queues.append(q)
                return q, False
            my_queue = asyncio.Queue()
            run = _RunState(
                task=asyncio.Future(),
                queues=[my_queue],
                buffer=[],
            )
            self._runs[run_key] = run
        async def _producer() -> None:
            run_record = None
            try:
                if self._chat_manager is not None and run_context is not None:
                    run_record = await self._chat_manager.start_run(run_context)
                async for sse in stream_fn(payload):
                    async with self._lock:
                        run.buffer.append(sse)
                        for q in run.queues:
                            q.put_nowait(sse)
                if run_record is not None:
                    await self._chat_manager.finish_run(
                        run_record.id,
                        status="completed",
                    )
            except asyncio.CancelledError:
                if run_record is not None:
                    await self._chat_manager.finish_run(
                        run_record.id,
                        status="cancelled",
                    )
                raise
            except Exception as exc:
                if run_record is not None:
                    await self._chat_manager.finish_run(
                        run_record.id,
                        status="failed",
                        error=str(exc),
                    )
                logger.exception("run error run_key=%s", run_key)
```

- [ ] **Step 5: Re-run the run-persistence tests**

Run: `venv/bin/python -m pytest tests/unit/runner/test_task_tracker_run_persistence.py -v`

Expected:
- PASS with `2 passed`

- [ ] **Step 6: Commit the durable-run work**

```bash
git add src/swe/app/runner/run_models.py src/swe/app/runner/repo/run_base.py src/swe/app/runner/repo/mysql_run_repo.py src/swe/app/runner/repo/mysql_schema.py src/swe/app/runner/manager.py src/swe/app/runner/task_tracker.py tests/unit/runner/test_task_tracker_run_persistence.py
git commit -m "feat(chat): persist durable interactive run facts"
```

---

### Task 4: Wire the workspace and expose durable run facts through the API

**Files:**
- Modify: `src/swe/app/workspace/service_factories.py:36-62`
- Modify: `src/swe/app/channels/base.py:375-430`
- Modify: `src/swe/app/routers/console.py:68-166`
- Modify: `src/swe/app/runner/api.py:64-240`
- Modify: `src/swe/app/runner/repo/__init__.py`
- Modify: `src/swe/app/runner/__init__.py`
- Create: `tests/unit/workspace/test_chat_service_factory.py`
- Create: `tests/unit/routers/test_chat_runs_api.py`

- [ ] **Step 1: Write the failing wiring and API tests**

```python
# tests/unit/workspace/test_chat_service_factory.py
import pytest


@pytest.mark.asyncio
async def test_create_chat_service_uses_migrating_mysql_repo(monkeypatch, tmp_path):
    from swe.app.workspace.service_factories import create_chat_service

    created = {}

    class FakeRunner:
        def set_chat_manager(self, manager):
            created["manager"] = manager

    class FakeWorkspace:
        agent_id = "agent-1"
        tenant_id = "tenant-a"
        workspace_dir = tmp_path
        _config = object()
        _task_tracker = type("Tracker", (), {"bind_chat_manager": lambda self, manager: created.setdefault("bound", manager)})()
        _service_manager = type(
            "SM",
            (),
            {"services": {"runner": FakeRunner()}},
        )()

    await create_chat_service(FakeWorkspace(), None)

    assert created["manager"].__class__.__name__ == "ChatManager"
    assert created["bound"] is created["manager"]
```

```python
# tests/unit/routers/test_chat_runs_api.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.app.runner.api import router
from swe.app.runner.models import ChatSpec
from swe.app.runner.run_models import ChatRunRecord


class FakeManager:
    async def get_chat(self, chat_id):
        if chat_id != "chat-1":
            return None
        return ChatSpec(
            id="chat-1",
            name="Alpha",
            session_id="console:alice",
            user_id="alice",
            channel="console",
        )

    async def list_runs(self, chat_id, limit=20):
        return [
            ChatRunRecord(
                id="run-1",
                chat_id=chat_id,
                status="completed",
                session_id="console:alice",
                user_id="alice",
                channel="console",
            ),
        ]
```

- [ ] **Step 2: Run the new tests**

Run: `venv/bin/python -m pytest tests/unit/workspace/test_chat_service_factory.py tests/unit/routers/test_chat_runs_api.py -v`

Expected:
- FAIL because `create_chat_service` still constructs `JsonChatRepository`
- FAIL because `GET /chats/{chat_id}/runs` does not exist

- [ ] **Step 3: Wire the workspace to the new repositories and bind the tracker**

```python
# src/swe/app/workspace/service_factories.py
async def create_chat_service(ws: "Workspace", service):
    from ..runner.manager import ChatManager
    from ..runner.repo.json_repo import JsonChatRepository
    from ..runner.repo.migrating_repo import MigratingChatRepository
    from ..runner.repo.mysql_chat_repo import MysqlChatRepository
    from ..runner.repo.mysql_run_repo import MysqlChatRunRepository
    from ..persistence.mysql import create_control_store_engine

    if service is None:
        engine = create_control_store_engine()
        json_repo = JsonChatRepository(ws.workspace_dir / "chats.json")
        chat_repo = MigratingChatRepository(
            MysqlChatRepository(
                engine,
                tenant_id=ws.tenant_id or "default",
                agent_id=ws.agent_id,
            ),
            json_repo,
            parity_check=MYSQL_CHAT_CONTROL_PARITY_CHECK,
        )
        run_repo = MysqlChatRunRepository(
            engine,
            tenant_id=ws.tenant_id or "default",
            agent_id=ws.agent_id,
        )
        cm = ChatManager(repo=chat_repo, run_repo=run_repo)
    else:
        cm = service

    ws._service_manager.services["chat_manager"] = cm
    ws._service_manager.services["runner"].set_chat_manager(cm)
    ws._task_tracker.bind_chat_manager(cm)
```

- [ ] **Step 4: Pass run context through console/channel entry points and add the API**

```python
# src/swe/app/channels/base.py
queue, is_new = await self._workspace.task_tracker.attach_or_start(
    chat.id,
    payload,
    self._stream_with_tracker,
    run_context=ChatRunContext.from_chat(chat),
)
```

```python
# src/swe/app/routers/console.py
queue, _ = await tracker.attach_or_start(
    chat.id,
    native_payload,
    console_channel.stream_one,
    run_context=ChatRunContext.from_chat(chat),
)
```

```python
# src/swe/app/runner/api.py
@router.get("/{chat_id}/runs", response_model=list[ChatRunRecord])
async def list_chat_runs(
    chat_id: str,
    limit: int = Query(20, ge=1, le=100),
    mgr: ChatManager = Depends(get_chat_manager),
):
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail=f"Chat not found: {chat_id}")
    return await mgr.list_runs(chat_id, limit=limit)
```

- [ ] **Step 5: Re-run the workspace/API tests and then the focused regression suite**

Run: `venv/bin/python -m pytest tests/unit/workspace/test_chat_service_factory.py tests/unit/routers/test_chat_runs_api.py -v`

Expected:
- PASS with `2 passed`

Run: `venv/bin/python -m pytest tests/unit/runner/test_chat_repository_contract.py tests/unit/runner/test_mysql_chat_repository.py tests/unit/runner/test_task_tracker_run_persistence.py tests/unit/workspace/test_workspace.py -v`

Expected:
- PASS with all targeted chat-control tests green

- [ ] **Step 6: Commit the wiring and API work**

```bash
git add src/swe/app/workspace/service_factories.py src/swe/app/channels/base.py src/swe/app/routers/console.py src/swe/app/runner/api.py src/swe/app/runner/repo/__init__.py src/swe/app/runner/__init__.py tests/unit/workspace/test_chat_service_factory.py tests/unit/routers/test_chat_runs_api.py
git commit -m "feat(chat): wire mysql chat control storage"
```

---

## Final Verification

- [ ] Run the storage contract suite

Run: `venv/bin/python -m pytest tests/unit/runner/test_chat_repository_contract.py tests/unit/runner/test_mysql_chat_repository.py -v`

Expected:
- PASS with the contract test, tenant/agent scoping test, and concurrent create test all green

- [ ] Run the durable run suite

Run: `venv/bin/python -m pytest tests/unit/runner/test_task_tracker_run_persistence.py tests/unit/routers/test_chat_runs_api.py -v`

Expected:
- PASS with completed and failed runs still queryable after tracker cleanup

- [ ] Run the workspace wiring regression suite

Run: `venv/bin/python -m pytest tests/unit/workspace/test_chat_service_factory.py tests/unit/workspace/test_workspace.py -v`

Expected:
- PASS with service wiring and base workspace behavior unchanged

---

## Spec Coverage Check

- Transactional durable chat metadata:
  - Implemented by Task 2 and wired into runtime reads/writes in Task 4.
- Durable interactive run facts:
  - Implemented by Task 3 and exposed for reads in Task 4.
- Migration from shared `chats.json`:
  - Implemented by `MigratingChatRepository` in Task 2; JSON remains a fallback/import source only during rollout.

Plan complete and saved to `docs/superpowers/plans/2026-04-08-mysql-chat-control-storage.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
