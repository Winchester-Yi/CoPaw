# -*- coding: utf-8 -*-
"""Scoped exact-content identities and request-local provenance sidecars."""

import hashlib
import hmac
import json
from contextvars import ContextVar

_origins: ContextVar[dict | None] = ContextVar("token_origins", default=None)


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
        config.fingerprint_key.encode(), text.encode(), hashlib.sha256
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
