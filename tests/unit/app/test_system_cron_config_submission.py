# -*- coding: utf-8 -*-
"""配置先保存，刷新失败可见，保存路径不提前解析将被后台重读的配置。"""

from types import SimpleNamespace
import importlib
from unittest.mock import AsyncMock

import pytest

router = importlib.import_module("swe.app.source_system_config.router")
from swe.app.source_system_config.models import (
    CurrentSourceSystemConfigResponse,
    CurrentSourceSystemConfigUpdateRequest,
    SourceSystemConfig,
)


@pytest.mark.asyncio
async def test_source_save_keeps_saved_data_and_reports_refresh_submission_failure():
    saved = CurrentSourceSystemConfigResponse(
        source_id="bank",
        config=SourceSystemConfig.model_validate({}),
        version=2,
    )
    service = SimpleNamespace(
        upsert_current_source_config=AsyncMock(return_value=saved),
        resolve_config=AsyncMock(
            side_effect=AssertionError("no config snapshot read"),
        ),
    )
    failed = SimpleNamespace(
        scheduler_sync={
            "submission_status": "failed",
            "error_code": "system_sync_not_submitted",
        },
    )
    scheduler = SimpleNamespace(
        refresh_task_session_cleanup=AsyncMock(return_value=failed),
        refresh_archive_maintenance=AsyncMock(return_value=failed),
    )
    request = SimpleNamespace(
        headers={
            "X-User-Id": "alice",
            "X-Tenant-Id": "alice",
            "X-User-Role": "manager",
        },
        state=SimpleNamespace(source_id="bank", tenant_id="alice"),
        app=SimpleNamespace(
            state=SimpleNamespace(
                source_system_config_service=service,
                source_system_task_scheduler=scheduler,
            ),
        ),
    )
    payload = CurrentSourceSystemConfigUpdateRequest(
        config=SourceSystemConfig.model_validate({}),
    )
    response = await router.upsert_current_source_system_config(
        payload,
        request,
    )
    assert response.version == 2
    assert response.scheduler_sync["submission_status"] == "failed"
    service.upsert_current_source_config.assert_awaited_once()
    service.resolve_config.assert_not_awaited()
    assert (
        scheduler.refresh_task_session_cleanup.call_args.kwargs[
            "identity"
        ].role
        == "manager"
    )
