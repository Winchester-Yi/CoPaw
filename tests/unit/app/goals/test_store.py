# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any
from contextlib import asynccontextmanager

import pytest

from swe.app.goals.models import (
    CompletionCriterion,
    GoalContract,
    GoalControlAction,
    GoalControlCommand,
    GoalScope,
)
from swe.app.goals.service import GoalService
from swe.app.goals.store import MySqlGoalStore


class FakeDb:
    def __init__(self) -> None:
        self.is_connected = True
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rows: list[dict[str, Any]] = []
        self.transactions = 0
        self.in_transaction = False

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        self.in_transaction = True
        try:
            yield self
        finally:
            self.in_transaction = False

    async def execute(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> int:
        if not query.lstrip().startswith("CREATE TABLE"):
            assert self.in_transaction
        self.executed.append((query, params))
        return 1

    async def fetch_one(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ):
        self.executed.append((query, params))
        return self.rows.pop(0) if self.rows else None


def contract() -> GoalContract:
    return GoalContract(
        objective="Build a goal runtime",
        completion_criteria=[
            CompletionCriterion(
                requirement="Runtime exists",
                observable_assertion="module imports",
                verification_method="run import check",
                expected_outcome="import succeeds",
            ),
        ],
        constraints={"must_preserve": [], "must_not_do": []},
        autonomy_boundary="No external writes",
    )


def scope() -> GoalScope:
    return GoalScope(
        tenant_id="tenant",
        source_id="source",
        agent_profile_id="agent",
        chat_id="chat",
        effective_model="model",
    )


@pytest.mark.asyncio
async def test_initialize_creates_snapshot_tables_but_no_goal_audit_table() -> (
    None
):
    db = FakeDb()
    store = MySqlGoalStore(db)

    await store.initialize()

    sql = "\n".join(query for query, _ in db.executed)
    for name in (
        "swe_goals",
        "swe_goal_revisions",
        "swe_goal_criteria",
        "swe_goal_steering",
        "swe_goal_subagent_links",
        "swe_goal_control_commands",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {name}" in sql
    assert "audit" not in sql.lower()


@pytest.mark.asyncio
async def test_save_writes_goal_revision_criteria_and_control_snapshot() -> (
    None
):
    db = FakeDb()
    store = MySqlGoalStore(db)
    service = GoalService(store)

    created = await service.create_goal(scope=scope(), contract=contract())
    created.control_commands.append(
        GoalControlCommand(action=GoalControlAction.PAUSE),
    )
    await store.save(created)

    sql = "\n".join(query for query, _ in db.executed)
    assert "INSERT INTO swe_goals" in sql
    assert "INSERT INTO swe_goal_revisions" in sql
    assert "INSERT INTO swe_goal_criteria" in sql
    assert "INSERT INTO swe_goal_control_commands" in sql
    assert db.transactions == 2
    assert not db.in_transaction


@pytest.mark.asyncio
async def test_save_projects_goal_owned_subagent_links() -> None:
    db = FakeDb()
    store = MySqlGoalStore(db)
    service = GoalService(store)
    created = await service.create_goal(scope=scope(), contract=contract())
    created.subagent_run_ids.append("subagent-1")

    await store.save(created)

    assert any(
        "INSERT INTO swe_goal_subagent_links" in query
        for query, _ in db.executed
    )
