# -*- coding: utf-8 -*-
"""Scheduler service configuration."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


class SchedulerDatabaseConfig(BaseModel):
    """Database connection settings for the standalone Scheduler service."""

    host: str = Field(default="localhost")
    port: int = Field(default=3306)
    user: str = Field(default="root")
    password: str = Field(default="")
    database: str = Field(default="copaw_scheduler")
    min_connections: int = Field(default=2)
    max_connections: int = Field(default=10)
    charset: str = Field(default="utf8mb4")


def _get_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _get_int(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
) -> int:
    raw = _get_str(name, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if min_value is not None and value < min_value:
        return min_value
    return value


def _get_bool(
    name: str,
    default: bool = False,
) -> bool:
    raw = _get_str(
        name,
        str(default).lower(),
    )
    return raw.strip().lower() in _TRUE_ENV_VALUES


def _clean_secret(value: str) -> str:
    return value.removeprefix("BEE_")


LOG_LEVEL_ENV = "SCHEDULER_LOG_LEVEL"

ENV_NAME = _get_str("SCHEDULER_ENV", "prd")
DOCS_ENABLED = _get_bool("SCHEDULER_OPENAPI_DOCS", False)
DEFAULT_HOST = _get_str("SCHEDULER_HOST", "127.0.0.1")
DEFAULT_PORT = _get_int("SCHEDULER_PORT", 9100, min_value=1)

DB_HOST = _get_str("SCHEDULER_DB_HOST", "")
DB_PORT = _get_int(
    "SCHEDULER_DB_PORT",
    3306,
    min_value=1,
)
DB_USER = _get_str("SCHEDULER_DB_USER", "root")
DB_ACCESS = _clean_secret(_get_str("SCHEDULER_DB_ACCESS", ""))
DB_NAME = _get_str("SCHEDULER_DB_NAME", "copaw_scheduler")
DB_MIN_CONN = _get_int(
    "SCHEDULER_DB_MIN_CONN",
    2,
    min_value=1,
)
DB_MAX_CONN = _get_int(
    "SCHEDULER_DB_MAX_CONN",
    10,
    min_value=1,
)
DB_INIT_TABLES = _get_bool("SCHEDULER_DB_INIT_TABLES", False)

SWE_API_BASE_URL = _get_str(
    "SCHEDULER_SWE_API_BASE_URL",
    "",
).rstrip("/")
SWE_INTERNAL_TOKEN_ENV = "SWE_INTERNAL_TOKEN"
SCHEDULER_SWE_INTERNAL_TOKEN_ENV = "SCHEDULER_SWE_INTERNAL_TOKEN"

DISPATCH_INTENTS_ENABLED_ENV = "SWE_CRON_DISPATCH_INTENTS_ENABLED"
DEFAULT_SCHEDULER_LOOP_INTERVAL_SECONDS = 60
DEFAULT_CAPACITY_CHECK_INTERVAL_SECONDS = 60
DISPATCHED_STALE_SECONDS_ENV = "SCHEDULER_CRON_DISPATCHED_STALE_SECONDS"


def get_scheduler_database_config() -> SchedulerDatabaseConfig:
    """Build the database config used by Scheduler's DB adapter."""
    return SchedulerDatabaseConfig(
        host=DB_HOST or "localhost",
        port=DB_PORT,
        user=DB_USER or "root",
        password=DB_ACCESS,
        database=DB_NAME or "copaw_scheduler",
        min_connections=DB_MIN_CONN,
        max_connections=DB_MAX_CONN,
    )
