# -*- coding: utf-8 -*-
"""Pure B rules over revision-bound metadata, independent of legacy A rules.

Snapshots contain run/calls/contexts/events/steps, prefix_comparisons and
history_contexts. Contexts include ordered occurrences; real business Steps
must explicitly identify their provenance. Missing evidence remains unknown.
Values use the frozen scalar DTO: no ad hoc string values or evidence blobs.
"""

import hashlib
import json
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from .prefix_analysis import check_deadline, utc_time

from .diagnostic_contract import (
    CANDIDATE_LIMIT,
    HISTORY_WINDOW_HOURS,
    RULE_IDS,
    RULE_VERSION,
    THRESHOLD_VERSION,
    Evaluation,
    EvaluationReference,
    EvaluationState,
    EvaluationValue,
    GapReason,
    RuleId,
    ValueKey,
)

STATIC_CATEGORIES = frozenset({"system", "tools", "inject", "skill"})
TRUSTED_ATTRIBUTION = frozenset({"explicit", "structure"})
SIGNATURE_VERSION = 1


def classify_low_prefix(lower, upper, candidate_limited, threshold=0.2):
    """Classify conservative ratio bounds; equality is not a low prefix."""
    if candidate_limited or lower is None or upper is None:
        return "insufficient_evidence"
    if not 0 <= lower <= upper <= 1:
        return "insufficient_evidence"
    if upper < threshold:
        return "candidate"
    if lower >= threshold:
        return "not_hit"
    return "insufficient_evidence"


def _check(snapshot):
    check_deadline(snapshot.get("_deadline"))


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()


def _number(value):
    return type(value) in (int, float) and value >= 0


def _value(
    key: ValueKey,
    amount,
    unit="estimated_tokens",
    threshold=None,
    lower=None,
    upper=None,
) -> EvaluationValue:
    decimal = unit == "ratio"
    return {
        "value_key": key,
        "unit": unit,
        "value_type": "decimal" if decimal else "integer",
        "integer_value": (
            amount if amount is not None and not decimal else None
        ),
        "decimal_value": amount if amount is not None and decimal else None,
        "enum_value": None,
        "threshold": threshold,
        "lower_bound": lower,
        "upper_bound": upper,
    }


def _ref(kind, identity, context=None, index=None) -> EvaluationReference:
    return {
        "reference_type": kind,
        "object_id": identity,
        "context_id": context,
        "occurrence_index": index,
    }


def _evaluation(
    snapshot,
    rule: RuleId,
    *,
    object_type="branch",
    object_id=None,
    state: EvaluationState = "insufficient_evidence",
    values=(),
    gaps: tuple[GapReason, ...] | list[GapReason] = (),
    refs=(),
    grouping=None,
) -> Evaluation:
    run = snapshot["run"]
    identity = object_id or run["branch_run_id"]
    revision = snapshot.get("input_revision", run.get("data_revision", 0))
    snapshot_id = snapshot["snapshot_id"]
    evaluation_id = str(
        uuid5(
            NAMESPACE_URL,
            _digest(
                [
                    run.get("source_id"),
                    run.get("tenant_id"),
                    object_type,
                    identity,
                    rule,
                    RULE_VERSION,
                    THRESHOLD_VERSION,
                    revision,
                    snapshot_id,
                ],
            ),
        ),
    )
    findings = []
    if state == "candidate":
        scope = _group_scope(snapshot, object_type, identity)
        reliable = all(scope) and grouping is not None
        signature = (
            _digest(
                [
                    *scope,
                    rule,
                    RULE_VERSION,
                    THRESHOLD_VERSION,
                    SIGNATURE_VERSION,
                    grouping,
                ],
            )
            if reliable
            else None
        )
        instance = _digest([object_type, identity, grouping])
        findings.append(
            {
                "finding_id": str(
                    uuid5(NAMESPACE_URL, evaluation_id + instance),
                ),
                "instance_key": instance,
                "signature": signature,
                "signature_version": SIGNATURE_VERSION,
                "reliable_grouping": bool(reliable),
                "recommendation_code": rule,
            },
        )
    return {
        "evaluation_id": evaluation_id,
        "object_type": object_type,
        "object_id": identity,
        "rule_id": rule,
        "rule_version": RULE_VERSION,
        "threshold_version": THRESHOLD_VERSION,
        "input_revision": revision,
        "snapshot_id": snapshot_id,
        "state": state,
        "values": list(values),
        "gaps": list(dict.fromkeys(gaps)),
        "refs": list(refs),
        "findings": findings,
    }


