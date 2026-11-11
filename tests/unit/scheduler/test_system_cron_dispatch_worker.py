# -*- coding: utf-8 -*-
"""系统传输重投复用ID，最终结果只从共库读取。"""

from unittest.mock import AsyncMock

import pytest

from scheduler.app.services.cron.system.dispatch_worker import (
    SystemDispatchWorker,
)
from tests.unit.scheduler.test_system_cron_callbacks import ready
from tests.unit.scheduler.test_system_cron_management import USER, system_setup


@pytest.mark.asyncio
async def test_dispatch_sends_only_existing_run_and_does_not_wait_for_result(
    system_setup,
):
    store, job, body = await ready(system_setup)
    run = await store.accept(USER, body)
    swe = AsyncMock()
    swe.execute.return_value = {
        "accepted": True,
        "execution_id": run["execution_id"],
        "status": "accepted",
    }
    assert (
        await SystemDispatchWorker(store, system_setup[1], swe).run_once() == 1
    )
    sent_job, sent_run = swe.execute.call_args.args
    assert sent_job["job_id"] == job["job_id"]
    assert sent_run["execution_id"] == run["execution_id"]
    assert (await store.get(run["execution_id"]))["status"] == "dispatching"


@pytest.mark.asyncio
async def test_transport_loss_after_swe_accept_does_not_override_shared_result(
    system_setup,
):
    from swe.app.crons.system_results import SystemResultWriter

    db, _, now = system_setup
    store, job, body = await ready(system_setup)
    run = await store.accept(USER, body)
    writer = SystemResultWriter(db, clock=lambda: now[0])

    async def execute(sent_job, sent_run):
        await writer.accept(
            sent_run["execution_id"],
            job["job_id"],
            sent_run["dispatch_owner"],
        )
        raise TimeoutError()

    swe = AsyncMock()
    swe.execute.side_effect = execute
    await SystemDispatchWorker(store, system_setup[1], swe).run_once()
    assert (await store.get(run["execution_id"]))["status"] == "accepted"
