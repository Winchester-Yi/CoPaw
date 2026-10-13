# -*- coding: utf-8 -*-
"""Cooperative B analysis on one saved relational snapshot."""

from uuid import uuid4

from .diagnostic_rules import evaluate_context_rules
from .diagnostic_storage import read_context, read_operation
from .prefix_analysis import build_comparison, check_deadline, select_history
from .rules import analyze_branch


async def attach_snapshot(cursor, snapshot, deadline):
    """Read B facts and historical candidates inside the existing transaction."""
    run = snapshot["run"]
    scope = (run["source_id"], run["tenant_id"])
    snapshot["snapshot_id"] = str(uuid4())
    snapshot["input_revision"] = run["data_revision"]
    snapshot.update(
        contexts=[],
        operations=[],
        prefix_comparisons=[],
        history_contexts=[],
        steps=[],
    )
    await cursor.execute(
        "SELECT context_id FROM swe_token_contexts WHERE branch_run_id=%s AND source_id=%s AND tenant_id=%s ORDER BY context_id LIMIT 257",
        (run["branch_run_id"], *scope),
    )
    identities = await cursor.fetchall()
    snapshot["truncated"] = snapshot["truncated"] or len(identities) > 256
    histories = {}
    for identity in identities[:256]:
        check_deadline(deadline)
        context = await read_context(cursor, identity["context_id"], scope)
        if context is None:
            raise ValueError("evidence_expired")
        snapshot["contexts"].append(context)
        if context["context_role"] != "prepared":
            continue
        if (
            context["capture_state"] != "complete"
            or not context.get("request_ended_at")
            or context.get("execution_state") == "running"
        ):
            history = {"contexts": [], "candidate_limited": False}
        else:
            history = await select_history(cursor, context, deadline=deadline)
        comparison = build_comparison(
            context,
            history["contexts"],
            snapshot_id=snapshot["snapshot_id"],
            input_revision=run["data_revision"],
            candidate_limited=history["candidate_limited"],
            deadline=deadline,
        )
        if history.get("reason_code") == "missing_identity":
            comparison.update(
                reason_code="missing_identity",
                analysis_state="insufficient_evidence",
                eligible=False,
            )
        snapshot["prefix_comparisons"].append(comparison)
        histories.update(
            {
                (row["context_id"], row["context_revision"]): row
                for row in history["contexts"]
            },
        )
    snapshot["history_contexts"] = list(histories.values())
    check_deadline(deadline)
    await cursor.execute(
        "SELECT operation_id FROM swe_token_context_operations WHERE branch_run_id=%s AND source_id=%s AND tenant_id=%s ORDER BY operation_id LIMIT 257",
        (run["branch_run_id"], *scope),
    )
    operations = await cursor.fetchall()
    snapshot["truncated"] = snapshot["truncated"] or len(operations) > 256
    for identity in operations[:256]:
        check_deadline(deadline)
        snapshot["operations"].append(
            await read_operation(cursor, identity["operation_id"], scope),
        )
    check_deadline(deadline)
    return snapshot


async def analyze_snapshot(snapshot, *, deadline):
    """Compute bounded rules cooperatively, without a detached timed-out thread."""
    check_deadline(deadline)
    result = analyze_branch(
        snapshot["calls"],
        snapshot["items"],
        snapshot["events"],
    )
    check_deadline(deadline)
    evaluations = evaluate_context_rules(snapshot, deadline=deadline)
    check_deadline(deadline)
    if snapshot["truncated"]:
        for rule in result["rules"]:
            rule["state"] = "insufficient_evidence"
            rule["missing_fields"].append("analysis_window_limited")
    return {
        **result,
        "diagnostic_evaluations": evaluations,
        "prefix_comparisons": snapshot.get("prefix_comparisons", []),
    }
