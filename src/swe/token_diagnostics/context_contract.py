# -*- coding: utf-8 -*-
"""Bounded metadata contract for client-side context evidence, never text."""

from typing import Literal, TypedDict, cast

from .contract import B_SCHEMA_VERSION

REPRESENTATION_VERSION = "client-ordered-text-v1"
PREFIX_BLOCK_BYTES = 256
CONTEXT_MAX_BYTES = 256 * 1024
CONTEXT_MAX_BLOCKS = 1024
CONTEXT_CAPTURE_SECONDS = 0.005
CONTEXT_MAX_ITEMS = 2000

CaptureState = Literal["complete", "partial", "unavailable"]
CAPTURE_STATES = frozenset({"complete", "partial", "unavailable"})
CaptureReason = Literal[
    "item_limit",
    "byte_limit",
    "time_budget",
    "missing_key",
    "media",
    "unsupported_content",
    "capture_error",
    "disabled",
    "queue_dropped",
]
ContextRole = Literal["prepared", "before", "after"]
Category = Literal[
    "system",
    "user",
    "assistant",
    "tool_result",
    "tools",
    "inject",
    "skill",
    "memory",
    "unclassified",
]
Attribution = Literal["explicit", "structure", "role", "unknown"]


class ContextOccurrence(TypedDict):
    """One ordered appearance; unknown source identity remains nullable."""

    occurrence_index: int
    source_object_id: str | None
    source_version: str | None
    category: Category
    attribution: Attribution
    content_digest: str | None
    structure_digest: str | None
    text_start: int | None
    text_end: int | None
    encoded_start: int | None
    encoded_end: int | None
    token_start: int | None
    token_end: int | None


class PrefixBlock(TypedDict):
    """A chained digest with cumulative half-open byte intervals."""

    block_index: int
    chained_digest: str
    encoded_start: int
    encoded_end: int
    text_start: int
    text_end: int


class CapturedContext(TypedDict):
    context_id: str
    context_role: ContextRole
    representation_version: str
    estimator_version: str
    key_version: str
    capture_state: CaptureState
    capture_reason: CaptureReason | None
    text_bytes: int | None
    encoded_bytes: int | None
    expected_item_count: int | None
    occurrences: list[ContextOccurrence]
    blocks: list[PrefixBlock]


def validate_capture_state(state) -> CaptureState:
    if not isinstance(state, str) or state not in CAPTURE_STATES:
        raise ValueError("Unsupported token diagnostic capture state")
    return cast(CaptureState, state)


def context_item_limit(config) -> int:
    """Permit a smaller A item limit, never a larger B capture budget."""
    limit = config.max_items
    if type(limit) is not int:
        return CONTEXT_MAX_ITEMS
    return max(1, min(limit, CONTEXT_MAX_ITEMS))
