# -*- coding: utf-8 -*-
"""wealth plans 路由单元测试：CRUD 权限、状态聚合与场景查询兜底。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.app.crons.broadcast_task_store import CronBroadcastTaskStore
from swe.app.wealth_plans import router as wealth_router
from swe.app.wealth_plans.store import WealthPlanStore

VIEWER = {"X-User-Id": "zhangwl"}


@pytest.fixture()
def store() -> WealthPlanStore:
    return WealthPlanStore()


@pytest.fixture()
def app(
    store: WealthPlanStore,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    launches: list[str] = []
    cascades: list[str] = []

    def fake_launch(request, _store, plan_id: str) -> None:
        launches.append(plan_id)

    async def fake_cascade(request, _store, plan) -> None:
        cascades.append(plan.id)

    monkeypatch.setattr(wealth_router, "launch_publish", fake_launch)
    monkeypatch.setattr(wealth_router, "cascade_delete", fake_cascade)

    application = FastAPI()
    application.state.wealth_plan_store = store
    application.state.wealth_plan_launches = launches
    application.state.wealth_plan_cascades = cascades
    application.include_router(wealth_router.router, prefix="/api")
    return application


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def plan_payload(**overrides) -> dict:
    payload = {
        "name": "九月重点客户经营计划",
        "source_label": "行长关注",
        "scenes": [
            {
                "scene_id": "skill-wealth-insurance-1",
                "scene_name": "保障潜客经营",
                "category": "insurance",
                "direction": "挖掘高潜保险客户",
                "cycle": "本月",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "cron_expr": "0 9 * * *",
                "mcp_relations": ["mcp-customer"],
            },
        ],
        "targets": [{"sap_id": "chenjy", "name": "陈**"}],
    }
    payload.update(overrides)
    return payload


def test_create_plan_persists_and_launches_publish(
    client: TestClient,
    app: FastAPI,
) -> None:
    resp = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["publish_status"] == "publishing"
    assert app.state.wealth_plan_launches == [body["id"]]


def test_list_visible_to_creator_and_target_only(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    creator_items = client.get("/api/wealth/plans", headers=VIEWER).json()
    target_items = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "chenjy"},
    ).json()
    outsider_items = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "wangly"},
    ).json()

    assert [p["id"] for p in creator_items["items"]] == [created["id"]]
    assert creator_items["items"][0]["editable"] is True
    assert creator_items["items"][0]["board_status"] == "发布中"
    assert [p["id"] for p in target_items["items"]] == [created["id"]]
    assert target_items["items"][0]["editable"] is False
    assert outsider_items["items"] == []


def test_get_detail_forbidden_for_outsider(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.get(
        f"/api/wealth/plans/{created['id']}",
        headers={"X-User-Id": "wangly"},
    )

    assert resp.status_code == 403


def test_update_rejected_while_publishing(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=plan_payload(name="改名"),
        headers=VIEWER,
    )

    assert resp.status_code == 409


async def test_update_relaunches_after_published(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")

    resp = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=plan_payload(name="十月计划"),
        headers=VIEWER,
    )

    assert resp.status_code == 200
    assert app.state.wealth_plan_launches == [created["id"], created["id"]]
    record = await store.get(created["id"])
    assert record is not None and record.name == "十月计划"


def test_update_forbidden_for_target(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.put(
        f"/api/wealth/plans/{created['id']}",
        json=plan_payload(),
        headers={"X-User-Id": "chenjy"},
    )

    assert resp.status_code == 403


def test_delete_rejected_while_publishing(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()

    resp = client.delete(f"/api/wealth/plans/{created['id']}", headers=VIEWER)

    assert resp.status_code == 409


async def test_delete_cascades_and_removes(
    client: TestClient,
    app: FastAPI,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")

    forbidden = client.delete(
        f"/api/wealth/plans/{created['id']}",
        headers={"X-User-Id": "chenjy"},
    )
    resp = client.delete(f"/api/wealth/plans/{created['id']}", headers=VIEWER)

    assert forbidden.status_code == 403
    assert resp.status_code == 200
    assert app.state.wealth_plan_cascades == [created["id"]]
    assert (
        client.get("/api/wealth/plans", headers=VIEWER).json()["items"] == []
    )


def test_scene_skills_rejects_unknown_category(client: TestClient) -> None:
    resp = client.get("/api/wealth/scene-skills?category=unknown")

    assert resp.status_code == 400


def test_scene_skills_empty_when_external_absent(
    client: TestClient,
) -> None:
    """外部接口未配置/不可达时返回空列表，不做假数据兜底。"""
    resp = client.get("/api/wealth/scene-skills?category=insurance")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_scene_skills_allows_empty_category(client: TestClient) -> None:
    """category 为空串表示查询全部大类，不再 400。"""
    resp = client.get("/api/wealth/scene-skills?category=")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


async def _make_broadcast_store(
    task_status: str,
    results: list[dict] | None = None,
    targets: list[str] | None = None,
) -> tuple[CronBroadcastTaskStore, str]:
    store = CronBroadcastTaskStore()
    snapshot, _ = await store.start_task(
        agent_id="default",
        source_id="RMASSIST",
        tenant_id="zhangwl",
        job_id="job-1",
        target_tenant_ids=targets or ["chenjy"],
    )
    for result in results or []:
        await store.record_target_result(snapshot.task_id, result)
    if task_status == "completed":
        await store.finish_task(snapshot.task_id)
    elif task_status == "failed":
        await store.record_task_failed(snapshot.task_id, "boom")
    return store, snapshot.task_id


async def test_board_status_aggregates_broadcast(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")
    broadcast_store, task_id = await _make_broadcast_store(
        "completed",
        results=[{"tenant_id": "chenjy", "success": True}],
    )
    await store.set_scene_links(
        created["id"],
        "skill-wealth-insurance-1",
        "job-1",
        task_id,
    )
    app.state.cron_broadcast_task_store = broadcast_store

    creator_view = client.get("/api/wealth/plans", headers=VIEWER).json()
    target_view = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "chenjy"},
    ).json()

    assert creator_view["items"][0]["board_status"] == "已自动下发"
    assert target_view["items"][0]["board_status"] == "已自动下发"


async def test_board_status_recipient_failure(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(
            targets=[
                {"sap_id": "chenjy", "name": "陈**"},
                {"sap_id": "liuxt", "name": "刘**"},
            ],
        ),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")
    broadcast_store, task_id = await _make_broadcast_store(
        "completed",
        results=[
            {"tenant_id": "chenjy", "success": True},
            {"tenant_id": "liuxt", "success": False},
        ],
        targets=["chenjy", "liuxt"],
    )
    await store.set_scene_links(
        created["id"],
        "skill-wealth-insurance-1",
        "job-1",
        task_id,
    )
    app.state.cron_broadcast_task_store = broadcast_store

    creator_view = client.get("/api/wealth/plans", headers=VIEWER).json()
    chenjy_view = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "chenjy"},
    ).json()
    liuxt_view = client.get(
        "/api/wealth/plans",
        headers={"X-User-Id": "liuxt"},
    ).json()

    assert creator_view["items"][0]["board_status"] == "分发失败"
    assert chenjy_view["items"][0]["board_status"] == "已自动下发"
    assert liuxt_view["items"][0]["board_status"] == "分发失败"


async def test_board_status_distributing_while_running(
    app: FastAPI,
    client: TestClient,
    store: WealthPlanStore,
) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers=VIEWER,
    ).json()
    await store.set_publish_status(created["id"], "published")
    broadcast_store, task_id = await _make_broadcast_store("running")
    await store.set_scene_links(
        created["id"],
        "skill-wealth-insurance-1",
        "job-1",
        task_id,
    )
    app.state.cron_broadcast_task_store = broadcast_store

    creator_view = client.get("/api/wealth/plans", headers=VIEWER).json()

    assert creator_view["items"][0]["board_status"] == "分发中"
