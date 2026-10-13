# -*- coding: utf-8 -*-
"""Bounded historical client text comparisons; no Provider cache inference."""

from datetime import datetime, timedelta, timezone
from time import monotonic

from .diagnostic_contract import CANDIDATE_LIMIT, HISTORY_WINDOW_HOURS
from .diagnostic_storage import read_context
from .store import decode_row, select_columns

GROUP_FIELDS = (
    "source_id",
    "tenant_id",
    "agent_id",
    "provider_id",
    "model_name",
    "assembly_stage",
    "representation_version",
    "key_version",
)
UNKNOWN_BOUNDS = {
    "lower_text_bytes": None,
    "upper_text_bytes": None,
    "first_difference_index": None,
}


def check_deadline(deadline):
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("analysis_timeout")


def valid_blocks(context):
    """Reject damaged intervals instead of deriving a confident zero."""
    if context.get("capture_state") != "complete":
        return False
    encoded, text = 0, 0
    blocks = context.get("blocks", [])
    if not isinstance(blocks, list) or len(blocks) > 1024:
        return False
    for index, block in enumerate(blocks):
        if any(
            (
                block.get("block_index") != index,
                block.get("encoded_start") != encoded,
                block.get("text_start") != text,
                not isinstance(block.get("encoded_end"), int),
                not isinstance(block.get("text_end"), int),
            ),
        ):
            return False
        if (
            not encoded < block["encoded_end"] <= encoded + 256
            or not text <= block["text_end"] <= block["encoded_end"]
            or not isinstance(block.get("chained_digest"), str)
            or len(block["chained_digest"]) != 64
        ):
            return False
        if block["text_end"] - text > block["encoded_end"] - encoded:
            return False
        encoded, text = block["encoded_end"], block["text_end"]
    return encoded == context.get("encoded_bytes") and text == context.get(
        "text_bytes",
    )


def compare_prefix(current, historical):
    """Binary search chained equal blocks, keeping a safe first-difference bound."""
    if not valid_blocks(current) or not valid_blocks(historical):
        return dict(UNKNOWN_BOUNDS)
    for field in (
        "representation_version",
        "key_version",
        "source_id",
        "tenant_id",
    ):
        if current.get(field) != historical.get(field):
            return dict(UNKNOWN_BOUNDS)
    left, right = current["blocks"], historical["blocks"]
    lo, hi = 0, min(len(left), len(right))
    while lo < hi:
        mid = (lo + hi) // 2
        if left[mid]["chained_digest"] == right[mid]["chained_digest"]:
            lo = mid + 1
        else:
            hi = mid
    lower = (
        min(left[lo - 1]["text_end"], right[lo - 1]["text_end"]) if lo else 0
    )
    upper = min(current["text_bytes"], historical["text_bytes"])
    difference = first_difference(current, historical)
    if lo < min(len(left), len(right)):
        upper = min(upper, left[lo]["text_end"], right[lo]["text_end"])
    return {
        "lower_text_bytes": lower,
        "upper_text_bytes": upper,
        "first_difference_index": difference,
    }


def first_difference(current, historical):
    left, right = current.get("occurrences", []), historical.get(
        "occurrences",
        [],
    )
    for index, (one, two) in enumerate(zip(left, right)):
        for key in ("content_digest", "structure_digest"):
            if not one.get(key) or not two.get(key):
                return None
        if any(
            one[key] != two[key]
            for key in ("content_digest", "structure_digest")
        ):
            return index
    return min(len(left), len(right)) if len(left) != len(right) else None


def utc_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("missing_request_time")
    if value.tzinfo:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


async def select_history(cursor, current, *, deadline=None):
    """Caller owns the consistent transaction; fetch one sentinel for coverage."""
    check_deadline(deadline)
    if any(not current.get(key) for key in GROUP_FIELDS):
        return {
            "contexts": [],
            "candidate_limited": False,
            "reason_code": "missing_identity",
        }
    start = utc_time(current.get("request_started_at"))
    group = " AND ".join(f"{field}=%s" for field in GROUP_FIELDS)
    await cursor.execute(
        f"SELECT {select_columns('swe_token_contexts')} FROM swe_token_contexts WHERE {group} AND context_role='prepared' AND capture_state='complete' AND execution_state<>'running' AND request_ended_at<%s AND request_ended_at>=%s ORDER BY request_ended_at DESC,call_id DESC LIMIT %s",
        (
            *[current[field] for field in GROUP_FIELDS],
            start,
            start - timedelta(hours=HISTORY_WINDOW_HOURS),
            CANDIDATE_LIMIT + 1,
        ),
    )
    rows = [decode_row(row) for row in await cursor.fetchall()]
    selected = []
    for row in rows[:CANDIDATE_LIMIT]:
        check_deadline(deadline)
        # The scalar comparison remains mandatory even with a compatibility digest.
        if any(row.get(key) != current.get(key) for key in GROUP_FIELDS):
            raise ValueError("incompatible_history")
        context = await read_context(
            cursor,
            row["context_id"],
            (current["source_id"], current["tenant_id"]),
        )
        if context is None:
            raise ValueError("evidence_expired")
        selected.append(context)
    check_deadline(deadline)
    return {
        "contexts": selected,
        "candidate_limited": len(rows) > CANDIDATE_LIMIT,
        "reason_code": None if selected else "no_history",
    }


