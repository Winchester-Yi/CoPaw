# -*- coding: utf-8 -*-
"""真实 ASGI 发送顺序验证维护先响应、后处理，后台错误不伪装成功。"""

import asyncio
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from scheduler.app.routers import managed_cron_date_ranges as api


@pytest.mark.asyncio
async def test_acceptance_is_sent_before_date_maintenance_finishes():
    started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Maintenance:
        async def apply(self, source):
            assert source == "bank"
            started.set()
            await release.wait()
            return {"source_id": source, "failed_count": 0}

    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.date_service] = Maintenance

    async def observed(scope, receive, send):
        async def capture(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body",
            ):
                sent.set()

        await app(scope, receive, capture)

    async with AsyncClient(
        transport=ASGITransport(app=observed),
        base_url="http://test",
    ) as client:
        request = asyncio.create_task(
            client.post(
                "/api/scheduler/cron/date-ranges/apply",
                json={"source_id": "bank"},
            ),
        )
        try:
            await asyncio.wait_for(sent.wait(), 2)
            await asyncio.wait_for(started.wait(), 2)
            assert not release.is_set()
        finally:
            release.set()
            response = await request
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "source_id": "bank"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["partial", "exception"])
async def test_background_failure_is_logged_without_changing_acceptance(
    failure,
    caplog,
):
    class Maintenance:
        async def apply(self, source):
            if failure == "exception":
                raise RuntimeError("private-maintenance-error")
            return {"source_id": source, "failed_count": 1, "scanned_count": 2}

    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.date_service] = Maintenance
    caplog.set_level(logging.INFO, logger=api.__name__)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/scheduler/cron/date-ranges/apply",
            json={"source_id": "bank"},
        )
        missing = await client.post(
            "/api/scheduler/cron/date-ranges/apply",
            json={},
        )
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "source_id": "bank"}
    assert missing.status_code == 422
    assert any(
        record.name == api.__name__
        and record.levelno >= logging.WARNING
        and "source_id=bank" in record.getMessage()
        for record in caplog.records
    )
    assert "private-maintenance-error" not in caplog.text
