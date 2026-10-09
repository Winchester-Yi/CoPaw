# -*- coding: utf-8 -*-
"""Cron skill_ids 绑定与 Monitor 同步的窄切片回归测试。"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from monitor.app.database.schema import (
    CREATE_CRON_JOBS_TABLE,
    CRON_JOBS_EXTRA_COLUMNS,
)
from monitor.app.models.cron import CronJobSyncRequest
from monitor.app.services.cron import sync_service
from monitor.app.services.cron.sync_service import SyncService
from swe.app.crons import models as cron_models
from swe.app.crons.monitor_sync_client import MonitorSyncClient

CronJobSpec = cron_models.CronJobSpec


def _job_payload(**overrides):
    payload = {
        "id": "job-1",
        "name": "skill cron",
        "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "UTC"},
        "task_type": "agent",
        "request": {"input": {"text": "ping"}},
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {
                "user_id": "user-1",
                "session_id": "session-1",
            },
            "mode": "stream",
            "meta": {},
        },
    }
    payload.update(overrides)
    return payload


def test_cron_job_spec_defaults_skill_ids_to_empty_string():
    job = CronJobSpec.model_validate(_job_payload())

    assert job.skill_ids == ""


def test_cron_job_spec_normalizes_skill_ids_string_and_list_values():
    job = CronJobSpec.model_validate(
        _job_payload(skill_ids="a, b\nc a"),
    )
    list_job = CronJobSpec.model_validate(
        _job_payload(skill_ids=["alpha", "beta gamma", "alpha"]),
    )

    assert job.skill_ids == "a,b,c"
    assert list_job.skill_ids == "alpha,beta,gamma"


def test_cron_job_spec_accepts_chinese_skill_ids():
    job = CronJobSpec.model_validate(
        _job_payload(skill_ids="存款到期客户评分, 授信预警"),
    )
    helper = getattr(cron_models, "cron_skill_ids_contains", None)

    assert job.skill_ids == "存款到期客户评分,授信预警"
    assert helper(job.skill_ids, "存款到期客户评分") is True
    assert helper(job.skill_ids, "存款") is False


def test_cron_job_spec_rejects_invalid_skill_id():
    with pytest.raises(ValidationError, match="skill_ids"):
        CronJobSpec.model_validate(_job_payload(skill_ids="ok,bad/id"))


def test_cron_job_spec_rejects_skill_ids_longer_than_200_chars():
    too_long = ",".join([f"s{i:03d}" for i in range(60)])

    with pytest.raises(ValidationError, match="200"):
        CronJobSpec.model_validate(_job_payload(skill_ids=too_long))


def test_cron_skill_ids_contains_uses_exact_comma_boundaries():
    helper = getattr(cron_models, "cron_skill_ids_contains", None)

    assert helper is not None
    assert helper("foo,bar", "fo") is False
    assert helper("foo,bar", "bar") is True


def test_model_copy_preserves_skill_ids_for_broadcast_style_copies():
    job = CronJobSpec.model_validate(_job_payload(skill_ids="foo bar"))

    copied = job.model_copy(update={"id": "job-2"})

    assert copied.skill_ids == "foo,bar"
    assert copied.model_dump(mode="json")["skill_ids"] == "foo,bar"


def test_workflow_task_uses_skills_without_an_agent_request():
    job = CronJobSpec.model_validate(
        _job_payload(
            task_type="workflow",
            request={"input": {"text": "stale Agent prompt"}},
            text="stale fixed text",
            skill_ids="first,second",
            model_slot={"provider_id": "provider", "model": "model"},
        ),
    )

    assert job.task_type == "workflow"
    assert job.skill_ids == "first,second"
    assert job.request is None
    assert job.text is None
    assert job.model_slot is None
    assert job.workflow_binding_id is None


def test_workflow_task_requires_a_skill_or_explicit_binding():
    with pytest.raises(ValidationError, match="workflow"):
        CronJobSpec.model_validate(
            _job_payload(task_type="workflow", request=None),
        )


def test_non_workflow_task_cannot_keep_workflow_binding():
    with pytest.raises(ValidationError, match="workflow_binding_id"):
        CronJobSpec.model_validate(
            _job_payload(workflow_binding_id="binding-1"),
        )


def test_monitor_sync_client_job_payload_includes_normalized_skill_ids():
    job = CronJobSpec.model_validate(_job_payload(skill_ids="a, b\nc a"))

    payload = MonitorSyncClient("")._build_job_sync_data(job)

    assert payload["skill_ids"] == "a,b,c"


def test_monitor_sync_client_job_payload_includes_workflow_binding_only_for_workflow():
    workflow = CronJobSpec.model_validate(
        _job_payload(
            task_type="workflow",
            request=None,
            skill_ids="skill-a",
            workflow_binding_id="binding-1",
        ),
    )
    agent = CronJobSpec.model_validate(_job_payload())

    client = MonitorSyncClient("")
    assert client._build_job_sync_data(workflow)["workflow_binding_id"] == (
        "binding-1"
    )
    assert client._build_job_sync_data(agent)["workflow_binding_id"] is None


def test_monitor_schema_declares_skill_ids_column_and_migration():
    assert "skill_ids" in CREATE_CRON_JOBS_TABLE
    assert "skill_ids" in CRON_JOBS_EXTRA_COLUMNS
    assert "VARCHAR(200)" in CRON_JOBS_EXTRA_COLUMNS["skill_ids"]


def test_monitor_schema_declares_workflow_binding_column_and_migration():
    assert "workflow_binding_id" in CREATE_CRON_JOBS_TABLE
    assert "workflow_binding_id" in CRON_JOBS_EXTRA_COLUMNS


def test_monitor_sync_contract_restricts_binding_to_workflow_jobs():
    with pytest.raises(ValidationError, match="workflow_binding_id"):
        CronJobSyncRequest.model_validate(
            {
                **_sync_request().model_dump(),
                "workflow_binding_id": "binding-1",
            },
        )
    with pytest.raises(ValidationError, match="workflow_binding_id"):
        CronJobSyncRequest.model_validate(
            {**_sync_request().model_dump(), "task_type": "workflow"},
        )


class _FakeDb:
    def __init__(self, existing=None):
        self.existing = existing
        self.executed: list[tuple[str, tuple]] = []

    async def fetch_one(self, sql, params=None):
        if "FROM swe_cron_jobs WHERE id" in sql:
            return self.existing
        return None

    async def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or ())))


def _sync_request() -> CronJobSyncRequest:
    return CronJobSyncRequest(
        id="job-1",
        name="skill cron",
        tenant_id="tenant-1",
        tenant_name="Tenant",
        bbk_id="1001",
        source_id="source-1",
        enabled=True,
        task_type="agent",
        cron_expr="0 9 * * *",
        timezone="UTC",
        channel="console",
        target_user_id="user-1",
        target_session_id="session-1",
        skill_ids="foo,bar",
    )


@pytest.mark.asyncio
async def test_monitor_sync_insert_sql_writes_skill_ids(monkeypatch):
    fake_db = _FakeDb(existing=None)
    monkeypatch.setattr(sync_service, "get_db_connection", lambda: fake_db)
    monkeypatch.setattr(
        sync_service,
        "_get_beijing_now",
        lambda: datetime(2026, 6, 18, 9, 0, 0),
    )

    async def _identity(request):
        return request

    monkeypatch.setattr(sync_service, "_enrich_sync_request", _identity)

    await SyncService().sync_job(_sync_request())

    sql, params = fake_db.executed[0]
    assert "skill_ids" in sql
    assert "foo,bar" in params
    assert sql.count("%s") == len(params)


@pytest.mark.asyncio
async def test_monitor_sync_update_sql_writes_skill_ids(monkeypatch):
    fake_db = _FakeDb(existing={"id": "job-1", "deleted_at": None})
    monkeypatch.setattr(sync_service, "get_db_connection", lambda: fake_db)
    monkeypatch.setattr(
        sync_service,
        "_get_beijing_now",
        lambda: datetime(2026, 6, 18, 9, 0, 0),
    )

    async def _identity(request):
        return request

    monkeypatch.setattr(sync_service, "_enrich_sync_request", _identity)

    await SyncService().sync_job(_sync_request())

    sql, params = fake_db.executed[0]
    assert "skill_ids" in sql
    assert "foo,bar" in params
    assert sql.count("%s") == len(params)


@pytest.mark.asyncio
async def test_monitor_sync_workflow_definition_writes_binding_id(monkeypatch):
    fake_db = _FakeDb(existing=None)
    monkeypatch.setattr(sync_service, "get_db_connection", lambda: fake_db)
    monkeypatch.setattr(
        sync_service,
        "_get_beijing_now",
        lambda: datetime(2026, 6, 18, 9, 0, 0),
    )

    async def _identity(request):
        return request

    monkeypatch.setattr(sync_service, "_enrich_sync_request", _identity)
    request = _sync_request().model_copy(
        update={"task_type": "workflow", "workflow_binding_id": "binding-1"},
    )

    await SyncService().sync_job(request)

    sql, params = fake_db.executed[0]
    assert "workflow_binding_id" in sql
    assert "binding-1" in params
    assert sql.count("%s") == len(params)
