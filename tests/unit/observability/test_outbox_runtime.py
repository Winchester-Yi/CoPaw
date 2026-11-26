# -*- coding: utf-8 -*-
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock
import json
import os
import tempfile
import threading

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.metadata_repository import (
    SCOPE_PREDICATE,
    WORKSPACES,
)
from swe.observability import outbox_runtime
from swe.observability.outbox_recovery import (
    clear_registered_scopes,
    scope_registered,
)


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "file"])
def test_config_snapshot_rejects_replacement_before_open(
    monkeypatch,
    tmp_path,
    replacement,
):
    path = tmp_path / "config.json"
    path.write_text("{}")
    target = tmp_path / "target"
    target.write_text('{"other": true}')
    real_open = os.open

    def replace_before_open(name, flags, *args):
        if name == path:
            path.unlink()
            if replacement == "symlink":
                path.symlink_to(target)
            elif replacement == "fifo":
                os.mkfifo(path)
            else:
                path.write_text('{"other": true}')
        return real_open(name, flags, *args)

    monkeypatch.setattr(outbox_runtime.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="recovery config"):
        outbox_runtime._read_config_snapshot(path)


def test_config_snapshot_rejects_in_place_change_during_read(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "config.json"
    path.write_text("{}")
    real_fstat = os.fstat
    calls = 0

    def change_before_second_stat(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text('{"modified": true}')
        return real_fstat(descriptor)

    monkeypatch.setattr(outbox_runtime.os, "fstat", change_before_second_stat)
    with pytest.raises(ValueError, match="changed during read"):
        outbox_runtime._read_config_snapshot(path)


@pytest.mark.parametrize(
    "content",
    [
        b'{"last_api_host":"127.0.0.1", "agents":{"active_agent":123}}',
        b'{"last_api_host":"127.0.0.1",}',
        b"[]",
        b"\xff",
    ],
)
def test_in_memory_config_matches_file_loading_without_writes(
    tmp_path,
    content,
):
    from swe.config.utils import load_config, load_config_bytes

    path = tmp_path / "config.json"
    path.write_bytes(content)
    expected = load_config(path).model_dump()
    for backup in tmp_path.glob("*.bak"):
        backup.unlink()
    assert load_config_bytes(content).model_dump() == expected
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_config_snapshot_does_not_block_event_loop_or_write_temp_file(
    monkeypatch,
    tmp_path,
):
    import asyncio

    root = tmp_path / "workspace"
    root.mkdir()
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "secret": "private-test-secret",
                "agents": {
                    "profiles": {"a": {"id": "a", "workspace_dir": str(root)}},
                },
            },
        ),
    )
    entered = threading.Event()
    release = threading.Event()
    original = outbox_runtime._read_config_snapshot

    def blocked_read(config_path):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test did not release config reader")
        return original(config_path)

    monkeypatch.setattr(outbox_runtime, "_read_config_snapshot", blocked_read)
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    monkeypatch.setattr(
        outbox_runtime,
        "get_tenant_storage_config_path",
        lambda _: path,
    )
    monkeypatch.setattr(outbox_runtime, "storage_authority", lambda: "legacy")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    task = asyncio.create_task(
        outbox_runtime.recovery_worker_for_scope(
            WorkspaceScope("t", "s", "a", service_unit_id="unit"),
            metadata_db=None,
            observability_db=object(),
            emitter=object(),
            expected_root=str(root),
            expected_authority="legacy",
        ),
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not task.done()
        assert sorted(item.name for item in tmp_path.iterdir()) == [
            "config.json",
            "workspace",
        ]
    finally:
        release.set()
    await task
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "config.json",
        "workspace",
    ]