def _tokens(item):
    amount = item.get("estimated_tokens")
    if type(amount) is int and amount >= 0:
        return amount
    start, end = item.get("token_start"), item.get("token_end")
    if type(start) is int and type(end) is int and 0 <= start <= end:
        return end - start
    return None


def _attributed(context):
    items = context.get("occurrences", [])
    if not items:
        return None
    if any(
        _tokens(item) is None
        or item.get("attribution") == "unknown"
        or item.get("category") in (None, "unclassified")
        for item in items
    ):
        return None
    return sum(_tokens(item) for item in items)


def _source_key(item):
    fields = ("source_object_id", "source_version", "content_digest")
    if item.get("attribution") not in TRUSTED_ATTRIBUTION:
        return None
    return (
        tuple(item[key] for key in fields)
        if all(item.get(key) for key in fields)
        else None
    )


def _static(context):
    total, keys, missing = 0, {}, False
    for item in context.get("occurrences", []):
        if item.get("category") not in STATIC_CATEGORIES:
            continue
        key, amount = _source_key(item), _tokens(item)
        if key is None or amount is None:
            missing = True
            continue
        total += amount
        keys[key] = keys.get(key, 0) + amount
    return total, keys, missing


def _context_signature(context):
    return [
        context.get(key)
        for key in (
            "representation_version",
            "estimator_version",
            "key_version",
            "assembly_stage",
        )
    ] + [
        [item.get("content_digest"), item.get("structure_digest")]
        for item in context.get("occurrences", [])
    ]


def _rounds(snapshot):
    branch = snapshot["run"]["branch_run_id"]
    contexts = {
        row["context_id"]: row
        for row in snapshot.get("contexts", [])
        if row.get("branch_run_id", branch) == branch
        and row.get("context_role") == "prepared"
    }
    by_call = {row.get("call_id"): row for row in contexts.values()}
    groups, gaps = defaultdict(list), []
    expected_scope = tuple(
        snapshot["run"].get(key) for key in ("source_id", "tenant_id")
    )
    for call in sorted(
        snapshot.get("calls", []),
        key=lambda row: (
            row.get("logical_seq", 0),
            row.get("attempt_no", 0),
            row["call_id"],
        ),
    ):
        _check(snapshot)
        if call.get("branch_run_id", branch) != branch:
            continue
        if (
            tuple(call.get(key) for key in ("source_id", "tenant_id"))
            != expected_scope
        ):
            gaps.append("incompatible_version")
        if call.get("gap_count") not in (None, 0):
            gaps.append("partial_context")
        logical = call.get("logical_call_id")
        if not logical:
            gaps.append("missing_identity")
            continue
        context = contexts.get(call.get("context_id")) or by_call.get(
            call["call_id"],
        )
        if context is None:
            gaps.append("missing_context")
            continue
        if context.get("capture_state") != "complete":
            gaps.append("partial_context")
        if not context.get("key_version"):
            gaps.append("missing_key")
        groups[logical].append((call, context))
    rounds = []
    for attempts in groups.values():
        _check(snapshot)
        signatures = [_context_signature(context) for _, context in attempts]
        if any(signature != signatures[0] for signature in signatures[1:]):
            gaps.append("incompatible_version")
        rounds.append(attempts[-1])
    gaps.extend(_round_sequence_gaps(snapshot, rounds, expected_scope))
    return rounds, list(dict.fromkeys(gaps))


def _contiguous(sequence):
    if not sequence:
        return True
    return all(type(value) is int for value in sequence) and sequence == list(
        range(sequence[0], sequence[-1] + 1),
    )


