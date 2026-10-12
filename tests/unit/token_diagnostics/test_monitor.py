# -*- coding: utf-8 -*-
"""Queries and analysis must remain scoped, complete and revision-aware."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "monitor" / "src")
)


def implementation():
    try:
        from monitor.app.services.token_diagnostics.query_service import (
            summarize_records,
            TokenQueryService,
        )
        from monitor.app.services.token_diagnostics.analysis import Analyzer
    except ModuleNotFoundError:
        pytest.fail("Independent Token query and analyzer are missing")
    return summarize_records, TokenQueryService, Analyzer


def run(**updates):
    return {
        "branch_run_id": "b",
        "task_run_id": "b",
        "parent_branch_run_id": None,
        "execution_state": "completed",
        "expected_attempt_count": 1,
        "expected_child_branches": [],
        "gap_count": 0,
        "data_revision": 2,
        "analyzed_revision": 2,
        **updates,
    }


def test_unknown_usage_does_not_become_zero_complete_total():
    summarize, _, _ = implementation()
    result = summarize(
        [run()],
        [
            {
                "input_tokens": 100,
                "output_tokens": None,
                "usage_status": "partial",
                "execution_state": "completed",
            }
        ],
    )
    assert result["known_input_tokens"] == 100
    assert result["known_total_tokens"] == 100
    assert result["usage_complete"] is False
    assert result["partial_call_count"] == 1


def test_missing_child_and_missing_terminal_count_are_not_complete():
    summarize, _, _ = implementation()
    data = [
        {
            "input_tokens": 10,
            "output_tokens": 2,
            "usage_status": "reported",
            "execution_state": "completed",
        }
    ]
    assert (
        summarize([run(expected_child_branches=["missing"])], data)[
            "capture_state"
        ]
        == "incomplete"
    )
    assert (
        summarize([run(expected_attempt_count=None)], data)["capture_state"]
        == "unverified"
    )
    assert summarize([run()], data)["capture_state"] == "complete"


def test_explicit_no_calls_is_zero_only_when_lifecycle_is_complete():
    summarize, _, _ = implementation()
    assert (
        summarize([run(expected_attempt_count=0)], [])["usage_complete"]
        is True
    )
    assert summarize([], [])["usage_complete"] is False


@pytest.mark.asyncio
async def test_missing_task_does_not_query_another_tenants_calls():
    _, service_cls, _ = implementation()

    class Store:
        queries = []

        async def rows(self, sql, params=()):
            self.queries.append((sql, params))
            return []

    store = Store()
    assert (
        await service_cls(store).get_run("unknown", ("s", "tenant-a")) is None
    )
    assert len(store.queries) == 1
    assert store.queries[0][1][:2] == ("s", "tenant-a")


@pytest.mark.asyncio
async def test_analyzer_commits_claim_identity_and_snapshot_revision():
    _, _, analyzer_cls = implementation()

    class Store:
        committed = None

        async def claim_next(self):
            return {
                "branch_run_id": "b",
                "source_id": "s",
                "tenant_id": "t",
                "claim_token": "lease1",
            }

        async def analysis_data(self, claim):
            return {
                "run": {**run(data_revision=7), **claim},
                "calls": [],
                "items": [],
                "events": [],
                "truncated": False,
            }

        async def renew(self, claim):
            return True

        async def commit_analysis(self, claim, revision, result):
            self.committed = (claim, revision, result)
            return True

    store = Store()
    assert await analyzer_cls(store).analyze_once()
    assert store.committed[0]["claim_token"] == "lease1"
    assert store.committed[1] == 7


@pytest.mark.asyncio
async def test_expired_or_replaced_lease_cannot_commit_findings():
    implementation()
    from monitor.app.services.token_diagnostics.store import valid_claim

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert (
        valid_claim(
            {"claim_token": "old", "lease_until": now + timedelta(seconds=5)},
            "new",
        )
        is False
    )
    assert (
        valid_claim(
            {"claim_token": "same", "lease_until": now - timedelta(seconds=1)},
            "same",
        )
        is False
    )
