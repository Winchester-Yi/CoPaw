# -*- coding: utf-8 -*-
"""技能/MCP 批量分发腿单元测试：清单汇总、批次幂等、提交/查询与状态聚合。"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.app.wealth_plans import market_dispatch
from swe.app.wealth_plans import router as wealth_router
from swe.app.wealth_plans.market_dispatch import (
    build_batch_id,
    collect_distribution_items,
    query_batch_status,
    submit_distribution,
)
from swe.app.wealth_plans.models import (
    PlanSceneRecord,
    PlanTargetRecord,
    WealthPlanRecord,
)
from swe.app.wealth_plans.store import WealthPlanStore

VIEWER = {"X-User-Id": "zhangwl", "X-Bbk-Id": "100"}


def make_scene(**overrides) -> PlanSceneRecord:
    scene = PlanSceneRecord(
        scene_id="skill-wealth-insurance-1",
        scene_name="保障潜客经营",
        category="insurance",
        cron_expr="0 9 * * *",
        item_id="item-wealth-insurance-1",
        mcp_relations=["mcp-customer", "mcp-insurance"],
    )
    for key, value in overrides.items():
        setattr(scene, key, value)
    return scene


def make_plan(
    targets: list[str] | None = None,
    scenes: list[PlanSceneRecord] | None = None,
) -> WealthPlanRecord:
    return WealthPlanRecord(
        id="plan-abc123def456",
        sap_id="zhangwl",
        creator_name="张**",
        bbk_id="100",
        source_id="RMASSIST",
        name="九月计划",
        scenes=scenes if scenes is not None else [make_scene()],
        targets=[PlanTargetRecord(sap_id=t) for t in (targets or ["chenjy"])],
    )


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# 清单汇总与批次 id
# ---------------------------------------------------------------------------


def test_collect_items_dedup_union_and_include_creator() -> None:
    plan = make_plan(
        targets=["chenjy", "liuxt"],
        scenes=[
            make_scene(),
            make_scene(
                scene_id="skill-wealth-finance-3",
                item_id="item-wealth-finance-3",
                mcp_relations=["mcp-customer", "mcp-wealth"],
            ),
            make_scene(scene_id="no-item", item_id=None, mcp_relations=[]),
        ],
    )

    skill_ids, mcp_ids, tenant_ids = collect_distribution_items(plan)

    assert skill_ids == ["item-wealth-finance-3", "item-wealth-insurance-1"]
    assert mcp_ids == ["mcp-customer", "mcp-insurance", "mcp-wealth"]
    # 创建人本人也运行源任务，必须一并收到技能/MCP
    assert tenant_ids == ["chenjy", "liuxt", "zhangwl"]


def test_batch_id_stable_for_same_content_and_changes_on_diff() -> None:
    plan = make_plan()
    args = collect_distribution_items(plan)
    first = build_batch_id(plan, *args)
    second = build_batch_id(plan, *args)

    assert first == second
    assert first.startswith("wealth-plan-plan-abc123def456-")
    assert len(first) <= 64

    changed = build_batch_id(
        plan,
        args[0],
        args[1],
        ["chenjy", "liuxt", "zhangwl"],
    )
    assert changed != first


# ---------------------------------------------------------------------------
# 提交与查询
# ---------------------------------------------------------------------------


async def test_submit_skipped_when_no_items() -> None:
    plan = make_plan(scenes=[make_scene(item_id=None, mcp_relations=[])])

    assert await submit_distribution(plan) is None


async def test_submit_skipped_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWE_MARKET_API_BASE_URL", raising=False)

    assert await submit_distribution(make_plan()) is None


async def test_submit_posts_batch_and_returns_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_MARKET_API_BASE_URL", "http://market.local")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={"batch_id": "b", "status": "queued", "task_ids": []},
        )

    monkeypatch.setattr(
        market_dispatch,
        "_new_client",
        lambda: mock_client(handler),
    )

    outcome = await submit_distribution(make_plan())

    assert outcome is not None
    batch_id, status = outcome
    assert batch_id.startswith("wealth-plan-plan-abc123def456-")
    assert status == "queued"
    assert captured["headers"]["X-Source-Id"] == "RMASSIST"
    assert captured["headers"]["X-Manager"] == "true"
    assert captured["headers"]["X-User-Id"] == "zhangwl"
    # 中文名按 UTF-8 百分号编码（HTTP 头仅支持 ASCII）
    assert captured["headers"]["X-User-Name"] == "%E5%BC%A0%2A%2A"
    assert '"skill_item_ids":["item-wealth-insurance-1"]' in captured["body"]
    assert (
        '"mcp_item_ids":["mcp-customer","mcp-insurance"]' in captured["body"]
    )
    assert '"target_tenant_ids":["chenjy","zhangwl"]' in captured["body"]


async def test_submit_raises_on_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_MARKET_API_BASE_URL", "http://market.local")
    monkeypatch.setattr(
        market_dispatch,
        "_new_client",
        lambda: mock_client(
            lambda _req: httpx.Response(404, json={"detail": "no"}),
        ),
    )

    with pytest.raises(RuntimeError, match="404"):
        await submit_distribution(make_plan())


async def test_query_batch_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWE_MARKET_API_BASE_URL", "http://market.local")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.query.decode()
        return httpx.Response(
            200,
            json={"batches": [{"batch_id": "b", "status": "partial_failed"}]},
        )

    monkeypatch.setattr(
        market_dispatch,
        "_new_client",
        lambda: mock_client(handler),
    )

    status = await query_batch_status("RMASSIST", "b")

    assert status == "partial_failed"
    assert "batchids=b" in captured["query"]


async def test_query_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWE_MARKET_API_BASE_URL", raising=False)

    assert await query_batch_status("RMASSIST", "b") is None


# ---------------------------------------------------------------------------
# 发布编排接入
# ---------------------------------------------------------------------------


async def test_run_publish_records_skill_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import swe.app.wealth_plans.publish as publish_module

    store = WealthPlanStore()
    plan = make_plan()
    await store.create(plan)

    async def fake_scenes(_request, _store, _plan) -> None:
        return None

    async def fake_submit(_plan):
        return ("wealth-plan-x-deadbeef", "queued")

    monkeypatch.setattr(publish_module, "_publish_all_scenes", fake_scenes)
    monkeypatch.setattr(publish_module, "submit_distribution", fake_submit)

    await publish_module._run_publish(None, store, plan.id)  # type: ignore[arg-type]

    record = await store.get(plan.id)
    assert record is not None
    assert record.status == "published"
    assert record.skill_dispatch_task_id == "wealth-plan-x-deadbeef"
    assert record.skill_dispatch_status == "queued"


async def test_run_publish_skill_failure_keeps_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import swe.app.wealth_plans.publish as publish_module

    store = WealthPlanStore()
    plan = make_plan()
    await store.create(plan)

    async def fake_scenes(_request, _store, _plan) -> None:
        return None

    async def fake_submit(_plan):
        raise RuntimeError("market down")

    monkeypatch.setattr(publish_module, "_publish_all_scenes", fake_scenes)
    monkeypatch.setattr(publish_module, "submit_distribution", fake_submit)

    await publish_module._run_publish(None, store, plan.id)  # type: ignore[arg-type]

    record = await store.get(plan.id)
    assert record is not None
    # 定时任务腿已成功，整体仍发布成功；技能腿独立标记失败
    assert record.status == "published"
    assert record.skill_dispatch_status == "failed"
    assert record.skill_dispatch_error == "market down"


# ---------------------------------------------------------------------------
# 看板状态聚合
# ---------------------------------------------------------------------------


def make_app(
    store: WealthPlanStore,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setattr(
        wealth_router,
        "launch_publish",
        lambda _request, _store, _plan_id: None,
    )
    application = FastAPI()
    application.state.wealth_plan_store = store

    @application.middleware("http")
    async def _bbk_state(request, call_next):
        request.state.bbk_id = request.headers.get("X-Bbk-Id")
        return await call_next(request)

    application.include_router(wealth_router.router, prefix="/api")
    return application


async def seed_published_plan(store: WealthPlanStore) -> str:
    plan = make_plan()
    plan.status = "published"
    await store.create(plan)
    return plan.id


async def test_board_status_failed_when_skill_leg_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WealthPlanStore()
    plan_id = await seed_published_plan(store)
    await store.set_skill_dispatch(plan_id, "batch-1", "failed", "boom")
    app = make_app(store, monkeypatch)

    items = TestClient(app).get("/api/wealth/plans", headers=VIEWER).json()

    assert items["items"][0]["board_status"] == "分发失败"


async def test_board_status_distributing_while_skill_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WealthPlanStore()
    plan_id = await seed_published_plan(store)
    await store.set_skill_dispatch(plan_id, "batch-1", "queued", None)

    async def fake_query(_source_id: str, _batch_id: str) -> str | None:
        return "running"

    monkeypatch.setattr(wealth_router, "query_batch_status", fake_query)
    app = make_app(store, monkeypatch)

    items = TestClient(app).get("/api/wealth/plans", headers=VIEWER).json()

    assert items["items"][0]["board_status"] == "分发中"
    record = await store.get(plan_id)
    assert record is not None and record.skill_dispatch_status == "running"


async def test_board_status_distributed_after_skill_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WealthPlanStore()
    plan_id = await seed_published_plan(store)
    await store.set_skill_dispatch(plan_id, "batch-1", "queued", None)

    async def fake_query(_source_id: str, _batch_id: str) -> str | None:
        return "succeeded"

    monkeypatch.setattr(wealth_router, "query_batch_status", fake_query)
    app = make_app(store, monkeypatch)

    items = TestClient(app).get("/api/wealth/plans", headers=VIEWER).json()

    assert items["items"][0]["board_status"] == "已自动下发"


async def test_board_status_ignores_skill_leg_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WealthPlanStore()
    await seed_published_plan(store)
    app = make_app(store, monkeypatch)

    items = TestClient(app).get("/api/wealth/plans", headers=VIEWER).json()

    assert items["items"][0]["board_status"] == "已自动下发"