def _round_sequence_gaps(snapshot, rounds, expected_scope):
    gaps = []
    versions = {
        tuple(_context_signature(context)[:4]) for _, context in rounds
    }
    configurations = {
        tuple(
            call.get(key)
            for key in (
                "agent_id",
                "provider_id",
                "model_name",
                "phase_id",
                "compaction_epoch",
            )
        )
        for call, _ in rounds
    }
    if len(configurations) > 1:
        gaps.append("incompatible_version")
    if snapshot["run"].get("gap_count") not in (None, 0):
        gaps.append("partial_context")
    if any(
        tuple(context.get(key) for key in ("source_id", "tenant_id"))
        != expected_scope
        for _, context in rounds
    ):
        gaps.append("incompatible_version")
    if len(versions) > 1 or any(None in version for version in versions):
        gaps.append("incompatible_version")
    sequence = [call.get("logical_seq") for call, _ in rounds]
    if not _contiguous(sequence):
        gaps.append("missing_progress")
    return gaps


def _round_refs(rounds):
    refs = []
    for call, context in rounds:
        refs += [
            _ref("call", call["call_id"]),
            _ref("context", context["context_id"], context["context_id"]),
        ]
    return refs


def _growth_grouping(snapshot, rounds):
    """Group only controlled sources; dynamic IDs have no cross-Task scope."""
    evidence = []
    for _, context in rounds:
        _check(snapshot)
        occurrences = context.get("occurrences", [])
        if not occurrences:
            return None
        sources = []
        for item in occurrences:
            _check(snapshot)
            key = _source_key(item)
            structure = item.get("structure_digest")
            if (
                item.get("category") not in STATIC_CATEGORIES
                or key is None
                or not structure
            ):
                return None
            sources.append([item["category"], key, structure])
        evidence.append([_context_signature(context)[:4], sources])
    return evidence or None


def _growth(snapshot, rounds, gaps):
    sizes = [context.get("estimated_tokens") for _, context in rounds]
    if any(type(amount) is not int for amount in sizes):
        gaps = [*gaps, "missing_usage"]
    values = [_value("logical_call_count", len(rounds), "count", 3)]
    pressure = None
    for call, context in rounds:
        _check(snapshot)
        limit, reserved = call.get("total_context_limit"), call.get(
            "reserved_output_tokens",
        )
        if (
            call.get("context_limit_trusted") is True
            and _number(limit)
            and limit > 0
            and _number(reserved)
            and _number(context.get("estimated_tokens"))
        ):
            ratio = (context["estimated_tokens"] + reserved) / limit
            pressure = max(pressure or 0, ratio)
    if pressure is not None:
        values.append(_value("context_pressure_ratio", pressure, "ratio", 0.8))
    state = "not_applicable" if len(rounds) < 3 else "not_hit"
    if len(rounds) >= 3 and not gaps:
        # Each maximal strictly growing sequence is a continuous candidate.
        sequences, sequence = [], []
        for amount in sizes:
            _check(snapshot)
            if sequence and amount <= sequence[-1]:
                sequences.append(sequence)
                sequence = []
            sequence.append(amount)
        sequences.append(sequence)
        eligible = [series for series in sequences if len(series) >= 3]
        hits = [
            series
            for series in eligible
            if series[0] > 0
            and series[-1] / series[0] >= 2
            and series[-1] - series[0] >= 8000
        ]
        selected = max(
            hits or eligible,
            key=lambda series: series[-1] - series[0],
            default=sizes,
        )
        first, last = selected[0], selected[-1]
        ratio = last / first if first else None
        if first == 0:
            gaps = [*gaps, "missing_usage"]
        values += [
            _value("first_tokens", first),
            _value("last_tokens", last),
            _value("growth_ratio", ratio, "ratio", 2),
            _value("net_growth_tokens", last - first, threshold=8000),
        ]
        if (
            ratio is not None
            and ratio >= 2
            and last - first >= 8000
            and all(one < two for one, two in zip(selected, selected[1:]))
        ):
            state = "candidate"
    if pressure is not None and pressure >= 0.8:
        state = "candidate"
    if gaps:
        state = "insufficient_evidence"
    return _evaluation(
        snapshot,
        "context_growth",
        state=state,
        values=values,
        gaps=gaps,
        refs=_round_refs(rounds),
        grouping=_growth_grouping(snapshot, rounds),
    )


