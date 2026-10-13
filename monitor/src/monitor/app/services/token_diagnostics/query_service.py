# -*- coding: utf-8 -*-
"""Scoped Token-only read projections, including explicit coverage gaps."""

import base64
import json

from .store import select_columns


def summarize_records(runs, calls):
    counts = {"reported": 0, "partial": 0, "unavailable": 0, "pending": 0}
    for call in calls:
        counts[call.get("usage_status", "unavailable")] += 1
    known_input = sum(c.get("input_tokens") or 0 for c in calls)
    known_output = sum(c.get("output_tokens") or 0 for c in calls)
    ids = {r["branch_run_id"] for r in runs}
    expected_children = {
        child for r in runs for child in r.get("expected_child_branches", [])
    }
    expected = [r.get("expected_attempt_count") for r in runs]
    state = "complete"
    if (
        not runs
        or any(value is None for value in expected)
        or any(r.get("gap_tracking_limited") for r in runs)
    ):
        state = "unverified"
    elif (
        expected_children - ids
        or sum(expected) != len(calls)
        or any(r.get("gap_count", 0) for r in runs)
        or any(
            c.get("execution_state") in {"running", "prepared", "abandoned"}
            for c in calls
        )
    ):
        state = "incomplete"
    return {
        "known_input_tokens": known_input,
        "known_output_tokens": known_output,
        "known_total_tokens": known_input + known_output,
        "observed_call_count": len(calls),
        "reported_call_count": counts["reported"],
        "partial_call_count": counts["partial"],
        "unknown_call_count": counts["unavailable"] + counts["pending"],
        "capture_state": state,
        "usage_complete": state == "complete"
        and counts["reported"] == len(calls),
        "data_revision": sum(r.get("data_revision", 0) for r in runs),
        "analyzed_revision": sum(r.get("analyzed_revision", 0) for r in runs),
    }


