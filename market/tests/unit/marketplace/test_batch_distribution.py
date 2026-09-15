# -*- coding: utf-8 -*-
"""批量技能/MCP分发接口测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market.app.routers import api_router
from market.app.routers.distribution import _status
from market.marketplace.models import MarketItem


def _make_app(tmp_path):
    db = AsyncMock()
    db.is_connected = True
    db.execute = AsyncMock(return_value=1)
    db.execute_many = AsyncMock(return_value=1)
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])

    marketplace = AsyncMock()
    marketplace.db = db
    marketplace.marketplace_root = tmp_path / "market"
    marketplace._resolve_target_users = AsyncMock(  # noqa: SLF001
        return_value=[
            {
                "tenant_id": "tenant-a",
                "tenant_name": "用户A",
                "bbk_id": "100",
            },
            {
                "tenant_id": "tenant-b",
                "tenant_name": "用户B",
                "bbk_id": "101",
            },
        ],
    )

    def find_market_item(source_id, item_ref):
        del source_id
        return MarketItem(
            item_id=item_ref,
            item_type="skill" if item_ref.startswith("skill") else "mcp",
            name=item_ref,
            skill_id=item_ref if item_ref.startswith("skill") else "",
            client_key=item_ref if item_ref.startswith("mcp") else "",
            creator_id="admin",
        )

    marketplace.find_market_item = find_market_item

    app = FastAPI()
    app.state.marketplace = marketplace
    app.include_router(api_router, prefix="/api")
    return app, db


def _headers():
    return {
        "X-Source-Id": "src1",
        "X-Manager": "true",
        "X-User-Id": "admin",
        "X-User-Name": "admin",
    }


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["queued", "succeeded"], "running"),
        (["queued", "failed"], "running"),
        (["running", "succeeded"], "running"),
        (["succeeded", "failed"], "partial_failed"),
    ],
)
def test_batch_status_keeps_unfinished_tasks_in_progress(
    statuses,
    expected,
):
    """批次含未完成任务时不能提前汇总为失败。"""
    assert _status([{"status": status} for status in statuses]) == expected


@pytest.mark.asyncio
async def test_batch_distribution_creates_one_task_per_item_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    """相同 batch_id 和请求重复提交时应返回原任务，不重复创建。"""
    app, db = _make_app(tmp_path)
    scheduled = []
    monkeypatch.setattr(
        "market.app.routers.distribution.asyncio.create_task",
        lambda coro: coro.close() or scheduled.append(coro) or object(),
    )
    monkeypatch.setattr(
        "market.app.routers.distribution.load_index",
        lambda root, source_id: [
            MarketItem(
                item_id="skill-1",
                item_type="skill",
                name="skill-1",
                creator_id="admin",
            ),
            MarketItem(
                item_id="skill-2",
                item_type="skill",
                name="skill-2",
                creator_id="admin",
            ),
            MarketItem(
                item_id="mcp-1",
                item_type="mcp",
                name="mcp-1",
                client_key="mcp-1",
                creator_id="admin",
            ),
        ],
    )

    client = TestClient(app)
    payload = {
        "batch_id": "batch-1",
        "skill_item_ids": ["skill-1", "skill-2", "skill-1"],
        "mcp_item_ids": ["mcp-1", "mcp-1"],
        "target_tenant_ids": ["tenant-a", "tenant-b", "tenant-a"],
    }

    first = client.post(
        "/api/market/distributions",
        json=payload,
        headers=_headers(),
    )
    assert first.status_code == 200
    assert len(first.json()["task_ids"]) == 3
    assert [
        (task["resource_type"], task["item_id"])
        for task in first.json()["tasks"]
    ] == [
        ("skill", "skill-1"),
        ("skill", "skill-2"),
        ("mcp", "mcp-1"),
    ]
    assert first.json()["status"] == "queued"

    db.fetch_all.side_effect = [
        [
            {
                "task_id": task_id,
                "batch_id": "batch-1",
                "task_type": (
                    "market.skill.distribute"
                    if index < 2
                    else "market.mcp.distribute"
                ),
                "status": "queued",
            }
            for index, task_id in enumerate(first.json()["task_ids"])
        ],
    ]
    second = client.post(
        "/api/market/distributions",
        json=payload,
        headers=_headers(),
    )
    assert second.status_code == 200
    assert second.json()["task_ids"] == first.json()["task_ids"]
    assert second.json()["reused"] is True
    assert (
        len(
            [
                call
                for call in db.execute.await_args_list
                if "INSERT INTO swe_async_tasks" in call.args[0]
            ],
        )
        == 3
    )


def test_batch_distribution_rejects_same_batch_id_with_different_request(
    tmp_path,
):
    """同 batch_id 但请求内容变化时返回冲突。"""
    app, db = _make_app(tmp_path)
    db.fetch_all = AsyncMock(
        return_value=[
            {
                "task_id": "task-1",
                "batch_id": "batch-1",
                "task_type": "market.skill.distribute",
                "status": "queued",
            },
        ],
    )
    import market.app.routers.distribution as distribution_router

    distribution_router.load_index = lambda root, source_id: [
        MarketItem(
            item_id="skill-1",
            item_type="skill",
            name="skill-1",
            creator_id="admin",
        ),
    ]
    client = TestClient(app)
    response = client.post(
        "/api/market/distributions",
        json={
            "batch_id": "batch-1",
            "skill_item_ids": ["skill-1"],
            "mcp_item_ids": [],
            "target_tenant_ids": ["tenant-a"],
        },
        headers=_headers(),
    )
    assert response.status_code == 409


def test_batch_distribution_query_returns_tasks_for_multiple_batch_ids(
    tmp_path,
):
    """批量批次查询返回每个批次的任务。"""
    app, db = _make_app(tmp_path)
    db.fetch_all = AsyncMock(
        return_value=[
            {
                "task_id": "task-1",
                "batch_id": "batch-1",
                "service": "market",
                "task_type": "market.skill.distribute",
                "status": "succeeded",
                "title": "技能分发",
                "summary": "summary",
                "source_id": "src1",
                "actor_user_id": "admin",
                "actor_user_name": "admin",
                "target_count": 1,
                "done_count": 1,
                "failed_count": 0,
                "error_message": None,
                "result_json": '{"item_id": "skill-1"}',
                "created_at": None,
                "updated_at": None,
                "finished_at": None,
            },
            {
                "task_id": "task-2",
                "batch_id": "batch-2",
                "service": "market",
                "task_type": "market.mcp.distribute",
                "status": "running",
                "title": "MCP 分发",
                "summary": "summary",
                "source_id": "src1",
                "actor_user_id": "admin",
                "actor_user_name": "admin",
                "target_count": 1,
                "done_count": 0,
                "failed_count": 0,
                "error_message": None,
                "result_json": '{"item_id": "mcp-1"}',
                "created_at": None,
                "updated_at": None,
                "finished_at": None,
            },
        ],
    )
    client = TestClient(app)
    response = client.get(
        "/api/market/distributions?batchids=batch-1&batchids=batch-2",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert [item["batch_id"] for item in response.json()["batches"]] == [
        "batch-1",
        "batch-2",
    ]
    assert response.json()["batches"][0]["tasks"][0]["task_id"] == "task-1"
    assert response.json()["batches"][1]["tasks"][0]["item_id"] == "mcp-1"
    query_sql = db.fetch_all.await_args.args[0]
    assert "service = 'market'" in query_sql
    assert (
        "task_type IN ('market.skill.distribute', 'market.mcp.distribute')"
        in query_sql
    )
