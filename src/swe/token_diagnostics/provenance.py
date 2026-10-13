# -*- coding: utf-8 -*-
"""Scoped exact-content identities and request-local provenance sidecars."""

import hashlib
import hmac
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_origins: ContextVar[dict | None] = ContextVar("token_origins", default=None)


@dataclass(frozen=True)
class _Source:
    borrow: "_BorrowLease"
    digest: str
    scope: tuple[str, str]
    key_version: str
    metadata: tuple[str, str, str]
    parent: "_Source | None"
    depth: int


@dataclass
class _BorrowLease:
    """Shared lifetime across copied ContextVars, without retained payload."""

    identity: object | None
    active: bool = True

    def release(self):
        self.active = False
        self.identity = None


_sources: ContextVar[_Source | None] = ContextVar(
    "token_sources",
    default=None,
)


def canonical(value) -> str:
    return (
        value
        if isinstance(value, str)
        else json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def fingerprint(value, scope: str, config) -> str | None:
    if not config.fingerprint_key:
        return None
    text = canonical([config.key_version, scope, canonical(value)])
    return hmac.new(
        config.fingerprint_key.encode(),
        text.encode(),
        hashlib.sha256,
    ).hexdigest()


def register_origin(value, scope, config, category, source_ref):
    identity = fingerprint(value, scope, config)
    if identity:
        origins = _origins.get()
        if origins is None:
            origins = {}
            _origins.set(origins)
        if identity not in origins and len(origins) >= config.max_items:
            return
        origins[identity] = {
            "category": category,
            "source_ref": source_ref[:256],
        }


def find_origin(identity):
    return (_origins.get() or {}).get(identity)


def bind_origins(origins):
    return _origins.set(origins)


def reset_origins(token):
    _origins.reset(token)


@contextmanager
def register_source(
    value,
    scope,
    config,
    *,
    source_object_id,
    source_version,
    category,
):
    """Borrow identity only around assembly/capture, release it in finally.

    Callers must supply reliable IDs/versions, never tool call IDs alone.
    Entries are immutable, bounded and request-local. They are not persisted
    in Branch.origins; equal-content copies cannot inherit provenance.
    A shared borrow lease expires in every copied async context on exit.
    """
    from .context_capture import source_content_digest
    from .context_contract import Category, context_item_limit
    from typing import get_args

    sources = _sources.get()
    valid = (
        type(source_object_id) is str
        and 0 < len(source_object_id) <= 256
        and type(source_version) is str
        and 0 < len(source_version) <= 256
        and type(category) is str
        and category in get_args(Category)
    )
    depth = sources.depth if sources else 0
    if not valid or depth >= context_item_limit(config):
        yield
        return
    digest = source_content_digest(value, scope, config, category)
    if digest is None:
        yield
        return
    # A persistent linked stack borrows each object once. Nested immutable
    # scopes therefore use O(items) storage, not copied O(items**2) tuples.
    source = _Source(
        _BorrowLease(value),
        digest,
        scope,
        config.key_version,
        (source_object_id, source_version, category),
        sources,
        depth + 1,
    )
    token = _sources.set(source)
    try:
        yield
    finally:
        source.borrow.release()
        _sources.reset(token)


def find_source(value, digest, scope, key_version):
    """Identity first, then exact content and scope/version verification."""
    source = _sources.get()
    while source:
        if source.borrow.active and source.borrow.identity is value:
            if (source.digest, source.scope, source.key_version) == (
                digest,
                scope,
                key_version,
            ):
                object_id, version, category = source.metadata
                return {
                    "source_object_id": object_id,
                    "source_version": version,
                    "category": category,
                }
            return None
        source = source.parent
    return None
