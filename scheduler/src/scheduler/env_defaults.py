# -*- coding: utf-8 -*-
"""Environment default loader for the standalone Scheduler service."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_ENVS = ("dev", "prd")
DEFAULT_ENV = "prd"
SWE_SOURCE_ROUTES_ENV = "SCHEDULER_SWE_SOURCE_ROUTES"


def _get_package_dir() -> Path:
    return Path(__file__).resolve().parent


def _load_env_json(env: str) -> dict[str, str]:
    if env not in VALID_ENVS:
        logger.warning(
            "Invalid SCHEDULER_ENV '%s', falling back to '%s'",
            env,
            DEFAULT_ENV,
        )
        env = DEFAULT_ENV

    config_file = _get_package_dir() / "config" / "envs" / f"{env}.json"
    if not config_file.exists():
        logger.warning("Scheduler env defaults not found: %s", config_file)
        return {}

    try:
        with open(config_file, encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to load Scheduler env defaults from %s: %s",
            config_file,
            exc,
        )
        return {}

    result: dict[str, str] = {}
    for key, value in data.items():
        if key == SWE_SOURCE_ROUTES_ENV and isinstance(value, list):
            result[key] = json.dumps(value, ensure_ascii=False)
        else:
            result[key] = "" if value is None else str(value)
    return result


def load_env_defaults(env: str | None = None) -> dict[str, str]:
    """Load package env defaults without overriding existing variables."""
    if env is None:
        env = os.environ.get("SCHEDULER_ENV", DEFAULT_ENV)

    defaults = _load_env_json(env)
    set_vars: dict[str, str] = {}
    for key, value in defaults.items():
        if key not in os.environ:
            os.environ[key] = value
            set_vars[key] = value

    if set_vars:
        logger.debug(
            "Loaded %d Scheduler env defaults for '%s': %s",
            len(set_vars),
            env,
            ", ".join(sorted(set_vars.keys())),
        )
    return set_vars


def get_current_env() -> str:
    return os.environ.get("SCHEDULER_ENV", DEFAULT_ENV)