class TokenQueryService:
    def __init__(self, store):
        self.store = store

    async def list_runs(self, scope, limit=50, cursor=None, model=None):
        where, params = (
            "source_id=%s AND tenant_id=%s AND parent_branch_run_id IS NULL",
            list(scope),
        )
        if cursor:
            decoded = json.loads(
                base64.urlsafe_b64decode(cursor.encode()).decode(),
            )
            where += " AND (started_at,branch_run_id)<(%s,%s)"
            params.extend(decoded)
        if model:
            where += " AND EXISTS (SELECT 1 FROM swe_token_model_calls c WHERE c.task_run_id=swe_token_runs.task_run_id AND c.source_id=%s AND c.tenant_id=%s AND c.model_name=%s)"
            params.extend((*scope, model))
        rows = await self.store.rows(
            f"SELECT {select_columns('swe_token_runs')} FROM swe_token_runs WHERE {where} ORDER BY started_at DESC,branch_run_id DESC LIMIT %s",
            tuple(params) + (limit + 1,),
        )
        has_more, rows = len(rows) > limit, rows[:limit]
        summaries = await self._list_totals(rows, scope)
        for row in rows:
            row.update(
                summaries.get(
                    row["task_run_id"],
                    summarize_records([row], []),
                ),
            )
        next_cursor = None
        if has_more and rows:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    [
                        rows[-1]["started_at"].replace("T", " ").rstrip("Z"),
                        rows[-1]["branch_run_id"],
                    ],
                ).encode(),
            ).decode()
        return {"items": rows, "next_cursor": next_cursor}

    async def _list_totals(self, runs, scope):
        if not runs:
            return {}
        marks = ",".join(["%s"] * len(runs))
        rows = await self.store.rows(
            f"SELECT task_run_id,COALESCE(SUM(input_tokens),0) AS known_input_tokens,COALESCE(SUM(output_tokens),0) AS known_output_tokens,COUNT(*) AS observed_call_count,SUM(usage_status='reported') AS reported_call_count,SUM(usage_status='partial') AS partial_call_count,SUM(usage_status IN ('pending','unavailable')) AS unknown_call_count FROM swe_token_model_calls WHERE source_id=%s AND tenant_id=%s AND task_run_id IN ({marks}) GROUP BY task_run_id",
            (*scope, *(r["task_run_id"] for r in runs)),
        )
        output = {}
        for row in rows:
            row["known_total_tokens"] = (
                row["known_input_tokens"] + row["known_output_tokens"]
            )
            # List totals alone cannot prove child lifecycle completeness.
            row["usage_complete"] = False
            row["capture_state"] = "unverified"
            output[row["task_run_id"]] = row
        return output

    async def get_run(self, identity, scope):
        runs = await self.store.rows(
            f"SELECT {select_columns('swe_token_runs')} FROM swe_token_runs WHERE source_id=%s AND tenant_id=%s AND task_run_id=%s ORDER BY parent_branch_run_id IS NOT NULL,started_at LIMIT 1001",
            (*scope, identity),
        )
        if not runs:
            return None
        calls = await self.store.rows(
            f"SELECT {select_columns('swe_token_model_calls')} FROM swe_token_model_calls WHERE source_id=%s AND tenant_id=%s AND task_run_id=%s ORDER BY branch_run_id,logical_seq,attempt_no LIMIT 1001",
            (*scope, identity),
        )
        items = await self.store.rows(
            "SELECT category,SUM(estimated_tokens) AS estimated_tokens,COUNT(*) AS item_count,SUM(estimated_tokens IS NULL) AS unknown_item_count FROM swe_token_input_items WHERE source_id=%s AND tenant_id=%s AND task_run_id=%s GROUP BY category",
            (*scope, identity),
        )
        findings = await self.findings(identity, scope)
        summary = summarize_records(runs, calls)
        limited = len(runs) > 1000 or len(calls) > 1000
        if limited:
            summary.update(capture_state="unverified", usage_complete=False)
            totals = await self._list_totals(
                [{"task_run_id": identity}],
                scope,
            )
            summary.update(totals.get(identity, {}))
        summary["composition_complete"] = (
            not limited
            and all(c.get("capture_state") == "complete" for c in calls)
            and bool(calls)
            and all(not row.get("unknown_item_count") for row in items)
        )
        return {
            "available": True,
            "task_run_id": identity,
            "run": runs[0],
            "branches": runs[:1000],
            "calls": calls[:1000],
            "categories": items,
            "findings": findings,
            "summary": summary,
            "data_limited": limited,
            "analysis_state": self._analysis_state(runs),
        }

    @staticmethod
    def _analysis_state(runs):
        states = {r.get("analysis_state", "pending") for r in runs}
        for state in ("failed", "running", "pending", "insufficient_evidence"):
            if state in states:
                return state
        return "ready"

    async def calls(self, identity, scope, branch=None, limit=100, offset=0):
        where, params = "source_id=%s AND tenant_id=%s AND task_run_id=%s", [
            *scope,
            identity,
        ]
        if branch:
            where += " AND branch_run_id=%s"
            params.append(branch)
        rows = await self.store.rows(
            f"SELECT {select_columns('swe_token_model_calls')} FROM swe_token_model_calls WHERE {where} ORDER BY branch_run_id,logical_seq,attempt_no LIMIT %s OFFSET %s",
            tuple(params) + (limit + 1, offset),
        )
        return {"items": rows[:limit], "has_more": len(rows) > limit}

    async def inputs(
        self,
        identity,
        scope,
        category=None,
        limit=100,
        offset=0,
    ):
        where, params = "source_id=%s AND tenant_id=%s AND call_id=%s", [
            *scope,
            identity,
        ]
        if category:
            where += " AND category=%s"
            params.append(category)
        rows = await self.store.rows(
            f"SELECT {select_columns('swe_token_input_items')} FROM swe_token_input_items WHERE {where} ORDER BY occurrence_index LIMIT %s OFFSET %s",
            tuple(params) + (limit + 1, offset),
        )
        return {"items": rows[:limit], "has_more": len(rows) > limit}

    async def findings(self, identity, scope):
        return await self.store.rows(
            f"SELECT {select_columns('swe_token_findings')} FROM swe_token_findings WHERE source_id=%s AND tenant_id=%s AND task_run_id=%s AND state='active' ORDER BY updated_at DESC LIMIT 500",
            (*scope, identity),
        )

    async def by_trace(self, identity, scope):
        return await self.store.rows(
            "SELECT DISTINCT task_run_id FROM swe_token_runs WHERE source_id=%s AND tenant_id=%s AND trace_id=%s LIMIT 100",
            (*scope, identity),
        )
