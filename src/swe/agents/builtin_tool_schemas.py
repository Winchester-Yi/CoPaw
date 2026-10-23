# -*- coding: utf-8 -*-
"""Reuse static SDK schemas without sharing request-owned toolkit objects."""

import copy
from functools import lru_cache
from typing import Any, Callable

from agentscope.tool import Toolkit

from .tools import (
    ask_plan_clarification,
    copy_file_to_static,
    edit_file,
    emit_wplus_sop_event,
    execute_shell_command,
    get_current_time,
    glob_search,
    grep_search,
    read_file,
    update_task_progress,
    write_file,
)

_BUILTINS = (
    execute_shell_command,
    read_file,
    write_file,
    edit_file,
    grep_search,
    glob_search,
    get_current_time,
    copy_file_to_static,
    update_task_progress,
    emit_wplus_sop_event,
    ask_plan_clarification,
)


@lru_cache(maxsize=32)
def _schema_for_function(function: Callable) -> dict[str, Any]:
    toolkit = Toolkit()
    toolkit.register_tool_function(function)
    return toolkit.tools[function.__name__].json_schema


def get_builtin_tool_schema(function: Callable) -> dict[str, Any] | None:
    """Return a private schema only for the fixed built-in function objects."""
    if not any(function is builtin for builtin in _BUILTINS):
        return None
    return copy.deepcopy(_schema_for_function(function))


def prepare_builtin_tool_schemas() -> None:
    """Warm static schemas on the existing Query preparation worker."""
    for function in _BUILTINS:
        _schema_for_function(function)
