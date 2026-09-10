# -*- coding: utf-8 -*-
"""Tenant-aware error push regression tests for CronManager."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
sys.path.insert(0, str(SRC_ROOT))

_ORIGINAL_MODULES = {
    name: sys.modules.get(name)
    for name in [
        "swe.app.crons.repo.base",
        "swe.app.crons.executor",
        "swe.app.crons.heartbeat",
        "swe.config",
        "swe.config.context",
        "swe.app.console_push_store",
        "swe.app.channels.schema",
        "swe.app.crons.models",
        "swe.app.crons.manager",
    ]
}

repo_module = types.ModuleType("swe.app.crons.repo.base")
repo_module.BaseJobRepository = object
sys.modules["swe.app.crons.repo.base"] = repo_module

executor_module = types.ModuleType("swe.app.crons.executor")
executor_module.CronExecutor = lambda runner, channel_manager: object()
sys.modules["swe.app.crons.executor"] = executor_module

heartbeat_module = types.ModuleType("swe.app.crons.heartbeat")
heartbeat_module.is_cron_expression = lambda every: False
heartbeat_module.parse_heartbeat_cron = lambda every: ("*", "*", "*", "*", "*")
heartbeat_module.parse_heartbeat_every = lambda every: 60


async def _run_heartbeat_once(**kwargs):
    return None


heartbeat_module.run_heartbeat_once = _run_heartbeat_once
sys.modules["swe.app.crons.heartbeat"] = heartbeat_module

config_module = types.ModuleType("swe.config")
config_module.__path__ = [str(SRC_ROOT / "swe" / "config")]
config_module.get_heartbeat_config = (
    lambda agent_id=None: types.SimpleNamespace(
        enabled=False,
        every="60s",
    )
)
sys.modules["swe.config"] = config_module

push_calls = []
push_module = types.ModuleType("swe.app.console_push_store")


async def _append(session_id, text, *, sticky=False, tenant_id=None):
    push_calls.append(
        {
            "session_id": session_id,
            "text": text,
            "sticky": sticky,
            "tenant_id": tenant_id,
        },
    )


push_module.append = _append
sys.modules["swe.app.console_push_store"] = push_module

channels_schema_module = types.ModuleType("swe.app.channels.schema")
channels_schema_module.ChannelType = str
channels_schema_module.DEFAULT_CHANNEL = "console"
sys.modules["swe.app.channels.schema"] = channels_schema_module

models_spec = importlib.util.spec_from_file_location(
    "swe.app.crons.models",
    SRC_ROOT / "swe" / "app" / "crons" / "models.py",
)
assert models_spec is not None and models_spec.loader is not None
models_module = importlib.util.module_from_spec(models_spec)
sys.modules["swe.app.crons.models"] = models_module
models_spec.loader.exec_module(models_module)

manager_spec = importlib.util.spec_from_file_location(
    "swe.app.crons.manager",
    SRC_ROOT / "swe" / "app" / "crons" / "manager.py",
)
assert manager_spec is not None and manager_spec.loader is not None
manager_module = importlib.util.module_from_spec(manager_spec)
sys.modules["swe.app.crons.manager"] = manager_module
manager_spec.loader.exec_module(manager_module)

for _name, _module in _ORIGINAL_MODULES.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module

CronManager = manager_module.CronManager
CronJobSpec = models_module.CronJobSpec
ScheduleSpec = models_module.ScheduleSpec
DispatchSpec = models_module.DispatchSpec
DispatchTarget = models_module.DispatchTarget
JobRuntimeSpec = models_module.JobRuntimeSpec
CronJobRequest = models_module.CronJobRequest


class _Task:
    def cancelled(self):
        return False

    def exception(self):
        return RuntimeError("boom")

    def get_name(self):
        return "cron-run-job-1"


def test_task_done_cb_pushes_error_to_tenant_scoped_store():
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
    )
    job = CronJobSpec(
        id="job-1",
        name="tenant cron",
        tenant_id="tenant-a",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(
            input=[{"content": [{"type": "text", "text": "ping"}]}],
        ),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={},
        ),
        runtime=JobRuntimeSpec(),
    )

    manager._task_done_cb(_Task(), job)
    asyncio.run(asyncio.sleep(0))

    assert push_calls[0]["tenant_id"] == "tenant-a"
    assert push_calls[0]["session_id"] == "session-a"


def test_task_done_cb_skips_error_already_sent_by_executor():
    push_calls.clear()
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
    )
    job = CronJobSpec(
        id="job-1",
        name="tenant cron",
        tenant_id="tenant-a",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(
            input=[{"content": [{"type": "text", "text": "ping"}]}],
        ),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={},
        ),
        runtime=JobRuntimeSpec(),
    )

    class _Task:
        def cancelled(self):
            return False

        def exception(self):
            exc = RuntimeError("already sent")
            exc.cron_execution_meta = {
                "terminal_notification_sent": True,
            }
            return exc

        def get_name(self):
            return "cron-run-job-1"

    manager._task_done_cb(_Task(), job)
    asyncio.run(asyncio.sleep(0))

    assert push_calls == []