@pytest.mark.asyncio
async def test_timed_out_config_reader_does_not_block_other_scope_or_queue(
    monkeypatch,
    tmp_path,
):
    import asyncio

    blocked = tmp_path / "blocked"
    healthy = tmp_path / "healthy"
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def read(path, _agent):
        calls.append(path)
        if path == blocked:
            entered.set()
            try:
                assert release.wait(3)
            finally:
                finished.set()
        return path

    monkeypatch.setattr(outbox_runtime, "_load_recovery_root", read)
    monkeypatch.setattr(outbox_runtime, "_CONFIG_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await outbox_runtime._resolve_recovery_root(blocked, "a")
        assert entered.is_set()
        with pytest.raises(RuntimeError, match="busy"):
            await outbox_runtime._resolve_recovery_root(blocked, "a")
        assert (
            await outbox_runtime._resolve_recovery_root(healthy, "a")
            == healthy
        )
        assert calls == [blocked, healthy]
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        for _ in range(100):
            with outbox_runtime._CONFIG_IN_FLIGHT_LOCK:
                if not outbox_runtime._CONFIG_IN_FLIGHT:
                    break
            await asyncio.sleep(0.01)
        assert not outbox_runtime._CONFIG_IN_FLIGHT


@pytest.mark.asyncio
async def test_cancelled_config_reads_keep_slots_until_threads_finish(
    monkeypatch,
    tmp_path,
):
    import asyncio

    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    paths = [tmp_path / "one", tmp_path / "two"]

    def read(path, _agent):
        entered[paths.index(path)].set()
        assert release.wait(3)
        return path

    monkeypatch.setattr(outbox_runtime, "_load_recovery_root", read)
    tasks = [
        asyncio.create_task(outbox_runtime._resolve_recovery_root(path, "a"))
        for path in paths
    ]
    try:
        assert await asyncio.to_thread(entered[0].wait, 1)
        assert await asyncio.to_thread(entered[1].wait, 1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(RuntimeError, match="busy"):
            await outbox_runtime._resolve_recovery_root(
                tmp_path / "three",
                "a",
            )
    finally:
        release.set()
        for _ in range(100):
            with outbox_runtime._CONFIG_IN_FLIGHT_LOCK:
                if not outbox_runtime._CONFIG_IN_FLIGHT:
                    break
            await asyncio.sleep(0.01)
        assert not outbox_runtime._CONFIG_IN_FLIGHT


@pytest.mark.asyncio
async def test_registration_is_not_installed_when_recovery_schema_is_missing(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    class Connection:
        config = SimpleNamespace(database="observability")

        @asynccontextmanager
        async def acquire(self):
            yield self

        async def fetch_all(self, *_args):
            raise RuntimeError("missing table")

    monkeypatch.setattr("swe.observability.outbox._keys", lambda: None)
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    manager = SimpleNamespace(set_outbox_register=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_observability_connection=Connection(),
            agent_observability_store=object(),
        ),
    )
    clear_registered_scopes()
    with pytest.raises(RuntimeError):
        await outbox_runtime.start_recovery(app, manager, None)
    assert app.state.agent_observability_recovery is None
    manager.set_outbox_register.assert_not_called()
    assert not scope_registered(
        {
            "service_unit_id": "unit",
            "tenant_id": "t",
            "source_id": "s",
            "agent_id": "a",
        },
        None,
    )


@pytest.mark.asyncio
async def test_factory_uses_read_only_legacy_workspace_and_checks_service_unit(
    monkeypatch,
    tmp_path,
):
    from swe.observability.outbox_recovery import scope_digest

    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    root = tmp_path / "workspace"
    root.mkdir()
    scope = WorkspaceScope("t", "s", "a", service_unit_id="unit")
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    monkeypatch.setattr(
        outbox_runtime,
        "get_tenant_storage_config_path",
        lambda _tenant: config_path,
    )
    monkeypatch.setattr(
        outbox_runtime,
        "load_config_bytes",
        lambda _path: SimpleNamespace(
            agents=SimpleNamespace(
                profiles={"a": SimpleNamespace(workspace_dir=str(root))},
            ),
        ),
    )
    monkeypatch.setattr(outbox_runtime, "storage_authority", lambda: "legacy")
    emitter = object()
    worker = await outbox_runtime.recovery_worker_for_scope(
        scope,
        metadata_db=None,
        observability_db=object(),
        emitter=emitter,
        expected_root=str(root),
        expected_authority="legacy",
    )
    assert worker.store.scope_key == scope_digest(scope)
    assert worker.scanner.scope == scope
    assert worker.scanner.workspace.workspace_dir == root
    assert worker.scanner.workspace.runner.session.save_dir == str(
        root / "sessions",
    )
    with pytest.raises(ValueError, match="service unit"):
        await outbox_runtime.recovery_worker_for_scope(
            WorkspaceScope("t", "s", "a", service_unit_id="other"),
            metadata_db=None,
            observability_db=object(),
            emitter=emitter,
            expected_root=str(root),
            expected_authority="legacy",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("config_kind", ["missing", "compatible", "invalid"])
async def test_factory_loads_workspace_with_application_config_semantics(
    monkeypatch,
    tmp_path,
    config_kind,
):
    from swe.config import config as config_module

    root = tmp_path / "workspaces" / "default"
    root.mkdir(parents=True)
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "WORKING_DIR", tmp_path)
    if config_kind == "compatible":
        config_path.write_text(
            json.dumps(
                {
                    "agents": {
                        "active_agent": 123,
                        "profiles": {
                            "default": {
                                "id": "default",
                                "workspace_dir": str(root),
                            },
                        },
                    },
                    "last_api_host": "127.0.0.1",
                },
            ),
        )
    elif config_kind == "invalid":
        config_path.write_text("[]")
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    monkeypatch.setattr(
        outbox_runtime,
        "get_tenant_storage_config_path",
        lambda _: config_path,
    )
    monkeypatch.setattr(outbox_runtime, "storage_authority", lambda: "legacy")
    scope = WorkspaceScope("t", "s", "default", service_unit_id="unit")
    kwargs = {
        "metadata_db": None,
        "observability_db": object(),
        "emitter": object(),
        "expected_root": str(root),
        "expected_authority": "legacy",
    }
    worker = await outbox_runtime.recovery_worker_for_scope(scope, **kwargs)
    assert worker.scanner.workspace.workspace_dir == root
    assert not list(tmp_path.glob("*.bak"))
    with pytest.raises(ValueError, match="storage binding changed"):
        await outbox_runtime.recovery_worker_for_scope(
            scope,
            **{**kwargs, "expected_root": str(tmp_path)},
        )
    with pytest.raises(ValueError, match="storage binding changed"):
        await outbox_runtime.recovery_worker_for_scope(
            scope,
            **{**kwargs, "expected_authority": "mysql-metadata"},
        )


@pytest.mark.asyncio
async def test_factory_rejects_symlinked_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.symlink_to(tmp_path / "missing.json")
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    monkeypatch.setattr(
        outbox_runtime,
        "get_tenant_storage_config_path",
        lambda _: config_path,
    )
    with pytest.raises(ValueError, match="config unavailable"):
        await outbox_runtime.recovery_worker_for_scope(
            WorkspaceScope("t", "s", "default", service_unit_id="unit"),
            metadata_db=None,
            observability_db=object(),
            emitter=object(),
            expected_root=str(tmp_path),
            expected_authority="legacy",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_status", ["matches", "wrong_root", "absent"])
async def test_mysql_recovery_checks_only_workspace_binding(
    monkeypatch,
    tmp_path,
    binding_status,
):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    root = tmp_path / "workspace"
    root.mkdir()
    scope = WorkspaceScope("t", "s", "a", service_unit_id="unit")
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    monkeypatch.setattr(
        outbox_runtime,
        "get_tenant_storage_config_path",
        lambda _: config_path,
    )
    monkeypatch.setattr(
        outbox_runtime,
        "load_config_bytes",
        lambda _: SimpleNamespace(
            agents=SimpleNamespace(
                profiles={"a": SimpleNamespace(workspace_dir=str(root))},
            ),
        ),
    )
    monkeypatch.setattr(
        outbox_runtime,
        "storage_authority",
        lambda: outbox_runtime.METADATA_AUTHORITY,
    )

    async def fetch_one(query, params):
        assert query == (
            f"SELECT workspace_root FROM {WORKSPACES} WHERE {SCOPE_PREDICATE}"
        )
        assert params == scope.sql_values
        if binding_status == "absent":
            return None
        bound_root = root if binding_status == "matches" else root / "other"
        return {"workspace_root": str(bound_root).encode()}

    async def build_worker():
        return await outbox_runtime.recovery_worker_for_scope(
            scope,
            metadata_db=SimpleNamespace(fetch_one=fetch_one),
            observability_db=object(),
            emitter=object(),
            expected_root=str(root),
            expected_authority="mysql-metadata",
        )

    if binding_status == "matches":
        worker = await build_worker()
        assert worker.scanner.scope == scope
    else:
        with pytest.raises(ValueError, match="workspace binding mismatch"):
            await build_worker()


@pytest.mark.asyncio
async def test_lifecycle_registers_after_scheduler_starts_and_clears_on_stop(
    monkeypatch,
    tmp_path,
):
    from contextlib import asynccontextmanager
    from swe.observability import outbox_recovery

    monkeypatch.setenv("SWE_AGENTOBS_ENABLED", "true")

    scope = WorkspaceScope("t", "s", "a", service_unit_id="unit")
    scope_dict = dict(
        zip(
            ("service_unit_id", "tenant_id", "source_id", "agent_id"),
            scope.values,
        ),
    )

    class Connection:
        config = SimpleNamespace(database="observability")

        @asynccontextmanager
        async def acquire(self):
            yield self

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetch_all(self, query, *_args):
            return (
                [
                    {"COLUMN_NAME": "event_id", "SEQ_IN_INDEX": 1},
                    {"COLUMN_NAME": "processing_state", "SEQ_IN_INDEX": 2},
                ]
                if "STATISTICS" in query
                else []
            )

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": str(tmp_path).encode(),
                "storage_authority": "legacy",
            }

    class Manager:
        register = None

        def set_outbox_register(self, value):
            self.register = value

    class Scheduler:
        started = False

        def __init__(self, *_args):
            pass

        async def start(self):
            self.started = True

        async def stop(self):
            self.started = False

    monkeypatch.setattr("swe.observability.outbox._keys", lambda: None)
    monkeypatch.setattr(
        "swe.app.conversation_storage.runtime.resolved_scope",
        lambda *_args, **_kwargs: scope,
    )
    monkeypatch.setattr(outbox_recovery, "OutboxRecoveryScheduler", Scheduler)
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    monkeypatch.setattr(outbox_runtime, "storage_authority", lambda: "legacy")
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_observability_connection=Connection(),
            agent_observability_store=object(),
        ),
    )
    manager = Manager()
    await outbox_runtime.start_recovery(app, manager, None)
    assert app.state.agent_observability_recovery.started
    assert not scope_registered(scope_dict, (str(tmp_path), "legacy"))
    await manager.register(SimpleNamespace(workspace_dir=tmp_path))
    assert scope_registered(scope_dict, (str(tmp_path), "legacy"))
    await outbox_runtime.stop_recovery(app, manager)
    assert manager.register is None
    assert not scope_registered(scope_dict, (str(tmp_path), "legacy"))
    assert app.state.agent_observability_recovery is None