def _continuous_tail(sequence, round_count):
    tail = []
    for entry in reversed(sequence):
        if entry[0] != round_count - 1 - len(tail):
            break
        tail.append(entry)
    return tail


def _carryover(snapshot, rounds, gaps):
    values, refs, candidate, grouping = [], _round_refs(rounds), False, None
    sequences = defaultdict(list)
    denominator = _attributed(rounds[-1][1]) if rounds else None
    for index, (_, context) in enumerate(rounds):
        _check(snapshot)
        amounts = defaultdict(int)
        for item in context.get("occurrences", []):
            _check(snapshot)
            key = _source_key(item)
            if key is None:
                gaps = [*gaps, "missing_identity"]
            elif _tokens(item) is None:
                gaps = [*gaps, "missing_usage"]
            else:
                amounts[key] += _tokens(item)
        for key, amount in amounts.items():
            _check(snapshot)
            sequences[key].append((index, amount))
    if denominator is None or denominator <= 0:
        gaps = [*gaps, "missing_denominator"]
    best = ()
    for key, sequence in sequences.items():
        _check(snapshot)
        tail = _continuous_tail(sequence, len(rounds))
        if len(tail) >= 3:
            final = tail[0][1]
            if not best or final > best[1]:
                best = (len(tail), final, key)
            if denominator and final >= 5000 and final / denominator >= 0.25:
                candidate, grouping = True, key
    if best:
        values += [
            _value("occurrence_count", best[0], "count", 3),
            _value("repeated_tokens", best[1], threshold=5000),
        ]
    values.append(_value("attributed_input_tokens", denominator))
    state = "candidate" if candidate else "not_hit"
    if len(rounds) < 3:
        state = "not_applicable"
    if gaps:
        state = "insufficient_evidence"
    return _evaluation(
        snapshot,
        "segment_carryover",
        state=state,
        values=values,
        gaps=gaps,
        refs=refs,
        grouping=(
            [_context_signature(rounds[-1][1])[:4], grouping]
            if rounds and grouping
            else None
        ),
    )


def _business_steps(snapshot):
    branch = snapshot["run"]["branch_run_id"]
    return sorted(
        [
            step
            for step in snapshot.get("steps", [])
            if step.get("branch_run_id", branch) == branch
            and step.get("identity_source") == "business"
            and step.get("step_id")
        ],
        key=lambda step: step.get("step_seq", 0),
    )


def _ended(step):
    return step.get("execution_state") in ("completed", "failed", "cancelled")


def _after_step(call, step):
    end = step.get("ended_logical_seq")
    if type(end) is int and type(call.get("logical_seq")) is int:
        return call["logical_seq"] > end
    ended, started = step.get("ended_at"), call.get("started_at")
    return started > ended if ended and started else None


