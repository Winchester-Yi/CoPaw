# -*- coding: utf-8 -*-
"""Authenticate durable, metadata-only evidence events stored with messages."""

import hashlib
import hmac
import json
import os
from typing import Any


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _keys() -> tuple[str, dict[str, bytes]]:
    key_id = os.environ.get("SWE_AGENTOBS_OUTBOX_KEY_ID", "")
    secret = os.environ.get("SWE_AGENTOBS_OUTBOX_SECRET", "").encode("utf-8")
    raw = os.environ.get("SWE_AGENTOBS_OUTBOX_VERIFY_KEYS", "{}")
    values = json.loads(raw)
    if not isinstance(values, dict) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in values.items()
    ):
        raise ValueError("Invalid observability outbox verification keys")
    keys = {k: v.encode("utf-8") for k, v in values.items()}
    if (
        not key_id
        or len(key_id) > 128
        or len(secret) < 32
        or any(len(v) < 32 for v in keys.values())
    ):
        raise ValueError("Observability outbox key unavailable")
    if key_id in keys and not hmac.compare_digest(keys[key_id], secret):
        raise ValueError("Conflicting observability outbox key")
    keys[key_id] = secret
    return key_id, keys


def sign_event(event: dict[str, Any]) -> dict[str, Any]:
    key_id, keys = _keys()
    body = {"version": 1, "key_id": key_id, "event": event}
    return {
        **body,
        "mac": hmac.new(
            keys[key_id],
            _canonical(body),
            hashlib.sha256,
        ).hexdigest(),
    }


def verify_event(record: dict[str, Any]) -> dict[str, Any]:
    _, keys = _keys()
    if (
        not isinstance(record, dict)
        or set(record) != {"version", "key_id", "event", "mac"}
        or type(record["version"]) is not int
        or record["version"] != 1
        or not isinstance(record["key_id"], str)
    ):
        raise ValueError("Invalid observability outbox record")
    key = keys.get(record["key_id"])
    if (
        key is None
        or not isinstance(record["event"], dict)
        or not isinstance(record["mac"], str)
    ):
        raise ValueError("Unknown observability outbox key")
    body = {k: record[k] for k in ("version", "key_id", "event")}
    expected = hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, record["mac"]):
        raise ValueError("Invalid observability outbox signature")
    return record["event"]