@pytest.mark.asyncio
async def test_cached_workspace_registration_retries_without_blocking(
    monkeypatch,
):
    import asyncio
    from swe.app import multi_agent_manager

    real_sleep = asyncio.sleep
    retry = asyncio.Event()

    async def wait_retry(_delay):
        await retry.wait()

    monkeypatch.setattr(multi_agent_manager.asyncio, "sleep", wait_retry)
    manager = multi_agent_manager.MultiAgentManager()

    class Workspace:
        pass

    workspace = Workspace()
    manager.agents["key"] = workspace
    calls = []

    async def register(value):
        calls.append(value)
        if len(calls) == 1:
            raise ConnectionError("temporarily unavailable")

    manager.set_outbox_register(register)
    await real_sleep(0)
    assert calls == [workspace]
    assert manager.agents["key"] is workspace
    assert "key" not in manager._outbox_registered
    retry.set()
    await real_sleep(0)
    await real_sleep(0)
    assert calls == [workspace, workspace]
    assert manager._outbox_registered["key"]() is workspace
    manager.set_outbox_register(None)


@pytest.mark.asyncio
async def test_start_supervisor_retries_schema_failure_then_succeeds(
    monkeypatch,
):
    import asyncio
    from swe.app import _app

    real_sleep = asyncio.sleep
    retry = asyncio.Event()

    async def wait_retry(_delay):
        await retry.wait()

    monkeypatch.setattr(_app.asyncio, "sleep", wait_retry)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_observability_connection=object(),
            agent_observability_store=object(),
        ),
    )
    calls = []

    async def start(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("database unavailable")
        return True

    monkeypatch.setattr(outbox_runtime, "start_recovery", start)
    task = asyncio.create_task(
        _app._supervise_evidence_recovery(app, object(), object()),
    )
    await real_sleep(0)
    assert len(calls) == 1 and not task.done()
    retry.set()
    await task
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_first_workspace_request_finishes_before_registration_commit():
    import asyncio
    from swe.app.multi_agent_manager import MultiAgentManager

    class Workspace:
        pass

    workspace = Workspace()
    manager = MultiAgentManager()
    manager.agents["agent"] = workspace
    entered = asyncio.Event()
    commit = asyncio.Event()

    async def register(_workspace):
        entered.set()
        await commit.wait()

    manager.set_outbox_register(register)
    await entered.wait()
    assert (
        await asyncio.wait_for(manager.get_agent("agent"), 0.25) is workspace
    )
    registration = manager._outbox_tasks["agent"]
    commit.set()
    await registration
    assert manager._outbox_registered["agent"]() is workspace
    manager.set_outbox_register(None)


@pytest.mark.asyncio
async def test_workspace_registration_retries_in_background(monkeypatch):
    import asyncio
    from swe.app.multi_agent_manager import MultiAgentManager

    class Workspace:
        pass

    workspace = Workspace()
    manager = MultiAgentManager()
    manager.agents["agent"] = workspace
    commit = asyncio.Event()
    retry = asyncio.Event()
    calls = 0

    async def register(_workspace):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("registration unavailable")
        await commit.wait()

    async def retry_delay(seconds):
        assert seconds == 5
        retry.set()

    monkeypatch.setattr(asyncio, "sleep", retry_delay)
    manager.set_outbox_register(register)
    registration = manager._outbox_tasks["agent"]
    await retry.wait()
    assert await manager.get_agent("agent") is workspace
    assert "agent" not in manager._outbox_registered
    commit.set()
    await registration
    assert calls == 2
    assert manager._outbox_registered["agent"]() is workspace
    assert await manager.get_agent("agent") is workspace
    manager.set_outbox_register(None)


@pytest.mark.asyncio
async def test_request_does_not_wait_for_production_supervisor_or_scope_commit(
    monkeypatch,
    tmp_path,
):  # pylint: disable=too-many-statements
    import asyncio
    from contextlib import asynccontextmanager
    from io import StringIO
    from uuid import uuid4
    from agentscope.message import Msg
    from swe.app import _app
    from swe.app.multi_agent_manager import MultiAgentManager
    from swe.observability import outbox_recovery
    from swe.observability.config import EmitterConfig
    from swe.observability.context import bind_context
    from swe.observability.emitter import JsonlEmitter
    from swe.observability.instrumentation import (
        ObservabilityModelWrapper,
        mark_pending_message_evidence,
    )
    from swe.observability.models import Scope
    from swe.observability.outbox import verify_event
    from swe.observability.runner_lifecycle import prepare_query_run

    monkeypatch.setenv("SWE_AGENTOBS_ENABLED", "true")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test-key")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "x" * 32)

    scope = WorkspaceScope("t", "s", "a", service_unit_id="unit")
    scope_dict = dict(
        zip(
            ("service_unit_id", "tenant_id", "source_id", "agent_id"),
            scope.values,
        ),
    )
    schema_read = asyncio.Event()
    commit = asyncio.Event()
    entered = asyncio.Event()
    root_lookup_started = asyncio.Event()
    release_root_lookup = threading.Event()
    loop = asyncio.get_running_loop()
    real_resolve = Path.resolve
    workspace_root = tmp_path.resolve()

    def delayed_resolve(path, *args, **kwargs):
        if path == workspace_root:
            assert threading.current_thread() is not threading.main_thread()
            loop.call_soon_threadsafe(root_lookup_started.set)
            assert release_root_lookup.wait(timeout=3)
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", delayed_resolve)

    class Connection:
        config = SimpleNamespace(database="observability")

        @asynccontextmanager
        async def acquire(self):
            yield self

        @asynccontextmanager
        async def transaction(self):
            yield self
            entered.set()
            await commit.wait()

        async def fetch_all(self, query, *_args):
            await schema_read.wait()
            return (
                [
                    {"COLUMN_NAME": "event_id", "SEQ_IN_INDEX": 1},
                    {"COLUMN_NAME": "processing_state", "SEQ_IN_INDEX": 2},
                ]
                if "STATISTICS" in query
                else []
            )

        async def execute(self, *_args):
            return 1

        async def fetch_one(self, *_args):
            return {
                **scope.fields,
                "workspace_root": str(workspace_root).encode(),
                "storage_authority": "legacy",
            }

    class Scheduler:
        def __init__(self, *_args):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    class Workspace:
        workspace_dir = workspace_root
        agent_id = "a"
        tenant_id = "t"

    monkeypatch.setattr(
        "swe.app.conversation_storage.runtime.resolved_scope",
        lambda *_args, **_kwargs: scope,
    )
    monkeypatch.setattr(outbox_recovery, "OutboxRecoveryScheduler", Scheduler)
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    monkeypatch.setattr(outbox_runtime, "storage_authority", lambda: "legacy")
    monkeypatch.setattr(
        "swe.observability.runner_lifecycle.service_unit_id",
        lambda: "unit",
    )
    connection = Connection()
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_observability_connection=connection,
            agent_observability_store=object(),
        ),
    )
    manager = MultiAgentManager()
    workspace = Workspace()
    manager.agents["agent"] = workspace
    sink = StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=sink)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    request = SimpleNamespace(source_id="s", channel_meta={"chat_id": "chat"})
    question = Msg("user", "private question", "user")

    class Model:
        stream = False

        async def __call__(self, _messages):
            return SimpleNamespace(
                id=str(uuid4()),
                content=[{"type": "text", "text": "internal body"}],
            )

    async def model_answer():
        context = prepare_query_run(workspace, request, [question])
        assert context.scope == Scope("unit", "s", "t", "a")
        with bind_context(context):
            response = await ObservabilityModelWrapper(
                Model(),
                provider_key="provider",
                model_key="model",
            )([])
            message = Msg("assistant", response.content, "assistant")
            message.invocation_id = response.id
            mark_pending_message_evidence(message)
        return message

    supervisor = asyncio.create_task(
        _app._supervise_evidence_recovery(app, manager, None),
    )
    assert (
        await asyncio.wait_for(manager.get_agent("agent"), 0.25) is workspace
    )
    assert not scope_registered(scope_dict, (str(workspace_root), "legacy"))
    schema_read.set()
    await asyncio.wait_for(root_lookup_started.wait(), 0.5)
    try:
        blocked = await asyncio.wait_for(model_answer(), 0.25)
        assert "agent_observability_outbox" not in (blocked.metadata or {})
    finally:
        release_root_lookup.set()
    await entered.wait()
    assert (
        await asyncio.wait_for(manager.get_agent("agent"), 0.25) is workspace
    )
    assert not scope_registered(scope_dict, (str(workspace_root), "legacy"))
    first = await asyncio.wait_for(model_answer(), 0.25)
    assert "agent_observability_outbox" not in (first.metadata or {})
    registration = manager._outbox_tasks["agent"]
    commit.set()
    await registration
    assert scope_registered(scope_dict, (str(workspace_root), "legacy"))
    second = await model_answer()
    envelope = second.metadata["agent_observability_outbox"]
    assert verify_event(envelope)["message_id"] == second.id
    await outbox_runtime.stop_recovery(app, manager)
    await supervisor
    assert emitter.close() == 0
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [
        event["status"]
        for event in events
        if event.get("span_type") == "LLM"
        and event["event_name"] == "span.ended"
    ] == ["succeeded", "succeeded", "succeeded"]
    assert [
        event["saved_state"]
        for event in events
        if event.get("content_type") == "assistant_answer"
    ] == ["not_saved", "not_saved", "pending"]