def _tool_volume(snapshot, rounds, gaps):
    if not rounds:
        return _evaluation(
            snapshot,
            "tool_context_volume",
            gaps=[*gaps, "missing_context"],
        )
    call, context = rounds[-1]
    denominator = _attributed(context)
    tools = [
        item
        for item in context.get("occurrences", [])
        if item.get("category") == "tool_result"
    ]
    amounts = [_tokens(item) for item in tools]
    total = (
        sum(amounts) if all(amount is not None for amount in amounts) else None
    )
    ratio = total / denominator if total is not None and denominator else None
    values = [
        _value("tool_result_tokens", total),
        _value("attributed_input_tokens", denominator),
        _value("tool_result_ratio", ratio, "ratio", 0.4),
    ]
    state, grouping = "not_hit", None
    steps = {
        step["step_id"]: step
        for step in _business_steps(snapshot)
        if _ended(step)
    }
    if tools and not steps:
        gaps = [*gaps, "missing_step"]
    if denominator is None:
        gaps = [*gaps, "missing_denominator"]
    if total is None:
        gaps = [*gaps, "missing_usage"]
    count = 0
    for item in tools:
        _check(snapshot)
        key = _source_key(item)
        step = steps.get(item.get("business_step_id"))
        if key is None:
            gaps = [*gaps, "missing_identity"]
            continue
        if step is None:
            gaps = [*gaps, "missing_step"]
            continue
        matches = 0
        for previous_call, previous in reversed(rounds):
            _check(snapshot)
            after = _after_step(previous_call, step)
            if after is None:
                gaps = [*gaps, "missing_progress"]
                break
            if not after:
                break
            if not any(
                _source_key(other) == key
                and other.get("business_step_id") == step["step_id"]
                for other in previous.get("occurrences", [])
            ):
                break
            matches += 1
        count = max(count, matches)
        if matches >= 3 and ratio is not None and ratio >= 0.4:
            state, grouping = "candidate", key
    values.append(_value("occurrence_count", count, "count", 3))
    if gaps:
        state = "insufficient_evidence"
    refs = _round_refs(rounds) + [_ref("step", step_id) for step_id in steps]
    return _evaluation(
        snapshot,
        "tool_context_volume",
        object_type="call",
        object_id=call["call_id"],
        state=state,
        values=values,
        gaps=gaps,
        refs=refs,
        grouping=(
            [_context_signature(context)[:4], grouping] if grouping else None
        ),
    )


def _safe_repeat(snapshot):
    branch = snapshot["run"]["branch_run_id"]
    groups, gaps, seen = defaultdict(list), [], set()
    for event in snapshot.get("events", []):
        _check(snapshot)
        if (
            event.get("branch_run_id", branch) != branch
            or event.get("event_type") != "tool_completed"
        ):
            continue
        operation = event.get("producer_operation_id")
        if not operation:
            gaps.append("missing_identity")
            continue
        if operation in seen:
            continue
        seen.add(operation)
        key = tuple(
            event.get(field)
            for field in (
                "tool_name",
                "arguments_fingerprint",
                "data_version",
                "result_fingerprint",
                "key_version",
            )
        )
        if not all(key):
            gaps.append("missing_identity")
            continue
        groups[key].append(event)
    repeats = [
        (key, events) for key, events in groups.items() if len(events) >= 2
    ]
    safe = [
        (key, events)
        for key, events in repeats
        if all(event.get("tool_kind") == "read" for event in events)
    ]
    if len(safe) < len(repeats):
        gaps.append("missing_tool_properties")
    state = "candidate" if safe else "not_hit"
    if gaps and not safe:
        state = "insufficient_evidence"
    key, events = max(
        safe or repeats,
        key=lambda pair: len(pair[1]),
        default=(None, []),
    )
    return _evaluation(
        snapshot,
        "safe_tool_repeat",
        state=state,
        gaps=gaps,
        values=[
            _value("extra_call_count", max(0, len(events) - 1), "count", 1),
        ],
        refs=[
            _ref("operation", event["producer_operation_id"])
            for event in events
        ],
        grouping=key if safe else None,
    )


def _loop(snapshot):
    steps, gaps = _business_steps(snapshot), []
    if not steps:
        return _evaluation(snapshot, "suspected_loop", gaps=["missing_step"])
    if any(
        not _ended(step)
        or not all(
            step.get(key)
            for key in (
                "action_signature",
                "result_signature",
                "signature_version",
            )
        )
        for step in steps
    ):
        gaps.append("missing_identity")
    step_seq = [step.get("step_seq") for step in steps]
    if any(type(seq) is not int for seq in step_seq) or step_seq != list(
        range(step_seq[0], step_seq[-1] + 1),
    ):
        gaps.append("missing_step")
    state, last, sequence = "not_hit", None, []
    best = []
    for step in steps:
        _check(snapshot)
        key = (
            step.get("action_signature"),
            step.get("result_signature"),
            step.get("signature_version"),
        )
        sequence = [*sequence, step] if key == last else [step]
        last = key
        if len(sequence) >= 3 and len(sequence) > len(best):
            best = sequence
    grouping = None
    if best and not gaps:
        state = "candidate"
        grouping = [
            best[0][key]
            for key in (
                "action_signature",
                "result_signature",
                "signature_version",
            )
        ]
        if any(step.get("progress_version") is None for step in best):
            gaps.append("missing_progress")
        elif len({step["progress_version"] for step in best}) > 1:
            state = "not_hit"
    if "missing_identity" in gaps or "missing_step" in gaps:
        state = "insufficient_evidence"
    elif len(steps) < 3:
        state = "not_applicable"
    return _evaluation(
        snapshot,
        "suspected_loop",
        object_type="step",
        object_id=steps[-1]["step_id"],
        state=state,
        gaps=gaps,
        values=[_value("step_count", len(best), "count", 3)],
        refs=[_ref("step", step["step_id"]) for step in steps],
        grouping=grouping,
    )


