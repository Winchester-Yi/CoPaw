# -*- coding: utf-8 -*-
"""Helpers for sanitizing filesystem-derived strings for JSON responses."""

from __future__ import annotations

import os
from dataclasses import dataclass
from logging import Logger
from typing import Any


@dataclass(frozen=True)
class SanitizedFsText:
    """Result of sanitizing a filesystem-derived string."""

    value: str
    changed: bool
    strategy: str = "utf8"


def _is_utf8_safe(text: str) -> bool:
    """Return True when a string can be encoded to UTF-8."""
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def sanitize_fs_text(text: str) -> SanitizedFsText:
    """Convert filesystem-derived text into JSON-safe UTF-8 text."""
    if _is_utf8_safe(text):
        return SanitizedFsText(value=text, changed=False)

    raw = os.fsencode(text)
    for encoding in ("gb18030", "gbk", "big5"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _is_utf8_safe(decoded):
            return SanitizedFsText(
                value=decoded,
                changed=True,
                strategy=encoding,
            )

    return SanitizedFsText(
        value=raw.decode("utf-8", errors="replace"),
        changed=True,
        strategy="replace",
    )


def _replace_surrogates(text: str) -> str:
    """Replace unsupported surrogate code points with Unicode replacement."""
    return text.encode(
        "utf-8",
        errors="surrogatepass",
    ).decode("utf-8", errors="replace")


def sanitize_text_for_json(text: str) -> SanitizedFsText:
    """Convert arbitrary text into a UTF-8-safe JSON string."""
    if _is_utf8_safe(text):
        return SanitizedFsText(value=text, changed=False)

    try:
        sanitized = sanitize_fs_text(text)
    except UnicodeEncodeError:
        sanitized = SanitizedFsText(
            value=_replace_surrogates(text),
            changed=True,
            strategy="replace",
        )

    if _is_utf8_safe(sanitized.value):
        return sanitized

    return SanitizedFsText(
        value=_replace_surrogates(text),
        changed=True,
        strategy="replace",
    )


def sanitize_json_payload(value: Any) -> tuple[Any, int]:
    """Recursively sanitize JSON-like data and count changed strings."""
    if isinstance(value, str):
        sanitized = sanitize_text_for_json(value)
        return sanitized.value, int(sanitized.changed)

    if isinstance(value, list):
        changed = 0
        sanitized_items = []
        for item in value:
            sanitized_item, item_changed = sanitize_json_payload(item)
            sanitized_items.append(sanitized_item)
            changed += item_changed
        return sanitized_items, changed

    if isinstance(value, tuple):
        changed = 0
        sanitized_items = []
        for item in value:
            sanitized_item, item_changed = sanitize_json_payload(item)
            sanitized_items.append(sanitized_item)
            changed += item_changed
        return tuple(sanitized_items), changed

    if isinstance(value, dict):
        changed = 0
        sanitized_dict = {}
        for key, item in value.items():
            sanitized_key, key_changed = (
                sanitize_json_payload(key)
                if isinstance(key, str)
                else (key, 0)
            )
            sanitized_item, item_changed = sanitize_json_payload(item)
            sanitized_dict[sanitized_key] = sanitized_item
            changed += key_changed + item_changed
        return sanitized_dict, changed

    return value, 0


def log_sanitized_fs_text(
    logger: Logger,
    *,
    source: str,
    original: str,
    sanitized: SanitizedFsText,
) -> None:
    """Log when a filesystem-derived string required sanitization."""
    if not sanitized.changed:
        return

    logger.warning(
        "Sanitized filesystem text in %s: original=%r sanitized=%r strategy=%s",
        source,
        original,
        sanitized.value,
        sanitized.strategy,
    )
