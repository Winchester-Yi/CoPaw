from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"broadcast_source_job_id": "p"},
        {
            "broadcast_dispatch_intents_enabled": True,
            "broadcast_source_job_id": "p",
        },
    ],
)
async def test_normal_jobs_and_children_cannot_operate_batch_control(meta):
    from src.swe.app.crons.batch_operations import get_batch_run_state

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [
                (b"x-user-role", b"manager"),
                (b"x-user-id", b"admin"),
                (b"x-source-id", b"source-a"),
            ],
        }
    )
    job = SimpleNamespace(
        id="job", source_id="source-a", tenant_id="owner", meta=meta
    )
    mgr = SimpleNamespace(get_job=AsyncMock(return_value=job))
    with pytest.raises(HTTPException) as exc:
        await get_batch_run_state(request, "job", mgr)
    assert exc.value.status_code == 400


def test_swe_contract_rejects_loose_boolean_and_identity_override():
    from src.swe.app.crons.batch_operations import BatchRunStateUpdate
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BatchRunStateUpdate(paused="false", expected_version=1)
    with pytest.raises(ValidationError):
        BatchRunStateUpdate(paused=True, expected_version=1, tenant_id="other")


def test_non_manager_is_rejected_before_run_state_workspace_resolution():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.swe.app.crons.batch_operations import router, get_cron_manager

    def forbidden_workspace():
        raise AssertionError("unauthorized request resolved a workspace")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_cron_manager] = forbidden_workspace
    with TestClient(app, raise_server_exceptions=False) as client:
        assert (
            client.get(
                "/cron/jobs/parent/batch-dispatch/run-state"
            ).status_code
            == 403
        )
