# -*- coding: utf-8 -*-
"""按 Scheduler 来源 ID 精确匹配 SWE 请求地址。"""

import json
import os
from functools import lru_cache
from typing import NamedTuple
from urllib.parse import urlsplit

from ....env_defaults import SWE_SOURCE_ROUTES_ENV


class SweSourceRoute(NamedTuple):
    api_base_url: str
    internal_api_base_url: str


def checked_base(value):
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("cron_service_url_not_configured")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_cron_service_url")
    return value


def _route_base(value: str) -> str:
    base = checked_base(value)
    parsed = urlsplit(base)
    if (
        parsed.port == 0
        or parsed.netloc.endswith(":")
        or "?" in base
        or "#" in base
    ):
        raise ValueError("invalid_cron_service_url")
    if parsed.path.endswith("/api"):
        return base.removesuffix("/api")
    return base


@lru_cache(maxsize=1)
def _parse_source_routes(raw: str) -> dict[str, SweSourceRoute]:
    routes = {}
    try:
        groups = json.loads(raw or "[]")
        if not isinstance(groups, list):
            raise ValueError
        for group in groups:
            route = SweSourceRoute(
                api_base_url=_route_base(group["api_base_url"]),
                internal_api_base_url=_route_base(
                    group["internal_api_base_url"],
                ),
            )
            source_ids = group["source_ids"]
            if not isinstance(source_ids, list) or not source_ids:
                raise ValueError
            for source_id in source_ids:
                if (
                    not isinstance(source_id, str)
                    or not source_id
                    or source_id.strip() != source_id
                    or source_id in routes
                ):
                    raise ValueError
                routes[source_id] = route
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {SWE_SOURCE_ROUTES_ENV}") from exc
    return routes


def validate_swe_source_routes() -> None:
    _parse_source_routes(os.environ.get(SWE_SOURCE_ROUTES_ENV, ""))


def swe_base_for_source(
    source_id: str,
    default: str,
    *,
    internal: bool = False,
) -> str:
    routes = _parse_source_routes(os.environ.get(SWE_SOURCE_ROUTES_ENV, ""))
    route = routes.get(source_id)
    if route is None:
        return default
    return route.internal_api_base_url if internal else route.api_base_url