def build_comparison(
    current,
    historical,
    *,
    snapshot_id,
    input_revision,
    candidate_limited=False,
    deadline=None,
):
    check_deadline(deadline)
    if len(historical) > CANDIDATE_LIMIT:
        raise ValueError("prefix_candidate_limit")
    candidates = []
    for row in historical:
        check_deadline(deadline)
        candidates.append(
            {
                "historical_context_id": row["context_id"],
                "historical_context_revision": row["context_revision"],
                "historical_call_id": row.get("call_id"),
                "historical_ended_at": row.get("request_ended_at"),
                **compare_prefix(current, row),
            },
        )
    valid = [row for row in candidates if row["lower_text_bytes"] is not None]
    best = max(valid, key=lambda row: row["lower_text_bytes"], default=None)
    upper = max(valid, key=lambda row: row["upper_text_bytes"], default=None)
    reason = comparison_reason(current, candidates, candidate_limited)
    check_deadline(deadline)
    return {
        **{
            key: current.get(key)
            for key in (
                "task_run_id",
                "branch_run_id",
                "source_id",
                "tenant_id",
                "call_id",
                "representation_version",
                "key_version",
                "compatibility_digest",
            )
        },
        "current_context_id": current["context_id"],
        "current_context_revision": current["context_revision"],
        "snapshot_id": snapshot_id,
        "input_revision": input_revision,
        "window_hours": HISTORY_WINDOW_HOURS,
        "candidate_limit": CANDIDATE_LIMIT,
        "candidate_count": len(candidates),
        "candidate_limited": candidate_limited,
        "selection_version": 1,
        "best_lower_context_id": (
            best["historical_context_id"] if best else None
        ),
        "max_upper_context_id": (
            upper["historical_context_id"] if upper else None
        ),
        "lower_text_bytes": best["lower_text_bytes"] if best else None,
        # A missing upper for any selected candidate cannot prove a low maximum.
        "upper_text_bytes": (
            upper["upper_text_bytes"]
            if upper and len(valid) == len(candidates)
            else None
        ),
        "total_text_bytes": current.get("text_bytes"),
        "first_difference_index": (
            best["first_difference_index"] if best else None
        ),
        "analysis_state": (
            "ready"
            if reason in (None, "candidate_limit", "empty_text", "no_history")
            else "insufficient_evidence"
        ),
        "reason_code": reason,
        "eligible": reason is None,
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "candidates": candidates,
    }


def comparison_reason(current, candidates, limited):
    if (
        not current.get("request_ended_at")
        or current.get("execution_state") == "running"
    ):
        return "missing_progress"
    if current.get("capture_state") != "complete":
        return current.get("capture_reason") or "partial_context"
    if limited:
        return "candidate_limit"
    if not candidates:
        return "no_history"
    if any(row["lower_text_bytes"] is None for row in candidates):
        return "incompatible_version"
    if not current.get("text_bytes"):
        return "empty_text"
    return None


def exclusion_reason(row):
    state = row.get("state", row.get("analysis_state"))
    if state in ("pending", "running"):
        return "pending"
    if state in ("failed", "unavailable"):
        return "failed"
    if row.get("candidate_limited"):
        return "candidate_limit"
    if row.get("capture_state") != "complete":
        return (
            "media"
            if row.get("capture_reason") == "media"
            else "partial_context"
        )
    if not row.get("candidate_count"):
        return "no_history"
    if state != "ready" or row.get("lower_text_bytes") is None:
        return "missing_evidence"
    if not row.get("text_bytes", row.get("total_text_bytes")):
        return "empty_text"
    return None


def aggregate_prefix(comparisons):
    matched = total = count = 0
    excluded = {
        reason: 0
        for reason in (
            "pending",
            "failed",
            "candidate_limit",
            "media",
            "partial_context",
            "missing_evidence",
            "no_history",
            "empty_text",
        )
    }
    for row in comparisons:
        reason = exclusion_reason(row)
        if reason:
            excluded[reason] += 1
            continue
        size = row.get("text_bytes", row.get("total_text_bytes"))
        lower = row["lower_text_bytes"]
        if (
            not isinstance(size, int)
            or not isinstance(lower, int)
            or not 0 <= lower <= size
        ):
            excluded["missing_evidence"] += 1
            continue
        matched += lower
        total += size
        count += 1
    return {
        "matched_text_bytes": matched,
        "eligible_text_bytes": total,
        "request_count": count,
        "ratio": matched / total if total else None,
        "excluded": excluded,
    }
