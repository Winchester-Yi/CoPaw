"""Workflow binding resolution is versioned and source-scoped."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from swe.app.crons.workflow.config_store import WorkflowConfigStore
from swe.app.crons.models import CronJobSpec
from swe.app.crons.api import _bind_workflow_config
from swe.app.crons.api import publish_workflow_binding
from fastapi import HTTPException


class _FakeDb:
    is_connected = True

    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def fetch_one(self, sql, params):
        self.queries.append((sql, params))
        if "FROM swe_workflow_bindings" in sql:
            return {
                "binding_id": "binding-1",
                "source_id": "RMASSIST",
                "skill_id": "skill-a",
                "current_version": 3,
                "enabled": 1,
            }
        if "FROM swe_workflow_binding_versions" in sql:
            return {
                "binding_id": "binding-1",
                "version": params[1],
                "skill_id": "skill-a",
                "source_id": "RMASSIST",
                "enabled": 1,
                "config_json": json.dumps(
                    {
                        "url": "https://workflow.example/run",
                        "method": "POST",
                        "result_fields": {"result": ["data", "result"]},
                        "provider_id": "p",
                        "model_id": "m",
                    },
                ),
            }
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_resolve_first_skill_uses_current_published_version():
    store = WorkflowConfigStore(_FakeDb())

    config = await store.resolve_first_skill("RMASSIST", "skill-a,skill-b")

    assert config.binding_id == "binding-1"
    assert config.skill_id == "skill-a"
    assert config.version == 3
    assert config.provider_id == "p"
    assert config.model_id == "m"


@pytest.mark.asyncio
async def test_explicit_version_loads_immutable_version():
    db = _FakeDb()
    store = WorkflowConfigStore(db)

    config = await store.get_version("binding-1", 2)

    assert config.version == 2
    assert db.queries[-1][1] == ("binding-1", 2)


@pytest.mark.asyncio
async def test_binding_cannot_be_used_from_another_source():
    store = WorkflowConfigStore(_FakeDb())

    with pytest.raises(ValueError, match="source"):
        await store.get_version(
            "binding-1",
            2,
            source_id="another-source",
        )


@pytest.mark.asyncio
async def test_workflow_job_binds_first_skill_configuration():
    class _Store:
        async def resolve_first_skill(self, source_id, skill_ids):
            assert (source_id, skill_ids) == ("RMASSIST", "skill-a,skill-b")
            return type("Binding", (), {"binding_id": "binding-1"})()

    job = CronJobSpec.model_validate(
        {
            "id": "job-1",
            "name": "workflow",
            "source_id": "RMASSIST",
            "schedule": {"cron": "0 9 * * *"},
            "task_type": "workflow",
            "skill_ids": "skill-a,skill-b",
            "dispatch": {
                "target": {"user_id": "user-1", "session_id": "session-1"},
            },
        },
    )

    bound = await _bind_workflow_config(job, _Store())

    assert bound.workflow_binding_id == "binding-1"


@pytest.mark.asyncio
async def test_publish_writes_immutable_version_and_moves_pointer():
    class _Cursor:
        statements = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, sql, params):
            self.statements.append((sql, params))

        async def fetchone(self):
            return ("binding-1", 0)

    class _Conn:
        committed = False
        rolled_back = False

        async def begin(self):
            return None

        def cursor(self):
            return cursor

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    class _PublishDb:
        is_connected = True

        @asynccontextmanager
        async def acquire(self):
            yield conn

    cursor = _Cursor()
    conn = _Conn()
    store = WorkflowConfigStore(
        _PublishDb(),
        allowed_hosts={"workflow.example"},
    )

    config = await store.publish(
        source_id="RMASSIST",
        skill_id="skill-a",
        definition={
            "url": "https://workflow.example/run",
            "headers": {
                "API-Key": {"source": "literal", "value": "test-key"},
            },
            "result_fields": {"result": ["data", "result"]},
            "provider_id": "provider-a",
            "model_id": "model-a",
        },
    )

    assert config.version == 1
    assert conn.committed is True
    assert conn.rolled_back is False
    assert len(cursor.statements) == 4
    assert "INSERT IGNORE INTO swe_workflow_bindings" in (
        cursor.statements[0][0]
    )
    assert (
        "INSERT INTO swe_workflow_binding_versions" in cursor.statements[2][0]
    )
    assert "UPDATE swe_workflow_bindings" in cursor.statements[3][0]
    assert config.binding_id == cursor.statements[2][1][0]
    stored_config = json.loads(cursor.statements[2][1][3])
    assert stored_config["headers"]["API-Key"]["value"] == "test-key"

    with pytest.raises(ValueError, match="renderer"):
        await store.publish(
            source_id="RMASSIST",
            skill_id="skill-a",
            definition={
                "url": "https://workflow.example/run",
                "result_fields": {"result": ["data", "result"]},
                "provider_id": "provider-a",
                "model_id": "model-a",
                "renderer_key": "not_registered",
            },
        )


@pytest.mark.asyncio
async def test_publish_endpoint_requires_manager_and_current_source():
    published = []

    class _Store:
        async def publish(self, **kwargs):
            published.append(kwargs)
            return SimpleNamespace(binding_id="binding-1")

    state = SimpleNamespace(source_id="RMASSIST")
    request = SimpleNamespace(
        headers={"X-User-Role": "manager", "X-User-Id": "admin-1"},
        state=state,
        app=SimpleNamespace(
            state=SimpleNamespace(workflow_config_store=_Store())
        ),
    )
    definition = {"url": "https://workflow.example/run"}

    result = await publish_workflow_binding("skill-a", definition, request)

    assert result.binding_id == "binding-1"
    assert published == [
        {
            "source_id": "RMASSIST",
            "skill_id": "skill-a",
            "definition": definition,
        },
    ]
    request.headers = {"X-User-Role": "user"}
    with pytest.raises(HTTPException) as error:
        await publish_workflow_binding("skill-a", definition, request)
    assert error.value.status_code == 403
