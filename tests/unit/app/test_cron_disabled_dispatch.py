from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, None])
async def test_callback_only_reports_skip_for_explicit_false(accepted):
    from src.swe.app.routers.internal import _run_job_callback
    from starlette.requests import Request

    mgr = SimpleNamespace(
        get_job=AsyncMock(return_value=SimpleNamespace(meta={})),
        run_job=AsyncMock(return_value=accepted),
    )
    response = await _run_job_callback(
        Request({"type": "http", "headers": []}),
        mgr,
        {
            "callback_source": "dispatch_service",
            "dispatch_intent_id": 7,
            "dispatch_batch_id": "batch",
            "dispatch_attempt": 1,
        },
        "owner",
        "source",
        "default",
        "job",
        "job-id",
    )
    if accepted is False:
        assert response["skipped"] == "job_disabled"
    else:
        assert response is None


@pytest.mark.asyncio
async def test_disabled_run_reports_no_background_execution():
    from src.swe.app.crons.manager import CronManager

    mgr = object.__new__(CronManager)
    mgr._repo = SimpleNamespace(
        get_job=AsyncMock(return_value=SimpleNamespace(enabled=False))
    )
    assert await mgr.run_job("disabled") is False