def _static_volume(snapshot, call, context, gaps):
    total, keys, missing = _static(context)
    denominator = _attributed(context)
    ratio = total / denominator if denominator else None
    if missing:
        gaps = [*gaps, "missing_static_identity"]
    if denominator is None or denominator <= 0:
        gaps = [*gaps, "missing_denominator"]
    state = (
        "candidate"
        if total >= 4000 and ratio is not None and ratio >= 0.25
        else "not_hit"
    )
    if gaps:
        state = "insufficient_evidence"
    return _evaluation(
        snapshot,
        "static_context_volume",
        object_type="call",
        object_id=call["call_id"],
        state=state,
        gaps=gaps,
        values=[
            _value("static_tokens", total, threshold=4000),
            _value("attributed_input_tokens", denominator),
            _value("static_ratio", ratio, "ratio", 0.25),
        ],
        refs=_round_refs([(call, context)]),
        grouping=[_context_signature(context)[:4], sorted(keys)],
    )


def _historical(snapshot, comparison):
    contexts = comparison.get(
        "historical_contexts",
        snapshot.get("history_contexts", []),
    )
    if isinstance(contexts, dict):
        return contexts
    return {context["context_id"]: context for context in contexts}


def _history_time_valid(current, previous):
    started = current.get("request_started_at")
    ended = previous.get("request_ended_at")
    if started is None or ended is None:
        return False
    try:
        delta = (utc_time(started) - utc_time(ended)).total_seconds()
        return 0 < delta <= HISTORY_WINDOW_HOURS * 3600
    except (TypeError, ValueError):
        return False


def _prefix_header_gaps(context, comparison, history):
    gaps = []
    if not _ended(context):
        gaps.append("missing_progress")
    if context.get("context_revision") is None or comparison.get(
        "current_context_revision",
    ) != context.get("context_revision"):
        gaps.append("evidence_expired")
    if not context.get("request_started_at") or not context.get(
        "request_ended_at",
    ):
        gaps.append("missing_progress")
    candidates = comparison.get("candidates", [])
    if (
        comparison.get("candidate_limited")
        or len(candidates) > CANDIDATE_LIMIT
    ):
        gaps.append("candidate_limit")
    identities = [row["historical_context_id"] for row in candidates]
    if len(set(identities)) != len(identities):
        gaps.append("missing_identity")
    if comparison.get("candidate_count") != len(candidates):
        gaps.append("partial_context")
    if comparison.get("analysis_state") != "ready":
        gaps.append("analysis_failed")
    historical_calls = [
        history.get(row["historical_context_id"], {}).get("call_id")
        for row in candidates
    ]
    if (
        any(call_id is None for call_id in historical_calls)
        or context.get("call_id") in historical_calls
        or len(set(historical_calls)) != len(historical_calls)
    ):
        gaps.append("missing_identity")
    return gaps


def _historical_gaps(context, previous, candidate):
    gaps = []
    if not _ended(previous):
        gaps.append("missing_progress")
    if (
        previous.get("context_revision") is None
        or candidate.get("historical_context_revision") is None
    ):
        gaps.append("evidence_expired")
    if not previous.get("request_ended_at") or not previous.get(
        "request_started_at",
    ):
        gaps.append("missing_progress")
    if not _history_time_valid(context, previous):
        gaps.append("incompatible_version")
    expected_revision = candidate.get("historical_context_revision")
    if (
        expected_revision is not None
        and previous.get("context_revision") != expected_revision
    ):
        gaps.append("evidence_expired")
    return gaps


