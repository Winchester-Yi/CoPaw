# -*- coding: utf-8 -*-
"""Request-local exact occurrence cache; SQL validation always remains authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_CACHE = ContextVar("conversation_projection_cache", default=None)


@contextmanager
def cache_scope():
    token = _CACHE.set({})
    try:
        yield
    finally:
        _CACHE.reset(token)


def cached(key):
    cache = _CACHE.get()
    return deepcopy(cache.get(key)) if cache is not None else None


def active():
    return _CACHE.get() is not None


def remember(key, value):
    cache = _CACHE.get()
    if cache is not None:
        cache.clear()
        cache[key] = deepcopy(value)


def cache_key(row, revision=None):
    return (
        *(row[key] for key in ("tenant_key", "source_key", "agent_key")),
        row["session_id"] if "session_id" in row else row["id"],
        row["base_state_revision"] if revision is None else revision,
    )
