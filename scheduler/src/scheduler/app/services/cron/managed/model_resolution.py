# -*- coding: utf-8 -*-
"""执行入队前向目标 SWE 租户解析实际模型，不保存默认模型占位。"""

import os

import httpx

from .identity import encode_scope
from ..swe_routing import checked_base, swe_base_for_source


async def resolve_agent_model(current, spec):
    default_base = checked_base(
        os.environ.get("SCHEDULER_SWE_API_BASE_URL", ""),
    ).removesuffix("/api")
    base = swe_base_for_source(current["source_id"], default_base)
    headers = {
        "X-User-Id": current["creator_user_id"],
        "X-Tenant-Id": current["tenant_id"],
        "X-Source-Id": current["source_id"],
        "X-Agent-Id": current["agent_id"],
        "X-Scope-Id": encode_scope(current["tenant_id"], current["source_id"]),
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post(
            f"{base}/api/internal/cron/managed/resolve-model",
            headers=headers,
            json={"model_slot": spec.get("model_slot")},
        )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("cron_execution_model_unavailable")
    result = {}
    for name in ("provider_id", "model"):
        field = value.get(name)
        if (
            not isinstance(field, str)
            or not field.strip()
            or field.strip() == "default"
        ):
            raise ValueError("cron_execution_model_unavailable")
        result[name] = field.strip()
    return result