def _prefix_evidence(snapshot, context, comparison):
    """Validate every fixed candidate and independent static-source evidence."""
    candidates = comparison.get("candidates", [])
    history = _historical(snapshot, comparison)
    gaps, refs = _prefix_header_gaps(context, comparison, history), []
    _, static_keys, missing = _static(context)
    if missing:
        gaps.append("missing_static_identity")
    compatible = (
        "source_id",
        "tenant_id",
        "agent_id",
        "provider_id",
        "model_name",
        "assembly_stage",
        "representation_version",
        "estimator_version",
        "key_version",
    )
    lowers, uppers, matches = [], [], []
    for candidate in candidates:
        _check(snapshot)
        context_id = candidate["historical_context_id"]
        previous = history.get(context_id)
        refs.append(_ref("context", context_id, context_id))
        if previous is None:
            gaps.append("missing_context")
            continue
        if previous.get("capture_state") != "complete":
            gaps.append("partial_context")
        if any(
            not context.get(key) or context.get(key) != previous.get(key)
            for key in compatible
        ):
            gaps.append("incompatible_version")
        _, previous_keys, previous_missing = _static(previous)
        if previous_missing:
            gaps.append("missing_static_identity")
        matches.append(
            {
                key: min(amount, previous_keys[key])
                for key, amount in static_keys.items()
                if key in previous_keys
            },
        )
        gaps.extend(_historical_gaps(context, previous, candidate))
        lower, upper = candidate.get("lower_text_bytes"), candidate.get(
            "upper_text_bytes",
        )
        total = context.get("text_bytes")
        if (
            not _number(lower)
            or not _number(upper)
            or not _number(total)
            or not 0 <= lower <= upper <= total
        ):
            gaps.append("partial_context")
        else:
            lowers.append(lower)
            uppers.append(upper)
    shared = _shared_static(matches, snapshot)
    return gaps, refs, sum(shared.values()), sorted(shared), lowers, uppers


def _shared_static(matches, snapshot):
    # Require the same two histories; all candidates still bound the prefix.
    shared = {}
    for first_index, first in enumerate(matches):
        for second in matches[first_index + 1 :]:
            _check(snapshot)
            pair = {
                key: min(amount, second[key])
                for key, amount in first.items()
                if key in second
            }
            if sum(pair.values()) > sum(shared.values()):
                shared = pair
    return shared


def _prefix(snapshot, call, context, common_gaps):
    comparison = next(
        (
            row
            for row in snapshot.get("prefix_comparisons", [])
            if row.get("current_context_id") == context["context_id"]
        ),
        None,
    )
    if comparison is None:
        return _evaluation(
            snapshot,
            "text_prefix_stability",
            object_type="call",
            object_id=call["call_id"],
            gaps=[*common_gaps, "no_history"],
            refs=_round_refs([(call, context)]),
        )
    gaps, refs, static, grouping, lowers, uppers = _prefix_evidence(
        snapshot,
        context,
        comparison,
    )
    gaps = [*common_gaps, *gaps]
    count = len(comparison.get("candidates", []))
    total = context.get("text_bytes")
    lower = max(lowers) / total if lowers and total else None
    upper = max(uppers) / total if uppers and total else None
    state = classify_low_prefix(
        lower,
        upper,
        comparison.get("candidate_limited"),
    )
    if count < 2:
        state = "not_applicable"
    elif static < 2000:
        state = "not_hit"
    if gaps:
        state = "insufficient_evidence"
    elif state == "insufficient_evidence":
        gaps.append("partial_context")
    return _evaluation(
        snapshot,
        "text_prefix_stability",
        object_type="call",
        object_id=call["call_id"],
        state=state,
        gaps=gaps,
        values=[
            _value("history_count", count, "count", 2),
            _value("static_tokens", static, threshold=2000),
            _value("prefix_ratio", lower, "ratio", 0.2, lower, upper),
        ],
        refs=[*_round_refs([(call, context)]), *refs],
        grouping=[_context_signature(context)[:4], grouping],
    )


