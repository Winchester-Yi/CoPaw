from fastapi import FastAPI
from fastapi.testclient import TestClient

from scheduler.app.routers.batch_operations import router


def test_internal_operations_require_identity_without_a_token(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = "/scheduler/cron/dispatch/batches/batch-a/retry"
    monkeypatch.delenv("SWE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("SCHEDULER_SWE_INTERNAL_TOKEN", raising=False)
    assert (
        client.post(
            path, json={"candidates": [{"id": 1, "attempt_count": 1}]}
        ).status_code
        == 400
    )
    monkeypatch.setenv("SWE_INTERNAL_TOKEN", "test-only-secret")
    assert (
        client.post(
            path,
            json={"candidates": [{"id": 1, "attempt_count": 1}]},
            headers={"X-Internal-Token": "Bearer test-only-secret"},
        ).status_code
        == 400
    )
    headers = {
        "X-Source-Id": "s",
        "X-User-Id": "alice",
    }
    assert (
        client.post(
            path,
            headers=headers,
            json={"candidates": [{"id": 1, "attempt_count": -1}]},
        ).status_code
        == 422
    )
