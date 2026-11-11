# -*- coding: utf-8 -*-
"""业务重试冻结当前定义/已发布工作流，不改原执行快照，也不打开批入口。"""

import json

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)


@pytest.mark.asyncio
async def test_business_retry_uses_current_workflow_version_while_batch_entry_paused(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    old = await db.fetch_one("SELECT * FROM swe_cron_execution_runs")
    definition = await db.fetch_one("SELECT * FROM swe_cron_jobs_v2")
    spec = json.loads(definition["spec_json"])
    spec.update(task_type="workflow", workflow_binding_id="wf-current")
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET definition_version=2,spec_json=?",
        (json.dumps(spec),),
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET dispatch_state='batch_paused'",
    )
    db.conn.executescript("""
        CREATE TABLE swe_workflow_bindings (binding_id TEXT PRIMARY KEY,source_id TEXT,current_version INTEGER,enabled INTEGER);
        CREATE TABLE swe_workflow_binding_versions (binding_id TEXT,version INTEGER,config_json TEXT);
        INSERT INTO swe_workflow_bindings VALUES ('wf-current','bank',2,1);
        INSERT INTO swe_workflow_binding_versions VALUES ('wf-current',2,'{"provider_id":"new-provider","model_id":"new-model"}');
    """)
    service = BatchRetryService(db)
    preview = await service.preview(caller, batch)
    assert preview["items"][0]["execution_definition_version"] == 1
    assert preview["items"][0]["definition_version"] == 2
    result = await service.retry(caller, batch, retry_body(preview))
    assert result["queued"] == 1
    run = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id<>%s",
        (old["execution_id"],),
    )
    meta = json.loads(run["dispatch_meta_json"])
    assert meta["workflow_config_version"] == 2
    assert run["provider_id"] == "new-provider"
    assert run["model_id"] == "new-model"
    assert run["definition_version"] == 2
    assert run["scheduled_fire_at"] == old["scheduled_fire_at"]
    assert (
        await db.fetch_one(
            "SELECT spec_json FROM swe_cron_execution_runs WHERE execution_id=%s",
            (old["execution_id"],),
        )
    )["spec_json"] == old["spec_json"]
    assert (
        await db.fetch_one("SELECT dispatch_state FROM swe_cron_job_states")
    )["dispatch_state"] == "batch_paused"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type,queued",
    [("Prompt", 1), ("Business,Prompt", 1), ("Business", 0), (None, 0)],
)
async def test_manual_retry_requires_prompt_for_async_failure(
    callback_setup,
    error_type,
    queued,
):
    from tests.unit.scheduler.test_managed_batch_retry import (
        failed_batch,
        retry_body,
    )
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    await db.execute(
        "UPDATE swe_cron_execution_runs SET status='success',settled_status='error',settled_error_code='cron_async_subtasks_failed'",
    )
    await db.execute(
        "UPDATE swe_cron_executions SET status='success',async_status='error',error_type=%s",
        (error_type,),
    )
    service = BatchRetryService(db)
    result = await service.retry(
        caller,
        batch,
        retry_body(await service.preview(caller, batch)),
    )
    assert result["queued"] == queued
    if not queued:
        assert result["results"][0]["reason"] == "cron_retry_non_model_failure"
