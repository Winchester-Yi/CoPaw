# -*- coding: utf-8 -*-
"""观测身份解析：写入端与查询端共用同一 service unit 来源。"""

from __future__ import annotations

import os

SERVICE_UNIT_ENV = "CMB_CAAS_SERVICEUNITID"
LEGACY_SERVICE_UNIT_ENV = "SWE_SERVICE_UNIT_ID"
DEFAULT_SERVICE_UNIT = "swe"


def service_unit_id() -> str:
    """返回观测写入与查询统一使用的 service unit 标识。

    优先使用部署侧业务标识 ``CMB_CAAS_SERVICEUNITID``，其次兼容旧的
    ``SWE_SERVICE_UNIT_ID``，两者都未配置时回退到 ``swe``。
    """
    for name in (SERVICE_UNIT_ENV, LEGACY_SERVICE_UNIT_ENV):
        value = os.environ.get(name)
        if value:
            return value
    return DEFAULT_SERVICE_UNIT


def same_tenant_scope(left: object, right: object, source: object) -> bool:
    """Compare logical and runtime tenant forms within one trusted Source."""
    from ..config.context import resolve_runtime_tenant_id

    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if not isinstance(source, str) or not left or not right or not source:
        return False
    try:
        return resolve_runtime_tenant_id(
            left,
            source,
        ) == resolve_runtime_tenant_id(right, source)
    except ValueError:
        return False


def same_observability_scope(left: object, right: object) -> bool:
    """Compare scope identities while accepting the equivalent runtime tenant."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    fields = ("service_unit_id", "source_id", "agent_id")
    if any(
        not isinstance(left.get(k), str)
        or not left[k]
        or left[k] != right.get(k)
        for k in fields
    ):
        return False
    return same_tenant_scope(
        left.get("tenant_id"),
        right.get("tenant_id"),
        left["source_id"],
    )
