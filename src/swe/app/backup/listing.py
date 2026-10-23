# -*- coding: utf-8 -*-
"""Synchronous restore selection, run together with S3 enumeration."""

from typing import Callable


def list_restore_tenants(
    list_backups: Callable[..., dict],
    instance_id: str | None,
    date: str | None,
    hour: int | None,
    tenant_ids: list[str] | None = None,
) -> list[str]:
    backups = list_backups(instance_id=instance_id, date=date, hour=hour)
    available = list(
        backups["backups"]
        .get(instance_id or "default", {})
        .get(date, {})
        .get(hour if hour is not None else 0, {})
        .keys(),
    )
    if tenant_ids:
        return [tenant for tenant in tenant_ids if tenant in available]
    return available


def find_latest_backup_hour(
    list_backups: Callable[..., dict],
    instance_id: str,
    date: str,
) -> int | None:
    backups = list_backups(instance_id=instance_id, date=date)
    hours = backups["backups"].get(instance_id, {}).get(date, {})
    return max(hours) if hours else None
