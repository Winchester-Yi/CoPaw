# -*- coding: utf-8 -*-
"""技能/MCP 批量分发腿：对接 market 服务 /api/market/distributions。

发布编排的第三条腿（前两条为建定时任务 + cron 广播）：
按规划维度汇总全部场景的技能 item 与 MCP 关联，一次提交批量分发；
批次状态在列表/详情查询时按需刷新，聚合成看板状态栏。
接口契约见 docs/batch-distribution.md。
"""

from __future__ import annotations

import hashlib
import logging
import os
from urllib.parse import quote

import httpx

from .models import WealthPlanRecord

logger = logging.getLogger(__name__)

# market 服务基础地址：默认值在 src/swe/config/envs/{dev,prd}.json 维护，
# 启动时由 load_env_defaults() 注入 os.environ；进程环境变量/K8s env 优先。
MARKET_API_BASE_ENV = "SWE_MARKET_API_BASE_URL"
_MARKET_DISTRIBUTIONS_PATH = "/market/distributions"
_MARKET_TIMEOUT_SECONDS = 8

# 批次状态（文档第 5 节）；前两个为非终态，列表查询时会继续刷新
BATCH_STATUS_QUEUED = "queued"
BATCH_STATUS_RUNNING = "running"
BATCH_STATUS_SUCCEEDED = "succeeded"
BATCH_STATUS_FAILED = "failed"
BATCH_STATUS_PARTIAL_FAILED = "partial_failed"
TERMINAL_BATCH_STATUSES = frozenset(
    {BATCH_STATUS_SUCCEEDED, BATCH_STATUS_FAILED, BATCH_STATUS_PARTIAL_FAILED},
)


def _market_base() -> str:
    return os.environ.get(MARKET_API_BASE_ENV, "").strip().rstrip("/")


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_MARKET_TIMEOUT_SECONDS)


def collect_distribution_items(
    plan: WealthPlanRecord,
) -> tuple[list[str], list[str], list[str]]:
    """汇总分发清单：技能 item、MCP item、目标租户（含创建人本人，去重排序）。"""
    skill_ids = sorted({s.item_id for s in plan.scenes if s.item_id})
    mcp_ids = sorted({m for s in plan.scenes for m in s.mcp_relations})
    tenant_ids = sorted({t.sap_id for t in plan.targets} | {plan.sap_id})
    return skill_ids, mcp_ids, tenant_ids


def build_batch_id(
    plan: WealthPlanRecord,
    skill_ids: list[str],
    mcp_ids: list[str],
    tenant_ids: list[str],
) -> str:
    """批次 id：plan id + 内容摘要。

    相同内容重试复用同一批次（接口按 来源+batch_id+内容 幂等），
    内容变化（修改规划重新发布）则生成新批次，避免 409 冲突。
    """
    digest = hashlib.sha256(
        "\x00".join(
            ["s", *skill_ids, "m", *mcp_ids, "t", *tenant_ids],
        ).encode(),
    ).hexdigest()[:8]
    return f"wealth-plan-{plan.id}-{digest}"


def _request_headers(plan: WealthPlanRecord) -> dict[str, str]:
    headers = {
        "X-Source-Id": plan.source_id or "",
        "X-Manager": "true",
        "X-User-Id": plan.sap_id,
    }
    if plan.creator_name:
        # HTTP 头仅支持 ASCII，中文名按 UTF-8 百分号编码
        headers["X-User-Name"] = quote(plan.creator_name)
    return headers


async def submit_distribution(
    plan: WealthPlanRecord,
) -> tuple[str, str] | None:
    """提交批量分发，返回 (batch_id, 批次状态)。

    返回 None 表示该规划无需分发（无技能/MCP item）或分发腿未配置
    （缺 SWE_MARKET_API_BASE_URL / source_id）；传输或非 200 响应抛异常。
    """
    skill_ids, mcp_ids, tenant_ids = collect_distribution_items(plan)
    if not skill_ids and not mcp_ids:
        return None
    base = _market_base()
    if not base or not plan.source_id:
        logger.info(
            "skill distribution leg disabled (base/source missing), plan=%s",
            plan.id,
        )
        return None
    batch_id = build_batch_id(plan, skill_ids, mcp_ids, tenant_ids)
    async with _new_client() as client:
        resp = await client.post(
            f"{base}{_MARKET_DISTRIBUTIONS_PATH}",
            headers=_request_headers(plan),
            json={
                "batch_id": batch_id,
                "skill_item_ids": skill_ids,
                "mcp_item_ids": mcp_ids,
                "target_tenant_ids": tenant_ids,
                "overwrite": True,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"market distributions rejected: http {resp.status_code} {resp.text[:200]}",
        )
    payload = resp.json()
    return batch_id, str(payload.get("status") or BATCH_STATUS_QUEUED)


async def query_batch_status(source_id: str, batch_id: str) -> str | None:
    """按批次查询分发状态；未配置或查询失败返回 None（调用方沿用已存状态）。"""
    base = _market_base()
    if not base or not source_id or not batch_id:
        return None
    try:
        async with _new_client() as client:
            resp = await client.get(
                f"{base}{_MARKET_DISTRIBUTIONS_PATH}",
                headers={"X-Source-Id": source_id, "X-Manager": "true"},
                params=[("batchids", batch_id)],
            )
        if resp.status_code != 200:
            logger.warning(
                "market distributions query rejected: http %s",
                resp.status_code,
            )
            return None
        batches = resp.json().get("batches") or []
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("market distributions query failed: %s", exc)
        return None
    if not batches:
        return None
    status = batches[0].get("status")
    return str(status) if status else None