@pytest.mark.asyncio
async def test_request_degrades_when_supervisor_is_not_ready_then_recovers():
    import asyncio
    from swe.app.multi_agent_manager import MultiAgentManager

    class Workspace:
        pass

    manager = MultiAgentManager()
    workspace = Workspace()
    manager.agents["agent"] = workspace
    ready = asyncio.Event()

    async def initialize():
        await ready.wait()

        async def register(_workspace):
            return None

        manager.set_outbox_register(register)

    supervisor = asyncio.create_task(initialize())
    assert await manager.get_agent("agent") is workspace
    assert "agent" not in manager._outbox_registered
    assert not supervisor.cancelled()
    ready.set()
    await supervisor
    await manager._outbox_tasks["agent"]
    assert await manager.get_agent("agent") is workspace
    assert manager._outbox_registered["agent"]() is workspace
    manager.set_outbox_register(None)


@pytest.mark.asyncio
async def test_disabled_capture_never_waits_for_registration_when_database_blocks(
    monkeypatch,
    tmp_path,
):
    import asyncio
    from contextlib import asynccontextmanager
    from swe.app.multi_agent_manager import MultiAgentManager
    from swe.observability import outbox_recovery

    monkeypatch.setenv("SWE_AGENTOBS_ENABLED", "false")
    blocked = asyncio.Event()
    registration_entered = asyncio.Event()
    registration_release = asyncio.Event()

    class Connection:
        config = SimpleNamespace(database="observability")

        @asynccontextmanager
        async def acquire(self):
            yield self

        @asynccontextmanager
        async def transaction(self):
            registration_entered.set()
            await registration_release.wait()
            yield self

        async def fetch_all(self, query, *_args):
            await blocked.wait()
            return (
                [
                    {"COLUMN_NAME": "event_id", "SEQ_IN_INDEX": 1},
                    {"COLUMN_NAME": "processing_state", "SEQ_IN_INDEX": 2},
                ]
                if "STATISTICS" in query
                else []
            )

    class Scheduler:
        def __init__(self, *_args):
            self.started = False

        async def start(self):
            self.started = True

        async def stop(self):
            self.started = False

    class Workspace:
        workspace_dir = tmp_path

    monkeypatch.setattr("swe.observability.outbox._keys", lambda: None)
    monkeypatch.setattr(outbox_recovery, "OutboxRecoveryScheduler", Scheduler)
    monkeypatch.setattr(
        outbox_runtime,
        "service_unit_from_env",
        lambda: "unit",
    )
    manager = MultiAgentManager()
    workspace = Workspace()
    manager.agents["agent"] = workspace
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_observability_connection=Connection(),
            agent_observability_store=object(),
        ),
    )
    startup = asyncio.create_task(
        outbox_runtime.start_recovery(app, manager, None),
    )
    await asyncio.sleep(0)
    assert (
        await asyncio.wait_for(manager.get_agent("agent"), timeout=0.2)
        is workspace
    )
    assert not startup.done()
    blocked.set()
    assert await startup
    assert app.state.agent_observability_recovery.started
    assert manager._outbox_register is None
    assert not manager._outbox_tasks
    assert (
        await asyncio.wait_for(manager.get_agent("agent"), timeout=0.2)
        is workspace
    )
    await asyncio.sleep(0)
    assert not registration_entered.is_set()
    await outbox_runtime.stop_recovery(app, manager)
