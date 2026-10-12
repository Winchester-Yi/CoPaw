# -*- coding: utf-8 -*-
"""Request-local search decisions, including child Agent tasks."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .config import JevConfig


@dataclass(frozen=True)
class JevRequestContext:
    config: JevConfig
    task: str


_current: ContextVar[JevRequestContext | None] = ContextVar(
    "jev_request_context",
    default=None,
)


def current_jev_context() -> JevRequestContext | None:
    return _current.get()


@contextmanager
def jev_context(config: JevConfig | None, task: str):
    value = (
        JevRequestContext(config.model_copy(deep=True), task)
        if config is not None and config.search_filtering.enabled
        else None
    )
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)
