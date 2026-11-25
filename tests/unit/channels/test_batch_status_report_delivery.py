# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from swe.app.channels.zhaohu.channel import ZhaohuChannel
from swe.app.crons.monitor_sync_client import MonitorSyncClient
from swe.app.routers import batch_status_report as report_api


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"returnCode": "SUC0000", "body": [{"expMsgId": "123"}]}, "sent"),
        ({"returnCode": "ERR001"}, "failed"),
        ({}, "outcome_unknown"),
        (httpx.ReadTimeout("timeout"), "outcome_unknown"),
    ],
)
async def test_batch_report_push_is_attempted_once(reply, expected):
    channel = ZhaohuChannel.from_config(
        report_api.report_channel_inbound,
        {
            "enabled": True,
            "push_url": "https://zhaohu.example/push",
            "sys_id": "report",
            "robot_open_id": "robot",
        },
    )
    channel._query_user_info_by_sap = AsyncMock(
        return_value={"sapId": "00001234", "ystId": "target"},
    )
    channel.crit_answer = AsyncMock(return_value="report")
    channel._post_push_payload = AsyncMock()
    if isinstance(reply, Exception):
        channel._post_push_payload.side_effect = reply
    else:
        channel._post_push_payload.return_value = reply

    result = await channel.send_batch_status_report(
        "00001234",
        "report",
        "delivery",
    )

    assert result["status"] == expected
    channel._post_push_payload.assert_awaited_once()
    payload = channel._post_push_payload.await_args.args[0]
    assert payload["baseInfo"]["sendAddrs"][0]["sendAddr"] == "target"
    assert payload["msgContent"]["message"] == [
        {"type": "txt", "value": [{"type": "txt", "text": "report"}]},
    ]
    if expected == "sent":
        assert result["receipt_ids"] == ["123"]


@pytest.mark.asyncio
async def test_batch_report_never_pushes_when_address_belongs_to_another_employee():
    channel = object.__new__(ZhaohuChannel)
    channel._validate_send_configuration = lambda *_: True
    channel._query_user_info_by_sap = AsyncMock(
        return_value={"sapId": "00005678", "ystId": "wrong-employee"},
    )
    channel._post_push_payload = AsyncMock()
    result = await channel.send_batch_status_report(
        "00001234",
        "report",
        "delivery",
    )
    assert result == {
        "status": "failed",
        "error_code": "zhaohu_address_unresolved",
    }
    channel._post_push_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_route_reads_source_default_channel_and_reuses_token(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SWE_INTERNAL_TOKEN", "test-secret")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    loaded = []

    def source_config_path(tenant):
        assert tenant == "default_S"
        return config_path

    def load_agent(agent_id, *, config_path):
        loaded.append((agent_id, config_path))
        return SimpleNamespace(
            channels=SimpleNamespace(zhaohu="existing-config"),
        )

    sender = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr(
        report_api,
        "get_tenant_storage_config_path",
        source_config_path,
    )
    monkeypatch.setattr(report_api, "load_agent_config", load_agent)
    monkeypatch.setattr(
        report_api.ZhaohuChannel,
        "from_config",
        lambda _, config: SimpleNamespace(send_batch_status_report=sender),
    )
    app = FastAPI()
    app.include_router(report_api.router, prefix="/api")
    delivery = str(uuid4())
    body = {
        "report_id": str(uuid4()),
        "delivery_id": delivery,
        "source_id": "S",
        "part_no": 1,
        "recipient_id": "00001234",
        "text": "report",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://swe",
    ) as client:
        path = "/api/internal/cron/batch-status-reports/send-part"
        assert (await client.post(path, json=body)).status_code == 401
        sender.assert_not_awaited()
        response = await client.post(
            path,
            json=body,
            headers={"X-Internal-Token": "Bearer test-secret"},
        )
    assert response.json() == {"status": "sent"}
    sender.assert_awaited_once_with(
        "00001234",
        "report",
        delivery,
    )
    assert loaded == [("default", config_path)]


@pytest.mark.asyncio
async def test_snapshot_uses_existing_monitor_url_without_new_token(
    monkeypatch,
):
    monkeypatch.delenv("SWE_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("SWE_MONITOR_API_URL", "http://monitor.example/api")
    monitor = MonitorSyncClient()

    def downstream(request):
        assert (
            str(request.url)
            == "http://monitor.example/api/monitor/internal/cron/batch-status-report-snapshot"
        )
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"batches": []})

    monitor._client = httpx.AsyncClient(
        base_url=monitor._base_url,
        transport=httpx.MockTransport(downstream),
    )
    monkeypatch.setattr(report_api, "get_monitor_sync_client", lambda: monitor)
    app = FastAPI()
    app.include_router(report_api.router, prefix="/api")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://swe",
        ) as client:
            response = await client.post(
                "/api/internal/cron/batch-status-reports/snapshot",
                json={"source_id": "S", "business_date": "2026-11-24"},
            )
        assert response.json() == {"batches": []}
    finally:
        await monitor.close()
