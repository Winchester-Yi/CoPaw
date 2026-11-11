# -*- coding: utf-8 -*-
"""Scheduler configuration tests."""

from __future__ import annotations

import importlib
import os

_CONFIG_ENV_KEYS = (
    "SCHEDULER_ENV",
    "SCHEDULER_OPENAPI_DOCS",
    "SCHEDULER_HOST",
    "SCHEDULER_PORT",
    "SCHEDULER_DB_HOST",
    "SCHEDULER_DB_PORT",
    "SCHEDULER_DB_USER",
    "SCHEDULER_DB_ACCESS",
    "SCHEDULER_DB_NAME",
    "SCHEDULER_DB_MIN_CONN",
    "SCHEDULER_DB_MAX_CONN",
    "SCHEDULER_DB_INIT_TABLES",
    "SCHEDULER_SWE_API_BASE_URL",
    "SCHEDULER_CRON_BATCH_MAX_ATTEMPTS",
    "OTHER_ENV",
    "OTHER_OPENAPI_DOCS",
    "OTHER_DB_HOST",
    "OTHER_DB_PORT",
    "OTHER_DB_USER",
    "OTHER_DB_ACCESS",
    "OTHER_DB_NAME",
    "OTHER_DB_MIN_CONN",
    "OTHER_DB_MAX_CONN",
    "OTHER_DB_INIT_TABLES",
    "OTHER_SWE_API_BASE_URL",
)


def _reload_scheduler_config(monkeypatch, **env: str):
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from scheduler.config import constant

    return importlib.reload(constant)


def test_scheduler_config_prefers_scheduler_namespace(monkeypatch) -> None:
    config = _reload_scheduler_config(
        monkeypatch,
        SCHEDULER_ENV="dev",
        SCHEDULER_OPENAPI_DOCS="true",
        SCHEDULER_HOST="0.0.0.0",
        SCHEDULER_PORT="19100",
        SCHEDULER_DB_HOST="scheduler-db",
        SCHEDULER_DB_PORT="13306",
        SCHEDULER_DB_USER="scheduler-user",
        SCHEDULER_DB_ACCESS="BEE_scheduler-secret",
        SCHEDULER_DB_NAME="scheduler_db",
        SCHEDULER_DB_MIN_CONN="3",
        SCHEDULER_DB_MAX_CONN="9",
        SCHEDULER_DB_INIT_TABLES="false",
        SCHEDULER_SWE_API_BASE_URL="http://scheduler-swe",
        SCHEDULER_CRON_BATCH_MAX_ATTEMPTS="5",
        OTHER_DB_HOST="other-db",
        OTHER_SWE_API_BASE_URL="http://other-swe",
    )

    assert config.ENV_NAME == "dev"
    assert config.DOCS_ENABLED is True
    assert config.DEFAULT_HOST == "0.0.0.0"
    assert config.DEFAULT_PORT == 19100
    assert config.DB_HOST == "scheduler-db"
    assert config.DB_PORT == 13306
    assert config.DB_USER == "scheduler-user"
    assert config.DB_ACCESS == "scheduler-secret"
    assert config.DB_NAME == "scheduler_db"
    assert config.DB_MIN_CONN == 3
    assert config.DB_MAX_CONN == 9
    assert config.DB_INIT_TABLES is False
    assert config.SWE_API_BASE_URL == "http://scheduler-swe"
    assert config.CRON_BATCH_MAX_ATTEMPTS == 5

    db_config = config.get_scheduler_database_config()
    assert db_config.host == "scheduler-db"
    assert db_config.password == "scheduler-secret"


def test_batch_max_attempts_defaults_to_three(monkeypatch) -> None:
    assert _reload_scheduler_config(monkeypatch).CRON_BATCH_MAX_ATTEMPTS == 3


def test_scheduler_config_ignores_non_scheduler_namespace(
    monkeypatch,
) -> None:
    config = _reload_scheduler_config(
        monkeypatch,
        SCHEDULER_DB_HOST="",
        SCHEDULER_SWE_API_BASE_URL="",
        OTHER_ENV="uat",
        OTHER_OPENAPI_DOCS="yes",
        OTHER_DB_HOST="other-db",
        OTHER_DB_PORT="23306",
        OTHER_DB_USER="other-user",
        OTHER_DB_ACCESS="BEE_other-secret",
        OTHER_DB_NAME="other_db",
        OTHER_DB_MIN_CONN="4",
        OTHER_DB_MAX_CONN="12",
        OTHER_DB_INIT_TABLES="true",
        OTHER_SWE_API_BASE_URL="http://other-swe",
    )

    assert config.ENV_NAME == "prd"
    assert config.DOCS_ENABLED is False
    assert config.DB_HOST == ""
    assert config.DB_PORT == 3306
    assert config.DB_USER == "root"
    assert config.DB_ACCESS == ""
    assert config.DB_NAME == "copaw_scheduler"
    assert config.DB_MIN_CONN == 2
    assert config.DB_MAX_CONN == 10
    assert config.DB_INIT_TABLES is False
    assert config.SWE_API_BASE_URL == ""


def test_scheduler_env_defaults_load_package_json(monkeypatch) -> None:
    from scheduler import env_defaults

    for key in (
        "SCHEDULER_ENV",
        "SCHEDULER_LOG_LEVEL",
        "SCHEDULER_OPENAPI_DOCS",
        "SCHEDULER_DB_NAME",
    ):
        monkeypatch.delenv(key, raising=False)

    loaded = env_defaults.load_env_defaults("dev")

    assert loaded["SCHEDULER_ENV"] == "dev"
    assert os.environ["SCHEDULER_LOG_LEVEL"] == "debug"
    assert os.environ["SCHEDULER_OPENAPI_DOCS"] == "true"
    assert os.environ["SCHEDULER_DB_NAME"] == "copaw_dev"


def test_scheduler_envs_load_without_overwriting_process_env(
    monkeypatch,
    tmp_path,
) -> None:
    from scheduler.envs import store

    envs_path = tmp_path / "envs.json"
    store.save_envs(
        {
            "SCHEDULER_DB_HOST": "persisted-db",
            "SCHEDULER_DB_USER": "persisted-user",
        },
        envs_path,
    )
    monkeypatch.setenv("SCHEDULER_DB_HOST", "process-db")
    monkeypatch.delenv("SCHEDULER_DB_USER", raising=False)
    loaded = store.load_envs_into_environ(envs_path)

    assert os.environ["SCHEDULER_DB_HOST"] == "process-db"
    assert os.environ["SCHEDULER_DB_USER"] == "persisted-user"
    assert loaded["SCHEDULER_DB_HOST"] == "persisted-db"
