# -*- coding: utf-8 -*-
"""只持久化系统调度所需的安全摘要，不复制业务配置和凭据。"""

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..calendar_schedule import validate_cron


def canonical(value):
    """固定安全摘要的序列化，供配置比较和状态查询使用。"""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def normalize_config(raw):
    """系统任务不启用用户日历过滤，只校验原有普通五段排程。"""
    if not isinstance(raw, dict) or type(raw.get("enabled")) is not bool:
        raise ValueError("invalid_system_config")
    cron = raw.get("cron", "")
    if not isinstance(cron, str):
        raise ValueError("invalid_system_cron")
    if cron:
        try:
            cron = validate_cron(cron)
            if len(cron.split()) != 5:
                raise ValueError("system tasks require five-field cron")
        except ValueError as exc:
            raise ValueError("invalid_system_cron") from exc
    if raw["enabled"] and not cron:
        raise ValueError("invalid_system_cron")
    zone = raw.get("timezone", "Asia/Shanghai")
    try:
        ZoneInfo(zone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid_system_timezone") from exc
    timeout = raw.get("timeout_seconds", 7200)
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("invalid_system_timeout")
    return {
        "enabled": raw["enabled"],
        "cron": " ".join(cron.split()),
        "timezone": zone,
        "timeout_seconds": timeout,
    }
