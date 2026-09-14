# -*- coding: utf-8 -*-
"""Market 异步任务提交接口测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market.app.routers import mcp_market as mcp_router
from market.app.routers import skills_market as skills_router
from market.database.connection import DatabaseConnection
from market.marketplace.schemas import (
    DistributeRequest,
    DistributeResponse,
    DistributeTenantResult,
    MCPDistributionRequest,
    MCPDistributionResponse,
    MCPDistributionTenantResult,
    PublishMCPRequest,
)
from market.marketplace.service import MarketplaceService


def _make_app(tmp_path):
    """构造带市场服务的测试应用。"""
    mock_db = AsyncMock(spec=DatabaseConnection)
    mock_db.is_connected = True
    mock_db.execute = AsyncMock(return_value=1)
    mock_db.execute_many = AsyncMock(return_value=1)
    mock_db.fetch_one = AsyncMock(return_value=None)
    mock_db.fetch_all = AsyncMock(return_value=[])

    svc = MarketplaceService(
        db=mock_db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    app = FastAPI()
    app.state.marketplace = svc
    app.state.db = mock_db
    from market.app.routers import api_router

    app.include_router(api_router, prefix="/api")
    return app


@pytest.mark.asyncio
async def test_distribute_skill_returns_task_submission(tmp_path, monkeypatch):
    """技能分发应返回受理中的任务。"""
    from market.marketplace.schemas import PublishSkillRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    svc._resolve_target_users = AsyncMock(  # noqa: SLF001
        return_value=[
            {
                "tenant_id": "tenant-a",
                "tenant_name": "用户A",
                "bbk_id": "100",
            },
        ],
    )
    await svc.publish_skill(
        "src1",
        PublishSkillRequest(
            name="skill-a",
            description="",
            creator_id="alice",
            creator_name="Alice",
            skill_json={},
            skill_md="",
        ),
    )

    monkeypatch.setattr(
        skills_router.asyncio,
        "create_task",
        lambda coro: coro.close() or object(),
    )

    client = TestClient(app)
    resp = client.post(
        "/api/market/skills/skill-a/distribute",
        json={"target_type": "all", "target_values": []},
        headers={
            "X-Source-Id": "src1",
            "X-Manager": "true",
            "X-User-Id": "admin",
            "X-User-Name": "admin",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    task_id = resp.json()["task_id"]
    assert task_id
    task_insert_call = next(
        call
        for call in app.state.db.execute.await_args_list
        if "INSERT INTO swe_async_tasks" in call.args[0]
    )
    assert task_insert_call.args[1][6] == "分发技能「skill-a」，目标 1 个用户"
    item_insert_call = next(
        call
        for call in app.state.db.execute_many.await_args_list
        if "INSERT IGNORE INTO swe_async_task_items" in call.args[0]
    )
    assert item_insert_call.args[1] == [
        (task_id, "tenant-a", "用户A", "queued", None, None),
    ]


@pytest.mark.asyncio
async def test_distribute_skill_accepts_skill_id_reference(
    tmp_path,
    monkeypatch,
):
    """技能分发路径传 skill_id 时仍应归一化到市场 item_id。"""
    from market.marketplace.schemas import PublishSkillRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    svc._resolve_target_users = AsyncMock(  # noqa: SLF001
        return_value=[
            {
                "tenant_id": "tenant-a",
                "tenant_name": "用户A",
                "bbk_id": "100",
            },
        ],
    )
    skill_id = "de587f2c-c44c-4e5a-84b6-7a7278bda901"
    item, _ = await svc.publish_skill(
        "src1",
        PublishSkillRequest(
            name="skill-a",
            description="",
            creator_id="alice",
            creator_name="Alice",
            skill_id=skill_id,
            skill_json={},
            skill_md="",
        ),
    )

    scheduled: dict[str, Any] = {}

    def capture_task(coro):
        """记录后台协程参数并关闭协程，避免测试真实分发。"""
        scheduled["item_id"] = coro.cr_frame.f_locals["item_id"]
        coro.close()
        return object()

    monkeypatch.setattr(skills_router.asyncio, "create_task", capture_task)

    client = TestClient(app)
    resp = client.post(
        f"/api/market/skills/{skill_id}/distribute",
        json={"target_type": "all", "target_values": []},
        headers={
            "X-Source-Id": "src1",
            "X-Manager": "true",
            "X-User-Id": "admin",
            "X-User-Name": "admin",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert scheduled["item_id"] == item.item_id
    task_insert_call = next(
        call
        for call in app.state.db.execute.await_args_list
        if "INSERT INTO swe_async_tasks" in call.args[0]
    )
    assert task_insert_call.args[1][6] == "分发技能「skill-a」，目标 1 个用户"


@pytest.mark.asyncio
async def test_distribute_mcp_returns_task_submission(tmp_path, monkeypatch):
    """MCP 分发应返回受理中的任务。"""
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    item, _ = await svc.publish_mcp(
        "src1",
        PublishMCPRequest(
            client_key="mcp-a",
            name="demo",
            description="demo",
            creator_id="alice",
            creator_name="Alice",
            config={"name": "demo", "transport": "stdio", "command": "/a"},
            version="1.0.0",
        ),
    )

    monkeypatch.setattr(
        mcp_router.asyncio,
        "create_task",
        lambda coro: coro.close() or object(),
    )

    client = TestClient(app)
    resp = client.post(
        f"/api/market/mcp/{item.item_id}/distribute",
        json={"target_tenant_ids": ["tenant-a"], "overwrite": True},
        headers={
            "X-Source-Id": "src1",
            "X-Manager": "true",
            "X-User-Id": "admin",
            "X-User-Name": "admin",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["task_id"]
    task_insert_call = next(
        call
        for call in app.state.db.execute.await_args_list
        if "INSERT INTO swe_async_tasks" in call.args[0]
    )
    assert task_insert_call.args[1][6] == "分发 MCP「demo」，目标 1 个用户"


@pytest.mark.asyncio
async def test_distribute_mcp_accepts_client_key_reference(
    tmp_path,
    monkeypatch,
):
    """MCP 分发路径传 client_key 时仍应归一化到市场 item_id。"""
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    item, _ = await svc.publish_mcp(
        "src1",
        PublishMCPRequest(
            client_key="mcp-a",
            name="demo",
            description="demo",
            creator_id="alice",
            creator_name="Alice",
            config={"name": "demo", "transport": "stdio", "command": "/a"},
            version="1.0.0",
        ),
    )

    scheduled: dict[str, Any] = {}

    def capture_task(coro):
        """记录后台协程参数并关闭协程，避免测试真实分发。"""
        scheduled["item_id"] = coro.cr_frame.f_locals["item_id"]
        coro.close()
        return object()

    monkeypatch.setattr(mcp_router.asyncio, "create_task", capture_task)

    client = TestClient(app)
    resp = client.post(
        "/api/market/mcp/mcp-a/distribute",
        json={"target_tenant_ids": ["tenant-a"], "overwrite": True},
        headers={
            "X-Source-Id": "src1",
            "X-Manager": "true",
            "X-User-Id": "admin",
            "X-User-Name": "admin",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert scheduled["item_id"] == item.item_id
    task_insert_call = next(
        call
        for call in app.state.db.execute.await_args_list
        if "INSERT INTO swe_async_tasks" in call.args[0]
    )
    assert task_insert_call.args[1][6] == "分发 MCP「demo」，目标 1 个用户"


@pytest.mark.asyncio
async def test_mcp_distribution_records_failed_item_target_name() -> None:
    """MCP 失败明细也应保留可回填目标名称的结果。"""

    class FakeStore:
        """记录后台任务写入参数的任务表替身。"""

        def __init__(self) -> None:
            self.items: list[dict[str, Any]] = []

        async def mark_running(self, task_id: str) -> None:
            assert task_id == "task-1"

        async def record_item_result(self, **kwargs: Any) -> None:
            self.items.append(kwargs)

        async def finish_task(self, **kwargs: Any) -> None:
            assert kwargs["status"] == "failed"

    class FakeService:
        """返回带用户名称的 MCP 分发结果。"""

        async def distribute_mcp(
            self,
            *args: Any,
            **kwargs: Any,
        ) -> MCPDistributionResponse:
            del args, kwargs
            return MCPDistributionResponse(
                source_agent_id="mcp-a",
                results=[
                    MCPDistributionTenantResult(
                        tenant_id="tenant-a",
                        tenant_name="用户A",
                        success=False,
                        error="failed",
                    ),
                ],
            )

    store = FakeStore()

    await mcp_router._run_mcp_distribution_task(  # noqa: SLF001
        task_id="task-1",
        store=store,
        svc=FakeService(),
        source_id="src1",
        item_id="mcp-a",
        operator_id="admin",
        operator_name="admin",
        req=MCPDistributionRequest(target_tenant_ids=["tenant-a"]),
    )

    assert store.items[0]["result"]["tenant_name"] == "用户A"
    assert store.items[0]["error_message"] == "failed"


@pytest.mark.asyncio
async def test_mcp_distribution_exception_keeps_item_id_in_task_result() -> (
    None
):
    """MCP 分发异常兜底时，任务结果也应保留 item_id。"""

    class FakeStore:
        """记录后台任务写入参数的任务表替身。"""

        def __init__(self) -> None:
            self.finished: dict[str, Any] | None = None

        async def mark_running(self, task_id: str) -> None:
            del task_id

        async def record_item_result(self, **kwargs: Any) -> None:
            del kwargs

        async def finish_task(self, **kwargs: Any) -> None:
            self.finished = kwargs

    class FakeService:
        """抛出 MCP 分发异常的服务替身。"""

        async def distribute_mcp(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("service unavailable")

    store = FakeStore()

    await mcp_router._run_mcp_distribution_task(  # noqa: SLF001
        task_id="task-1",
        store=store,
        svc=FakeService(),
        source_id="src1",
        item_id="mcp-a",
        operator_id="admin",
        operator_name="admin",
        req=MCPDistributionRequest(target_tenant_ids=["tenant-a"]),
    )

    assert store.finished is not None
    assert store.finished["result"] == {
        "item_id": "mcp-a",
        "status": "failed",
        "error": "service unavailable",
    }


@pytest.mark.asyncio
async def test_skill_distribution_records_failed_item_per_result() -> None:
    """技能分发明细应使用逐用户结果，避免复制异常用户被标为成功。"""

    class FakeStore:
        """记录后台任务写入参数的任务表替身。"""

        def __init__(self) -> None:
            self.items: list[dict[str, Any]] = []
            self.finished: dict[str, Any] | None = None

        async def mark_running(self, task_id: str) -> None:
            assert task_id == "task-1"

        async def record_item_result(self, **kwargs: Any) -> None:
            self.items.append(kwargs)

        async def finish_task(self, **kwargs: Any) -> None:
            self.finished = kwargs

    class FakeService:
        """返回一成一败的技能分发结果。"""

        async def distribute_skill(
            self,
            *args: Any,
            **kwargs: Any,
        ) -> DistributeResponse:
            del args, kwargs
            return DistributeResponse(
                distributed_count=1,
                failed_count=1,
                conflict_count=0,
                item_id="skill-a",
                results=[
                    DistributeTenantResult(
                        user_id="tenant-a",
                        success=True,
                        status="distributed",
                    ),
                    DistributeTenantResult(
                        user_id="tenant-b",
                        success=False,
                        status="failed",
                        error="copy failed",
                    ),
                ],
            )

    store = FakeStore()

    await skills_router._run_skill_distribution_task(  # noqa: SLF001
        task_id="task-1",
        store=store,
        svc=FakeService(),
        source_id="src1",
        item_id="skill-a",
        operator_id="admin",
        operator_name="admin",
        req=DistributeRequest(
            target_type="user_id",
            target_values=["tenant-a"],
        ),
        target_user_ids=["tenant-a", "tenant-b"],
    )

    assert [item["success"] for item in store.items] == [True, False]
    assert [item["item_status"] for item in store.items] == [
        "succeeded",
        "failed",
    ]
    assert store.items[1]["target_id"] == "tenant-b"
    assert store.items[1]["error_message"] == "copy failed"
    assert store.finished is not None
    assert store.finished["status"] == "partial_failed"
    assert store.finished["done_count"] == 2
    assert store.finished["failed_count"] == 1
    assert store.finished["result"]["item_id"] == "skill-a"


@pytest.mark.asyncio
async def test_skill_distribution_exception_keeps_item_id_in_task_result() -> (
    None
):
    """技能分发异常兜底时，任务结果也应保留 item_id。"""

    class FakeStore:
        """记录后台任务写入参数的任务表替身。"""

        def __init__(self) -> None:
            self.finished: dict[str, Any] | None = None

        async def mark_running(self, task_id: str) -> None:
            del task_id

        async def record_item_result(self, **kwargs: Any) -> None:
            del kwargs

        async def finish_task(self, **kwargs: Any) -> None:
            self.finished = kwargs

    class FakeService:
        """抛出技能分发异常的服务替身。"""

        async def distribute_skill(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("service unavailable")

    store = FakeStore()

    await skills_router._run_skill_distribution_task(  # noqa: SLF001
        task_id="task-1",
        store=store,
        svc=FakeService(),
        source_id="src1",
        item_id="skill-a",
        operator_id="admin",
        operator_name="admin",
        req=DistributeRequest(
            target_type="user_id",
            target_values=["tenant-a"],
        ),
        target_user_ids=["tenant-a"],
    )

    assert store.finished is not None
    assert store.finished["result"]["item_id"] == "skill-a"
