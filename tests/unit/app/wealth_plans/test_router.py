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

    @application.middleware("http")
    async def _bbk_state(request, call_next):  # 模拟租户中间件的 bbk 注入
        bbk = request.headers.get("X-Bbk-Id")
        if bbk:
            request.state.bbk_id = bbk
        return await call_next(request)

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
                "cron_example": "每日生成高潜保险客户名单",
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
    assert (
        creator_items["items"][0]["scenes"][0]["cron_example"]
        == "每日生成高潜保险客户名单"
    )
    assert [p["id"] for p in target_items["items"]] == [created["id"]]
    assert target_items["items"][0]["editable"] is False
    assert outsider_items["items"] == []


def test_list_branch_wide_for_president_and_middle(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "100"},
    ).json()

    president = {
        "X-User-Id": "wangly",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB1101",
    }
    middle = {**president, "X-Position-Id": "RB0304"}
    rm = {**president, "X-Position-Id": "RB0101"}
    other_branch = {**president, "X-Bbk-Id": "200"}

    for headers in (president, middle):
        items = client.get("/api/wealth/plans", headers=headers).json()[
            "items"
        ]
        assert [p["id"] for p in items] == [created["id"]]
        assert items[0]["editable"] is False  # 可见但只读
    assert client.get("/api/wealth/plans", headers=rm).json()["items"] == []
    assert (
        client.get("/api/wealth/plans", headers=other_branch).json()["items"]
        == []
    )


def test_detail_visible_to_branch_peer_readonly(client: TestClient) -> None:
    created = client.post(
        "/api/wealth/plans",
        json=plan_payload(),
        headers={**VIEWER, "X-Bbk-Id": "100"},
    ).json()
    president = {
        "X-User-Id": "wangly",
        "X-Bbk-Id": "100",
        "X-Position-Id": "RB0306",
    }

    detail = client.get(
        f"/api/wealth/plans/{created['id']}",
        headers=president,
    )
    assert detail.status_code == 200
    assert detail.json()["editable"] is False

    outsider = {**president, "X-Bbk-Id": "200"}
    resp = client.get(f"/api/wealth/plans/{created['id']}", headers=outsider)
    assert resp.status_code == 403


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


def test_name_list_allows_empty_skill_id(client: TestClient) -> None:
    """skill_id 为空表示客户视角（该经理名下全部技能客户），参数合法。"""
    resp = client.get("/api/wealth/name-list?sap_id=10086")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_name_list_empty_when_external_absent(client: TestClient) -> None:
    """外部接口未配置/不可达时返回空列表，不做假数据兜底。"""
    resp = client.get("/api/wealth/name-list?skill_id=loan_verify")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_name_list_allows_empty_sap_id(client: TestClient) -> None:
    """sap_id 可空（缺省场景/兼容调用），参数合法。"""
    resp = client.get("/api/wealth/name-list?skill_id=loan_verify&sap_id=")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_name_list_rejects_invalid_touched(client: TestClient) -> None:
    """touched 仅接受 0 未触达 / 1 已触达 / 2 全部。"""
    resp = client.get("/api/wealth/name-list?touched=3")

    assert resp.status_code == 400

    ok = client.get("/api/wealth/name-list?touched=2")
    assert ok.status_code == 200


def test_name_list_forwards_touch_filter_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """触达筛选请求应携带支行、岗位和登录态。"""
    captured: dict = {}

    class FakeResponse:
        def json(self) -> dict:
            return {"code": "200", "data": {"list": []}}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(
            self,
            url: str,
            json: dict,
            headers: dict | None = None,
        ) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setenv("SWE_SKILL_CONFIG_API_BASE", "http://external.test")
    monkeypatch.setattr(wealth_router.httpx, "AsyncClient", FakeClient)

    resp = client.get(
        "/api/wealth/name-list?skill_id=SKILL0001&sap_id=80280256&touched=0",
        headers={
            **VIEWER,
            "X-Bbk-Id": "755",
            "X-Org-Code": "755480",
            "X-Position-Id": "RB0101",
            "x-header-cookie": "session=active",
        },
    )

    assert resp.status_code == 200
    assert captured["url"].endswith("/api/agent/workspace/name-list")
    assert captured["json"] == {
        "bbkId": "755",
        "platformSource": "WP",
        "pageSource": "WP_AGENT_WORKSPACE_TASK_LIST",
        "touched": 0,
        "skillId": "SKILL0001",
        "sapId": "80280256",
        "subBbkId": "755480",
        "posId": "RB0101",
    }
    assert captured["headers"] == {"Cookie": "session=active"}


def test_skill_stats_empty_when_external_absent(client: TestClient) -> None:
    """外部接口未配置/不可达时返回空列表，由前端保持占位。"""
    resp = client.post(
        "/api/wealth/skill-stats",
        json={
            "skills": [
                {
                    "skillId": "s1",
                    "startDate": "2026-09-01",
                    "endDate": "2026-09-30",
                },
            ],
        },
        headers=VIEWER,
    )

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_skill_stats_requires_non_empty_skills(client: TestClient) -> None:
    resp = client.post(
        "/api/wealth/skill-stats",
        json={"skills": []},
        headers=VIEWER,
    )

    assert resp.status_code == 422


def test_skill_stats_forwards_bbk_and_skills(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """代理层注入 bbkId 并原样转发 skills，响应项透传。"""
    captured: dict = {}

    class FakeResponse:
        def json(self) -> dict:
            return {
                "code": "200",
                "data": {
                    "items": [
                        {
                            "skillId": "s1",
                            "targetCustomerCount": 5,
                            "generatedTaskCount": 2,
                        },
                    ],
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, url: str, json: dict) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setenv("SWE_SKILL_CONFIG_API_BASE", "http://external.test")
    monkeypatch.setattr(wealth_router.httpx, "AsyncClient", FakeClient)

    resp = client.post(
        "/api/wealth/skill-stats",
        json={
            "skills": [
                {
                    "skillId": "s1",
                    "startDate": "2026-09-01",
                    "endDate": "2026-09-30",
                },
            ],
        },
        headers={**VIEWER, "X-Bbk-Id": "755"},
    )

    assert resp.status_code == 200
    assert resp.json()["items"][0]["targetCustomerCount"] == 5
    assert captured["url"].endswith("/api/agent/workspace/skill-stats")
    assert captured["json"]["bbkId"] == "755"
    assert captured["json"]["skills"][0]["skillId"] == "s1"


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


def test_skill_ids_by_customer_dedupes_and_skips_bad_rows() -> None:
    """客户视角标签列的数据来源：custuid → 命中技能列表（去重、跳过脏行）。"""
    rows = [
        {"custuid": "CUST001", "skillId": "loan_verify"},
        {"custuid": "cust001", "skillId": "loan_verify"},  # 大小写归一并去重
        {"custuid": "CUST001", "skillId": "deposit_growth"},
        {"custuid": "CUST002", "skillId": ""},  # 无技能，跳过
        {"custuid": "", "skillId": "loan_verify"},  # 无客户，跳过
        "not-a-dict",
    ]

    mapping = wealth_router._skill_ids_by_customer(rows)

    assert mapping == {"cust001": ["loan_verify", "deposit_growth"]}
