# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from swe.utils.fs_text import (
    log_sanitized_fs_text,
    sanitize_fs_text,
    sanitize_json_payload,
    sanitize_text_for_json,
)


def test_sanitize_fs_text_recovers_surrogate_string() -> None:
    raw = b"\xbf.md".decode("utf-8", errors="surrogateescape")

    sanitized = sanitize_fs_text(raw)

    assert sanitized.changed is True
    assert sanitized.strategy in {"gb18030", "gbk", "big5", "replace"}
    assert sanitized.value
    sanitized.value.encode("utf-8")


def test_log_sanitized_fs_text_only_logs_when_changed() -> None:
    calls: list[tuple] = []

    class FakeLogger:
        def warning(self, *args, **kwargs):
            calls.append((args, kwargs))

    logger = FakeLogger()

    unchanged = sanitize_fs_text("normal.md")
    log_sanitized_fs_text(
        logger,
        source="test.normal",
        original="normal.md",
        sanitized=unchanged,
    )

    changed = sanitize_fs_text(
        b"\xbf.md".decode("utf-8", errors="surrogateescape"),
    )
    log_sanitized_fs_text(
        logger,
        source="test.surrogate",
        original=b"\xbf.md".decode("utf-8", errors="surrogateescape"),
        sanitized=changed,
    )

    assert len(calls) == 1
    assert "Sanitized filesystem text in %s" in calls[0][0][0]


def test_sanitize_text_for_json_handles_non_filesystem_surrogate() -> None:
    raw = "\ud800broken"

    sanitized = sanitize_text_for_json(raw)

    assert sanitized.changed is True
    assert sanitized.strategy == "replace"
    sanitized.value.encode("utf-8")
    assert "\ud800" not in sanitized.value


def test_sanitize_json_payload_recovers_nested_surrogate_strings() -> None:
    raw = b"\xc4\xe3\xba\xc3.md".decode("utf-8", errors="surrogateescape")
    payload = {
        "messages": [{"role": "user", "content": raw}],
        "metadata": {
            "path": raw,
            "items": [raw],
        },
    }

    sanitized, changed = sanitize_json_payload(payload)

    assert changed == 3
    for value in (
        sanitized["messages"][0]["content"],
        sanitized["metadata"]["path"],
        sanitized["metadata"]["items"][0],
    ):
        assert value != raw
        value.encode("utf-8")
    json.dumps(sanitized, ensure_ascii=False).encode("utf-8")
