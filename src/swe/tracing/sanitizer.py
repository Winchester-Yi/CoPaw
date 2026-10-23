# -*- coding: utf-8 -*-
"""Tracing 数据脱敏工具。"""

from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
from typing import Any, Optional

# Sensitive keys to redact from tool input/output
SENSITIVE_KEYS = frozenset(
    [
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "credential",
        "private_key",
        "access_token",
        "refresh_token",
        "session_id",
        "auth",
        "private-key",
        "privatekey",
        "secret_key",
        "secretkey",
        "api_secret",
        "apisecret",
    ],
)

_runtime_secret_values: ContextVar[tuple[str, ...]] = ContextVar(
    "runtime_secret_values",
    default=(),
)


@dataclass(frozen=True)
class SanitizedTraceValue:
    """A JSON-compatible trace value and its bounding metadata."""

    value: Any
    original_bytes: int
    truncated: bool


_TRACE_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|"
        r"password|session[_-]?token)\b\s*[:=]\s*([^\s,;]+)",
    ),
    re.compile(
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@",
    ),
    re.compile(
        r"-----BEGIN\s+[^\r\n-]*PRIVATE\s+KEY-----[\s\S]*?"
        r"-----END\s+[^\r\n-]*PRIVATE\s+KEY-----",
    ),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b",
    ),
)


def _redact_trace_text(text: str) -> str:
    """Redact registered secrets and credential-like text fragments."""
    redacted = _redact_registered_values(text)
    for pattern in _TRACE_TEXT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _truncate_trace_text(text: str, max_bytes: int) -> tuple[str, bool]:
    if max_bytes <= 0:
        return "", bool(text)
    if len(text.encode("utf-8")) <= max_bytes:
        return text, False

    marker = "..."
    marker_bytes = len(marker.encode("utf-8"))
    if max_bytes <= marker_bytes:
        return marker[:max_bytes], True

    prefix = text.encode("utf-8")[: max_bytes - marker_bytes].decode(
        "utf-8",
        errors="ignore",
    )
    return prefix + marker, True


def _trace_json_bytes(value: Any) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
        )
    except Exception:
        return len(str(value).encode("utf-8", errors="replace"))


def sanitize_trace_value(
    value: Any,
    *,
    max_bytes: int = 2048,
    max_depth: int = 5,
    max_items: int = 32,
) -> SanitizedTraceValue:
    """Return a JSON-compatible, redacted and bounded trace value."""
    original_bytes = _trace_json_bytes(value)
    truncated = False

    def visit(current: Any, depth: int) -> Any:
        nonlocal truncated
        if depth > max_depth:
            truncated = True
            return "<max-depth-exceeded>"

        if current is None or isinstance(current, (bool, int, float)):
            return current
        if isinstance(current, str):
            bounded, was_truncated = _truncate_trace_text(
                _redact_trace_text(current),
                max_bytes,
            )
            truncated = truncated or was_truncated
            return bounded

        model_dump = getattr(current, "model_dump", None)
        if callable(model_dump):
            try:
                current = model_dump(mode="json")
            except Exception:
                try:
                    current = model_dump()
                except Exception:
                    return _truncate_trace_text(
                        _redact_trace_text(str(current)),
                        max_bytes,
                    )[0]

        if isinstance(current, dict):
            result: dict[str, Any] = {}
            for index, (key, item) in enumerate(current.items()):
                if index >= max_items:
                    truncated = True
                    break
                key_text = str(key)
                if any(
                    marker in key_text.lower() for marker in SENSITIVE_KEYS
                ):
                    result[key_text] = "[REDACTED]"
                else:
                    result[key_text] = visit(item, depth + 1)
            return result

        if isinstance(current, (list, tuple, set, frozenset)):
            items = list(current)
            if len(items) > max_items:
                truncated = True
            return [visit(item, depth + 1) for item in items[:max_items]]

        try:
            json.dumps(current, ensure_ascii=False)
            return current
        except (TypeError, ValueError):
            bounded, was_truncated = _truncate_trace_text(
                _redact_trace_text(str(current)),
                max_bytes,
            )
            truncated = truncated or was_truncated
            return bounded

    safe_value = visit(value, 0)
    return SanitizedTraceValue(
        value=safe_value,
        original_bytes=original_bytes,
        truncated=truncated,
    )


def register_sensitive_values(values: Any) -> None:
    """登记当前上下文需要按值脱敏的 secret。"""
    existing = list(_runtime_secret_values.get())
    for value in values or ():
        if not isinstance(value, str) or not value:
            continue
        if value not in existing:
            existing.append(value)
    _runtime_secret_values.set(tuple(existing))


def _redact_registered_values(text: str) -> str:
    """按当前上下文登记的 secret 值执行最小替换。"""
    redacted = text
    for secret in _runtime_secret_values.get():
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def sanitize_dict(
    data: Optional[dict[str, Any]],
    max_length: int = 500,
) -> Optional[dict]:
    """按 key 和已登记 secret 值清理字典。"""
    if data is None:
        return None

    result = {}
    for key, value in data.items():
        key_lower = key.lower()
        # Check if key contains any sensitive keyword
        if any(sensitive in key_lower for sensitive in SENSITIVE_KEYS):
            result[key] = "[REDACTED]"
        elif isinstance(value, str):
            result[key] = sanitize_string(value, max_length)
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value, max_length)
        elif isinstance(value, list):
            result[key] = [
                (
                    sanitize_dict(item, max_length)
                    if isinstance(item, dict)
                    else (
                        sanitize_string(item, max_length)
                        if isinstance(item, str)
                        else item
                    )
                )
                for item in value
            ]
        else:
            result[key] = value
    return result


def sanitize_string(
    text: Optional[str],
    max_length: int = 500,
) -> Optional[str]:
    """截断字符串，并替换当前上下文已登记的 secret 值。"""
    if text is None:
        return None
    text = _redact_registered_values(text)
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def sanitize_user_message(
    message: Optional[str],
    max_length: int = 500,
) -> Optional[str]:
    """清理用户消息后再落入 tracing。"""
    return sanitize_string(message, max_length)


def capture_runtime_secret_values() -> tuple[str, ...]:
    """Capture immutable secret values without retaining trace objects."""
    return tuple(_runtime_secret_values.get())
