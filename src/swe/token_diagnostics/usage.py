# -*- coding: utf-8 -*-
"""Normalize only allowlisted Provider numeric usage, never raw responses."""

from collections.abc import Mapping

from .models import UsageSnapshot


def get_field(value, key, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def first_count(value, *names):
    for name in names:
        count = token_count(get_field(value, name))
        if count is not None:
            return count
    return None


def extract_usage(response) -> UsageSnapshot:
    metadata = get_field(response, "metadata", {}) or {}
    raw = get_field(response, "raw", {}) or {}
    usage = get_field(metadata, "usage")
    if usage is None:
        usage = get_field(response, "usage")
    if usage is None:
        usage = get_field(raw, "usage")
    details = {}
    for target, names in {
        "cache_read_tokens": ("cache_read_input_tokens", "cache_read_tokens"),
        "cache_write_tokens": (
            "cache_creation_input_tokens",
            "cache_write_tokens",
        ),
        "reasoning_tokens": ("reasoning_tokens",),
    }.items():
        count = first_count(usage, *names)
        if count is not None:
            details[target] = count
    detail_usage = get_field(usage, "metadata") or usage
    nested = (
        ("prompt_tokens_details", "cached_tokens", "cache_read_tokens"),
        ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    )
    for parent, name, target in nested:
        count = token_count(get_field(get_field(detail_usage, parent), name))
        if count is not None:
            details[target] = count
    # Anthropic input excludes cache-read/write; normalize once at this boundary.
    input_count = first_count(usage, "input_tokens", "prompt_tokens")
    if (
        get_field(usage, "cache_read_input_tokens") is not None
        or get_field(usage, "cache_creation_input_tokens") is not None
    ) and input_count is not None:
        input_count += sum(
            details.get(k, 0)
            for k in ("cache_read_tokens", "cache_write_tokens")
        )
    reason = get_field(response, "finish_reason") or get_field(
        metadata, "finish_reason"
    )
    reason = (
        reason
        or get_field(raw, "stop_reason")
        or get_field(raw, "finish_reason")
    )
    reasons = {
        "stop": "stop",
        "end_turn": "stop",
        "length": "length",
        "max_tokens": "length",
        "tool_calls": "tool_calls",
        "tool_use": "tool_calls",
    }
    return UsageSnapshot(
        input_count,
        first_count(usage, "output_tokens", "completion_tokens"),
        details,
        reasons.get(reason) if isinstance(reason, str) else None,
    )
