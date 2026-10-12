# -*- coding: utf-8 -*-
"""Versioned pure rules over saved numeric facts; no SWE/model imports."""

import hashlib
import json
from collections import defaultdict

RULE_VERSION = 1
RULE_IDS = (
    "repeated_tool_result",
    "duplicate_injection",
    "repeated_tool_call",
    "history_growth",
    "output_budget",
)


def key_of(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def finding(
    rule, key, title, action, evidence, affected=None, quality="needs_review"
):
    return {
        "rule_id": rule,
        "rule_version": RULE_VERSION,
        "finding_key": key_of(key),
        "title": title,
        "action": action,
        "evidence": evidence,
        "affected_tokens": affected,
        "evidence_quality": quality,
        "state": "active",
    }


def repeat_results(calls, items):
    calls_by_id = {c["call_id"]: c for c in calls}
    groups = defaultdict(dict)
    for item in sorted(
        items,
        key=lambda item: (
            calls_by_id.get(item["call_id"], {}).get("logical_seq", 0),
            calls_by_id.get(item["call_id"], {}).get("attempt_no", 0),
            item.get("occurrence_index", 0),
        ),
    ):
        call = calls_by_id.get(item["call_id"])
        if item.get("category") != "tool_result" or not call:
            continue
        if not item.get("fingerprint") or not item.get(
            "producer_operation_id"
        ):
            continue
        key = (
            item["producer_operation_id"],
            item["fingerprint"],
            item.get("key_version"),
        )
        logical = call.get("logical_call_id", call["call_id"])
        groups[key].setdefault(
            logical, (call["call_id"], item.get("estimated_tokens") or 0)
        )
    output = []
    for key, appearances in groups.items():
        values = list(appearances.values())
        repeated = sum(x[1] for x in values[1:])
        if len(values) >= 3 and repeated >= 8000:
            output.append(
                finding(
                    RULE_IDS[0],
                    key,
                    "同一工具结果重复进入模型输入",
                    "检查在线结果保留长度；保留完整工件供按需读回。",
                    {
                        "call_ids": [x[0] for x in values],
                        "producer_operation_id": key[0],
                        "occurrences": len(values),
                        "minimum_occurrences": 3,
                        "threshold_tokens": 8000,
                    },
                    repeated,
                )
            )
    return output


def duplicate_injections(items):
    groups = defaultdict(list)
    for item in items:
        if item.get("category") in {"system", "inject", "skill"} and item.get(
            "fingerprint"
        ):
            groups[
                (item["call_id"], item["fingerprint"], item.get("key_version"))
            ].append(item)
    output = []
    for key, copies in groups.items():
        sizes = [x.get("estimated_tokens") or 0 for x in copies]
        duplicate = sum(sizes) - max(sizes)
        if len(copies) >= 2 and duplicate >= 1000:
            output.append(
                finding(
                    RULE_IDS[1],
                    key,
                    "同次输入中内容重复注入",
                    "检查来源是否重复装配，并核实各副本是否具有独立用途。",
                    {
                        "call_ids": [key[0]],
                        "source_refs": [x.get("source_ref") for x in copies],
                        "occurrences": len(copies),
                        "threshold_tokens": 1000,
                    },
                    duplicate,
                )
            )
    return output


def repeat_tools(events):
    output, seen, sequence = [], set(), []
    last_key = None
    for event in sorted(events, key=lambda x: x.get("event_seq", 0)):
        if event.get("event_type") != "tool_completed":
            continue
        identity = event.get("producer_operation_id")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        key = (
            event.get("tool_name"),
            event.get("arguments_fingerprint"),
            event.get("result_fingerprint"),
            event.get("error_code"),
            event.get("progress_version"),
            event.get("compaction_epoch", 0),
            event.get("key_version"),
        )
        if event.get("polling") or not key[1] or (not key[2] and not key[3]):
            sequence, last_key = [], None
            continue
        if key != last_key:
            sequence = []
        sequence.append(identity)
        last_key = key
        if len(sequence) >= 3:
            output.append(
                finding(
                    RULE_IDS[2],
                    (key, sequence[0]),
                    "相同工具操作反复执行，需复核进展",
                    "检查错误反馈、重试策略与轮询目的；次数本身不证明死循环。",
                    {
                        "operation_ids": list(sequence),
                        "tool_name": key[0],
                        "occurrences": len(sequence),
                        "minimum_occurrences": 3,
                    },
                )
            )
    # One stable finding per continuing sequence, with its latest evidence.
    return list({x["finding_key"]: x for x in output}.values())


def history_growth(calls, items):
    amounts = defaultdict(int)
    for item in items:
        if item.get("category") in {"user", "assistant", "tool_result"}:
            amounts[item["call_id"]] += item.get("estimated_tokens") or 0
    groups = defaultdict(dict)
    for call in sorted(
        calls, key=lambda c: (c.get("logical_seq", 0), c.get("attempt_no", 0))
    ):
        if call.get("capture_state") != "complete":
            continue
        key = tuple(
            call.get(k)
            for k in (
                "purpose",
                "phase_id",
                "model_name",
                "estimator_version",
                "compaction_epoch",
            )
        )
        groups[key][call.get("logical_call_id", call["call_id"])] = call
    output = []
    for key, logical in groups.items():
        values = list(logical.values())
        if len(values) < 4:
            continue
        first, last = values[0], values[-1]
        baseline, current = amounts[first["call_id"]], amounts[last["call_id"]]
        if (
            baseline > 0
            and current >= baseline * 2
            and current - baseline >= 8000
        ):
            output.append(
                finding(
                    RULE_IDS[3],
                    key,
                    "在线历史持续增长",
                    "检查新增证据与旧结果的占比，核对保留范围及压缩时机。",
                    {
                        "call_ids": [first["call_id"], last["call_id"]],
                        "initial_tokens": baseline,
                        "current_tokens": current,
                        "growth_ratio": current / baseline,
                        "phase_known": key[1] is not None,
                        "threshold_tokens": 8000,
                    },
                    current - baseline,
                )
            )
    return output


def output_budget(calls):
    output = []
    for call in calls:
        count, limit = call.get("output_tokens"), call.get("max_output_tokens")
        cut = call.get("finish_reason") == "length"
        near_cap = (
            type(count) is int
            and type(limit) is int
            and limit > 0
            and count >= limit * 0.9
        )
        if not cut and not near_cap:
            continue
        output.append(
            finding(
                RULE_IDS[4],
                call["call_id"],
                "模型输出被长度上限截断" if cut else "模型输出接近预算上限",
                "核对输出目标、有效上限与补写开销；完整报告可能需要提高上限。",
                {
                    "call_ids": [call["call_id"]],
                    "output_tokens": count,
                    "output_limit": limit,
                    "finish_reason": call.get("finish_reason"),
                    "threshold_ratio": 0.9,
                },
                quality="observed",
            )
        )
    return output


def analyze_branch(calls, items, events):
    findings = (
        repeat_results(calls, items)
        + duplicate_injections(items)
        + repeat_tools(events)
        + history_growth(calls, items)
        + output_budget(calls)
    )
    rules = []
    for rule in RULE_IDS:
        missing = []
        if rule in RULE_IDS[:2] and any(
            not x.get("fingerprint") for x in items
        ):
            missing.append("fingerprint")
        if rule == RULE_IDS[0] and any(
            x.get("category") == "tool_result"
            and not x.get("producer_operation_id")
            for x in items
        ):
            missing.append("producer_operation_identity")
        if rule in {RULE_IDS[0], RULE_IDS[1], RULE_IDS[3]} and any(
            c.get("capture_state") != "complete" for c in calls
        ):
            missing.append("complete_input_capture")
        if rule == RULE_IDS[2] and any(
            x.get("event_type") == "tool_completed"
            and (
                not x.get("arguments_fingerprint")
                or (
                    not x.get("result_fingerprint") and not x.get("error_code")
                )
            )
            for x in events
        ):
            missing.append("tool_arguments_or_result_fingerprint")
        if (
            rule == RULE_IDS[3]
            and calls
            and any(not c.get("phase_id") for c in calls)
        ):
            missing.append("phase_identity")
        if rule == RULE_IDS[4] and any(
            c.get("output_tokens") is None
            or c.get("max_output_tokens") is None
            for c in calls
        ):
            missing.append("output_usage_or_limit")
        rules.append(
            {
                "rule_id": rule,
                "rule_version": RULE_VERSION,
                "state": "insufficient_evidence" if missing else "evaluated",
                "missing_fields": missing,
            }
        )
    return {"findings": findings, "rules": rules}
