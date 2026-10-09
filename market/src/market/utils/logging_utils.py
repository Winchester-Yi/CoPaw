# -*- coding: utf-8 -*-
"""Logging utility functions for market service."""

from __future__ import annotations

import logging
from typing import Any


def log_params(
    logger: logging.Logger,
    method: str,
    path: str,
    **kwargs: Any,
) -> None:
    """记录接口参数，空白参数显示为 key=（空值）"""
    param_parts = []
    for k, v in kwargs.items():
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        param_parts.append(f"{k}={v}")
    param_str = ", ".join(param_parts) if param_parts else "-"
    logger.info(
        "[params] method=%s path=%s params=%s",
        method,
        path,
        param_str,
    )