def _group_scope(snapshot, object_type, identity):
    run = snapshot["run"]
    fields = ("agent_id", "provider_id", "model_name")
    rows = [
        call
        for call in snapshot.get("calls", [])
        if call.get("branch_run_id") == run["branch_run_id"]
        and (object_type != "call" or call["call_id"] == identity)
    ]
    configurations = {tuple(row.get(key) for key in fields) for row in rows}
    config = (
        next(iter(configurations)) if len(configurations) == 1 else (None,) * 3
    )
    return [run.get("source_id"), run.get("tenant_id"), *config]


def _actual_calls(snapshot):
    branch = snapshot["run"]["branch_run_id"]
    contexts = {
        row["context_id"]: row
        for row in snapshot.get("contexts", [])
        if row.get("branch_run_id", branch) == branch
    }
    return [
        (call, contexts.get(call.get("context_id")))
        for call in snapshot.get("calls", [])
        if call.get("branch_run_id", branch) == branch
    ]


def _call_context_gaps(snapshot, call, context):
    if context is None:
        return ["missing_context"]
    run = snapshot["run"]
    keys = ("source_id", "tenant_id", "branch_run_id")
    gaps = []
    if (
        any(
            context.get(key) != run.get(key) or call.get(key) != run.get(key)
            for key in keys
        )
        or context.get("context_role") != "prepared"
        or context.get("call_id") != call["call_id"]
    ):
        gaps.append("incompatible_version")
    return gaps


def evaluate_context_rules(snapshot, *, deadline=None) -> list[Evaluation]:
    """Evaluate B independently; current v1 deliberately cannot evaluate cohorts.

    A logical call's retries count once. Unresolvable or differing retry input
    cannot establish a continuous sequence. No raw content is read or returned.
    """
    snapshot = {**snapshot, "_deadline": deadline}
    _check(snapshot)
    rounds, gaps = _rounds(snapshot)
    if snapshot.get("truncated"):
        gaps.append("capture_limit")
    output = [
        _growth(snapshot, rounds, gaps),
        _carryover(snapshot, rounds, gaps),
        _tool_volume(snapshot, rounds, gaps),
        _safe_repeat(snapshot),
        _loop(snapshot),
    ]
    actual_calls = _actual_calls(snapshot)
    if actual_calls:
        for call, context in actual_calls:
            _check(snapshot)
            local_gaps = _call_context_gaps(snapshot, call, context)
            if context is None:
                output += [
                    _evaluation(
                        snapshot,
                        rule,
                        object_type="call",
                        object_id=call["call_id"],
                        gaps=local_gaps,
                        refs=[_ref("call", call["call_id"])],
                    )
                    for rule in (
                        "text_prefix_stability",
                        "static_context_volume",
                    )
                ]
                continue
            if context.get("capture_state") != "complete":
                local_gaps.append("partial_context")
            if not all(
                context.get(key)
                for key in (
                    "estimator_version",
                    "representation_version",
                    "key_version",
                )
            ):
                local_gaps.append("incompatible_version")
            output += [
                _prefix(snapshot, call, context, local_gaps),
                _static_volume(snapshot, call, context, local_gaps),
            ]
    else:
        output += [
            _evaluation(snapshot, rule, gaps=[*gaps, "missing_context"])
            for rule in ("text_prefix_stability", "static_context_volume")
        ]
    output.append(
        _evaluation(
            snapshot,
            RULE_IDS[-1],
            gaps=["missing_cohort"],
            values=[_value("cohort_count", None, "count", 30)],
        ),
    )
    if snapshot.get("truncated"):
        for evaluation in output:
            _check(snapshot)
            evaluation["state"] = "insufficient_evidence"
            evaluation["findings"] = []
            if "capture_limit" not in evaluation["gaps"]:
                evaluation["gaps"].append("capture_limit")
    _check(snapshot)
    return output
