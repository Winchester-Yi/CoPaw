# -*- coding: utf-8 -*-
"""发布编排的纯逻辑单元测试（不触碰真实 CronManager）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swe.app.wealth_plans.models import PlanSceneRecord, WealthPlanRecord
from swe.app.wealth_plans.publish import (
    _build_job_spec,
    _build_request_input,
    _build_task_text,
    _broadcast_scene_job,
)


def make_scene(**overrides) -> PlanSceneRecord:
    scene = PlanSceneRecord(
        scene_id="skill-wealth-insurance-1",
        scene_name="保障潜客经营",
        category="insurance",
        cron_expr="30 8 * * mon,thu",
        direction="挖掘高潜保险客户",
        start_date="2026-09-01",
        end_date="2026-09-30",
        mcp_relations=["mcp-customer"],
    )
    for key, value in overrides.items():
        setattr(scene, key, value)
    return scene


def make_plan(targets: list[str]) -> WealthPlanRecord:
    return WealthPlanRecord(
        id="plan-1",
        sap_id="zhangwl",
        name="九月计划",
        scenes=[make_scene()],
        targets=[SimpleNamespace(sap_id=t) for t in targets],  # type: ignore[arg-type]
    )


def fake_request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            tenant_id="zhangwl",
            bbk_id="100",
            source_id="RMASSIST",
            scope_id="scope-1",
            user_name="张**",
            agent_id=None,
        ),
    )


def test_build_task_text_composes_name_direction_and_period() -> None:
    text = _build_task_text(make_plan([]), make_scene())

    assert "经营规划「九月计划」场景「保障潜客经营」。" in text
    assert "经营方向：挖掘高潜保险客户。" in text
    assert "有效期：2026-09-01 至 2026-09-30" in text


def test_build_task_text_skips_optional_parts() -> None:
    scene = make_scene(direction=None, start_date=None, end_date=None)

    text = _build_task_text(make_plan([]), scene)

    assert "经营方向" not in text
    assert "有效期" not in text


def test_build_request_input_wraps_cron_example_in_message() -> None:
    scene = make_scene(cron_example="每日生成高潜保险客户名单")

    messages = _build_request_input(make_plan([]), scene)

    assert messages == [
        {
            "content": [{"text": "每日生成高潜保险客户名单", "type": "text"}],
            "role": "user",
            "type": "message",
        },
    ]


def test_build_request_input_falls_back_to_task_text() -> None:
    scene = make_scene(cron_example=None)

    messages = _build_request_input(make_plan([]), scene)

    assert messages[0]["content"][0]["text"] == _build_task_text(
        make_plan([]),
        scene,
    )


def test_build_job_spec_request_input_uses_cron_example() -> None:
    job = _build_job_spec(
        fake_request(),
        make_plan(["chenjy"]),
        make_scene(cron_example="每日生成高潜保险客户名单"),
        "job-1",
    )

    assert job.request.input == [
        {
            "content": [{"text": "每日生成高潜保险客户名单", "type": "text"}],
            "role": "user",
            "type": "message",
        },
    ]
    assert job.request.user_id == "zhangwl"


def test_build_job_spec_maps_identity_and_schedule() -> None:
    job = _build_job_spec(
        fake_request(),
        make_plan(["chenjy"]),
        make_scene(),
        "job-1",
    )

    assert job.id == "job-1"
    assert job.name == "九月计划｜保障潜客经营"
    assert job.tenant_id == "zhangwl"
    assert job.bbk_id == "100"
    assert job.schedule.cron == "30 8 * * mon,thu"
    assert job.schedule.timezone == "Asia/Shanghai"
    assert job.skill_ids == "skill-wealth-insurance-1"
    assert job.meta["wealth_plan_id"] == "plan-1"
    assert job.meta["wealth_item_id"] == ""
    assert job.meta["wealth_mcp_relations"] == ["mcp-customer"]
    assert job.dispatch.target.user_id == "zhangwl"


async def test_broadcast_skipped_when_only_creator_is_target() -> None:
    # request 传 None：若未提前返回则会立刻报错，证明确实跳过了广播
    result = await _broadcast_scene_job(
        None,  # type: ignore[arg-type]
        make_plan(["zhangwl"]),
        SimpleNamespace(id="job-1"),
    )

    assert result is None


async def test_broadcast_invoked_for_other_targets() -> None:
    plan = make_plan(["zhangwl", "chenjy"])
    job = SimpleNamespace(id="job-1")
    captured: dict[str, object] = {}

    # 广播链路重依赖 request/app.state，这里仅验证目标过滤逻辑：
    # 本人应被剔除，仅剩 chenjy。通过打补丁截获归一化入参。
    import swe.app.wealth_plans.publish as publish_module

    def fake_normalize(body):
        captured["target_tenant_ids"] = list(body.target_tenant_ids)
        raise RuntimeError("stop here")

    original = publish_module._normalize_broadcast_targets
    publish_module._normalize_broadcast_targets = fake_normalize
    try:
        with pytest.raises(RuntimeError, match="stop here"):
            await _broadcast_scene_job(None, plan, job)  # type: ignore[arg-type]
    finally:
        publish_module._normalize_broadcast_targets = original

    assert captured["target_tenant_ids"] == ["chenjy"]
