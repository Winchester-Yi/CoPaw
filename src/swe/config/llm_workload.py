# -*- coding: utf-8 -*-
"""Context helpers for classifying LLM call workloads."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generator, Literal, TypeAlias

LLMWorkload: TypeAlias = Literal["chat", "cron"]

LLM_WORKLOAD_CHAT: LLMWorkload = "chat"
LLM_WORKLOAD_CRON: LLMWorkload = "cron"
_VALID_LLM_WORKLOADS: frozenset[str] = frozenset(
    (LLM_WORKLOAD_CHAT, LLM_WORKLOAD_CRON),
)

_current_llm_workload: ContextVar[LLMWorkload] = ContextVar(
    "swe_current_llm_workload",
    default=LLM_WORKLOAD_CHAT,
)


def normalize_llm_workload(workload: str | None) -> LLMWorkload:
    """Validate and normalize an LLM workload identity."""
    if workload is None:
        return get_current_llm_workload()
    if workload not in _VALID_LLM_WORKLOADS:
        raise ValueError(f"Unsupported LLM workload: {workload!r}")
    return workload  # type: ignore[return-value]


def get_current_llm_workload() -> LLMWorkload:
    """Return the current LLM workload, defaulting to chat."""
    return _current_llm_workload.get()


def capture_llm_workload_context_value() -> (
    tuple[ContextVar[LLMWorkload], LLMWorkload]
):
    """Capture the workload ContextVar and its scalar value for a child task."""
    return _current_llm_workload, _current_llm_workload.get()


@contextmanager
def bind_llm_workload(
    workload: LLMWorkload,
) -> Generator[None, None, None]:
    """Temporarily bind the LLM workload for model calls in this context."""
    normalized = normalize_llm_workload(workload)
    token = _current_llm_workload.set(normalized)
    try:
        yield
    finally:
        _current_llm_workload.reset(token)
