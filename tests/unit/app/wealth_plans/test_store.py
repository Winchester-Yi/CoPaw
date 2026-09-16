# -*- coding: utf-8 -*-
"""WealthPlanStore 内存模式单元测试。"""

from __future__ import annotations

import pytest

from swe.app.wealth_plans.models import (
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_PUBLISHING,
    PlanSceneRecord,
    PlanTargetRecord,
    WealthPlanRecord,
)
from swe.app.wealth_plans.store import WealthPlanStore, new_plan_id


def make_plan(
    plan_id: str = "plan-1",
    sap_id: str = "zhangwl",
    targets: list[str] | None = None,
) -> WealthPlanRecord:
    return WealthPlanRecord(
        id=plan_id,
        sap_id=sap_id,
        name="九月重点客户经营计划",
        source_label="行长关注",
        scenes=[
            PlanSceneRecord(
                scene_id="skill-wealth-insurance-1",
                scene_name="保障潜客经营",
                category="insurance",
                cron_expr="0 9 * * *",
                cron_example="每日生成高潜保险客户名单",
                direction="挖掘高潜保险客户",
                mcp_relations=["mcp-customer", "mcp-insurance"],
            ),
        ],
        targets=[
            PlanTargetRecord(sap_id=t, target_name=f"用户{t}", sort_order=i)
            for i, t in enumerate(targets or [])
        ],
    )


@pytest.mark.asyncio
async def test_create_and_get_roundtrip() -> None:
    store = WealthPlanStore()
    await store.create(make_plan(targets=["chenjy", "liuxt"]))

    record = await store.get("plan-1")

    assert record is not None
    assert record.sap_id == "zhangwl"
    assert record.status == PUBLISH_STATUS_PUBLISHING
    assert record.scenes[0].mcp_relations == ["mcp-customer", "mcp-insurance"]
    assert record.scenes[0].cron_example == "每日生成高潜保险客户名单"
    assert [t.sap_id for t in record.targets] == ["chenjy", "liuxt"]
    assert record.created_at is not None


@pytest.mark.asyncio
async def test_list_for_viewer_covers_creator_and_target() -> None:
    store = WealthPlanStore()
    await store.create(
        make_plan("plan-1", sap_id="zhangwl", targets=["chenjy"]),
    )
    await store.create(make_plan("plan-2", sap_id="liuxt", targets=[]))

    creator_view = await store.list_for_viewer("zhangwl", "rm", None)
    target_view = await store.list_for_viewer("chenjy", "rm", None)
    outsider_view = await store.list_for_viewer("wangly", "rm", None)

    assert {r.id for r in creator_view} == {"plan-1"}
    assert {r.id for r in target_view} == {"plan-1"}
    assert outsider_view == []


@pytest.mark.asyncio
async def test_list_for_viewer_branch_wide_for_president_and_middle() -> None:
    store = WealthPlanStore()
    await store.create(
        make_plan("plan-1", sap_id="zhangwl", targets=["chenjy"]),
    )
    await store.create(make_plan("plan-2", sap_id="liuxt", targets=[]))
    other_branch = make_plan("plan-3", sap_id="waibu", targets=[])
    other_branch.bbk_id = "200"
    await store.create(other_branch)

    # 行长/中台看本行全部规划（fixture 默认 bbk_id=None，需显式补上）
    for record in store._plans.values():
        if record.id != "plan-3":
            record.bbk_id = "100"
    president_view = await store.list_for_viewer("wangly", "president", "100")
    middle_view = await store.list_for_viewer("wangly", "middle", "100")
    rm_view = await store.list_for_viewer("wangly", "rm", "100")
    no_bbk_view = await store.list_for_viewer("wangly", "president", None)

    assert {r.id for r in president_view} == {"plan-1", "plan-2"}
    assert {r.id for r in middle_view} == {"plan-1", "plan-2"}
    assert rm_view == []  # 客户经理不看本行全部
    assert no_bbk_view == []  # bbk 缺失时退化为个人口径


@pytest.mark.asyncio
async def test_update_replaces_children_and_resets_status() -> None:
    store = WealthPlanStore()
    plan = make_plan(targets=["chenjy"])
    await store.create(plan)
    await store.set_publish_status("plan-1", PUBLISH_STATUS_PUBLISHED)

    updated = make_plan(targets=["zhaom"])
    updated.name = "十月计划"
    updated.scenes[0].cron_job_id = "job-keep"  # 沿用既有任务 id
    await store.update(updated)

    record = await store.get("plan-1")
    assert record is not None
    assert record.name == "十月计划"
    assert record.status == PUBLISH_STATUS_PUBLISHING
    assert [t.sap_id for t in record.targets] == ["zhaom"]
    assert record.scenes[0].cron_job_id == "job-keep"


@pytest.mark.asyncio
async def test_update_clears_skill_dispatch_columns() -> None:
    store = WealthPlanStore()
    await store.create(make_plan())
    await store.set_skill_dispatch(
        "plan-1",
        batch_id="wealth-plan-plan-1-abcd1234",
        status="partial",
        error="部分目标分发失败",
    )

    updated = make_plan()
    updated.name = "十月计划"
    await store.update(updated)

    record = await store.get("plan-1")
    assert record is not None
    assert record.skill_dispatch_task_id is None
    assert record.skill_dispatch_status is None
    assert record.skill_dispatch_error is None


@pytest.mark.asyncio
async def test_delete_removes_plan() -> None:
    store = WealthPlanStore()
    await store.create(make_plan())

    await store.delete("plan-1")

    assert await store.get("plan-1") is None
    assert await store.list_for_viewer("zhangwl", "rm", None) == []


@pytest.mark.asyncio
async def test_set_scene_links_writes_job_and_broadcast_ids() -> None:
    store = WealthPlanStore()
    await store.create(make_plan())

    await store.set_scene_links(
        "plan-1",
        "skill-wealth-insurance-1",
        "job-1",
        "task-1",
    )

    record = await store.get("plan-1")
    assert record is not None
    assert record.scenes[0].cron_job_id == "job-1"
    assert record.scenes[0].broadcast_task_id == "task-1"


def test_new_plan_id_format() -> None:
    plan_id = new_plan_id()
    assert plan_id.startswith("plan-")
    assert len(plan_id) == len("plan-") + 12
