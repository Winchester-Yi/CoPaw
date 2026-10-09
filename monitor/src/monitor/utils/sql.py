# -*- coding: utf-8 -*-
"""SQL 调试工具函数."""

from datetime import datetime
from typing import Any, Optional, Sequence


def format_sql(query: str, params: Optional[Sequence[Any]]) -> str:
    """格式化 SQL，字符串参数加引号，用于调试日志.

    Args:
        query: SQL 查询模板
        params: 查询参数

    Returns:
        格式化后的完整 SQL 字符串
    """
    if not params:
        return query

    formatted = query
    for param in reversed(params):
        if param is None:
            formatted = formatted.replace("%s", "NULL", 1)
        elif isinstance(param, (int, float)):
            formatted = formatted.replace("%s", str(param), 1)
        elif isinstance(param, datetime):
            formatted = formatted.replace("%s", f"'{param}'", 1)
        elif isinstance(param, str):
            formatted = formatted.replace("%s", f"'{param}'", 1)
        else:
            formatted = formatted.replace("%s", f"'{param}'", 1)
    return formatted
