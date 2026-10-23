# -*- coding: utf-8 -*-
"""Create summary tasks with a small, explicit context allowlist."""

import asyncio
from contextvars import Context

from swe.config.context import capture_summary_context_values
from swe.constant import capture_env_var_overrides, install_env_var_overrides
from swe.tracing import capture_current_trace_context
from swe.tracing.sanitizer import (
    capture_runtime_secret_values,
    register_sensitive_values,
)


def create_summary_task(coroutine):
    """Support Python 3.10 without copying the parent request Context."""
    values = capture_summary_context_values()
    overrides = capture_env_var_overrides()
    secrets = capture_runtime_secret_values()
    context = Context()
    for variable, value in values:
        context.run(variable.set, value)
    context.run(install_env_var_overrides, overrides)
    context.run(register_sensitive_values, secrets)
    return context.run(asyncio.create_task, coroutine)


def capture_summary_trace_context() -> dict | None:
    """Copy plain trace metadata for explicit model-factory binding."""
    trace = capture_current_trace_context()
    return dict(trace) if trace is not None else None
