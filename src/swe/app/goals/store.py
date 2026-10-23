# -*- coding: utf-8 -*-
"""MySQL authority store for durable Goal Runtime snapshots."""

from __future__ import annotations

import json
from typing import Any

from .models import GoalSnapshot, TERMINAL_GOAL_STATES

_GOALS = "swe_goals"
_REVISIONS = "swe_goal_revisions"
_CRITERIA = "swe_goal_criteria"
_STEERING = "swe_goal_steering"
_SUBAGENT_LINKS = "swe_goal_subagent_links"
_CONTROLS = "swe_goal_control_commands"


class MySqlGoalStore:
    """Store each snapshot projection in the phase-one MySQL tables."""

    def __init__(self, db: Any) -> None:
        self._db = db

    @property
    def is_available(self) -> bool:
        return bool(getattr(self._db, "is_connected", False))

    async def initialize(self) -> None:
        if not self.is_available:
            return
        for statement in _SCHEMA:
            await self._db.execute(statement)

    async def create(self, snapshot: GoalSnapshot) -> GoalSnapshot:
        await self._write(snapshot)
        return snapshot.model_copy(deep=True)

    async def get(self, goal_id: str) -> GoalSnapshot | None:
        row = await self._db.fetch_one(
            f"SELECT snapshot_json FROM {_GOALS} WHERE goal_id = %s",
            (goal_id,),
        )
        return _snapshot_from_row(row)

    async def save(self, snapshot: GoalSnapshot) -> GoalSnapshot:
        await self._write(snapshot)
        return snapshot.model_copy(deep=True)

    async def latest_for_chat(self, chat_id: str) -> GoalSnapshot | None:
        terminal = tuple(state.value for state in TERMINAL_GOAL_STATES)
        row = await self._db.fetch_one(
            f"""
            SELECT snapshot_json FROM {_GOALS}
            WHERE chat_id = %s
            ORDER BY CASE WHEN state NOT IN (%s, %s) THEN 0 ELSE 1 END,
                created_at DESC
            LIMIT 1
            """,
            (chat_id, *terminal),
        )
        return _snapshot_from_row(row)

    async def _write(self, snapshot: GoalSnapshot) -> None:
        """Write current snapshot plus denormalized Revision/criterion/control views."""
        payload = _dump(snapshot)
        async with self._db.transaction() as db:
            await self._write_with(db, snapshot, payload)

    async def _write_with(
        self,
        db: Any,
        snapshot: GoalSnapshot,
        payload: str,
    ) -> None:
        await db.execute(
            _UPSERT_GOAL,
            (
                snapshot.goal_id,
                snapshot.scope.tenant_id,
                snapshot.scope.source_id,
                snapshot.scope.agent_profile_id,
                snapshot.scope.chat_id,
                snapshot.scope.effective_model,
                snapshot.state.value,
                snapshot.revision,
                snapshot.budget_cycle,
                snapshot.turn_budget,
                snapshot.turns_used,
                snapshot.next_focus,
                snapshot.state_reason,
                payload,
            ),
        )
        await db.execute(
            _UPSERT_REVISION,
            (
                snapshot.goal_id,
                snapshot.revision,
                _dump(snapshot.contract),
            ),
        )
        for criterion in snapshot.criteria:
            await db.execute(
                _UPSERT_CRITERION,
                (
                    snapshot.goal_id,
                    snapshot.revision,
                    criterion.criterion_id,
                    1 if criterion.verified else 0,
                    criterion.consecutive_failures,
                    _dump(criterion.criterion),
                    _dump(criterion.evidence_refs),
                ),
            )
        for command in snapshot.control_commands:
            await db.execute(
                _UPSERT_CONTROL,
                (
                    command.command_id,
                    snapshot.goal_id,
                    command.action.value,
                    command.status,
                    _dump(command.contract) if command.contract else None,
                ),
            )
        for steering in snapshot.steering:
            await db.execute(
                _UPSERT_STEERING,
                (
                    snapshot.goal_id,
                    snapshot.revision,
                    steering.sequence_no,
                    steering.content,
                    1 if steering.consumed else 0,
                ),
            )
        for run_id in snapshot.subagent_run_ids:
            await db.execute(
                _UPSERT_SUBAGENT_LINK,
                (snapshot.goal_id, snapshot.revision, run_id),
            )


def _dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _snapshot_from_row(row: dict[str, Any] | None) -> GoalSnapshot | None:
    if row is None:
        return None
    payload = row.get("snapshot_json")
    if not isinstance(payload, str):
        return None
    return GoalSnapshot.model_validate(json.loads(payload))


_SCHEMA = (
    f"""CREATE TABLE IF NOT EXISTS {_GOALS} (
        goal_id VARCHAR(64) NOT NULL PRIMARY KEY,
        tenant_id VARCHAR(128) NOT NULL, source_id VARCHAR(128) NOT NULL,
        agent_profile_id VARCHAR(128) NOT NULL, chat_id VARCHAR(255) NOT NULL,
        effective_model VARCHAR(255) NOT NULL, state VARCHAR(32) NOT NULL,
        current_revision INT NOT NULL, budget_cycle INT NOT NULL,
        turn_budget INT NOT NULL, turns_used INT NOT NULL,
        next_focus MEDIUMTEXT NULL, state_reason MEDIUMTEXT NULL,
        snapshot_json MEDIUMTEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_goals_chat_state (chat_id, state, created_at)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_REVISIONS} (
        goal_id VARCHAR(64) NOT NULL, revision INT NOT NULL,
        contract_json MEDIUMTEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (goal_id, revision)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_CRITERIA} (
        goal_id VARCHAR(64) NOT NULL, revision INT NOT NULL,
        criterion_id VARCHAR(64) NOT NULL, verified TINYINT(1) NOT NULL,
        consecutive_failures INT NOT NULL, criterion_json MEDIUMTEXT NOT NULL,
        evidence_refs_json MEDIUMTEXT NOT NULL,
        PRIMARY KEY (goal_id, revision, criterion_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_STEERING} (
        steering_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        goal_id VARCHAR(64) NOT NULL, revision INT NOT NULL,
        sequence_no INT NOT NULL, content MEDIUMTEXT NOT NULL,
        consumed TINYINT(1) NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_goal_steering_sequence (goal_id, sequence_no)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_SUBAGENT_LINKS} (
        goal_id VARCHAR(64) NOT NULL, revision INT NOT NULL,
        subagent_run_id VARCHAR(128) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (goal_id, revision, subagent_run_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {_CONTROLS} (
        command_id VARCHAR(64) NOT NULL PRIMARY KEY, goal_id VARCHAR(64) NOT NULL,
        action VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL,
        contract_json MEDIUMTEXT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_goal_controls_pending (goal_id, status, created_at)
    )""",
)

_UPSERT_GOAL = f"""INSERT INTO {_GOALS} (
    goal_id, tenant_id, source_id, agent_profile_id, chat_id, effective_model,
    state, current_revision, budget_cycle, turn_budget, turns_used, next_focus,
    state_reason, snapshot_json
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE state=VALUES(state), current_revision=VALUES(current_revision),
budget_cycle=VALUES(budget_cycle), turns_used=VALUES(turns_used),
next_focus=VALUES(next_focus), state_reason=VALUES(state_reason),
snapshot_json=VALUES(snapshot_json)"""
_UPSERT_REVISION = f"""INSERT INTO {_REVISIONS} (goal_id, revision, contract_json)
VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE contract_json=VALUES(contract_json)"""
_UPSERT_CRITERION = f"""INSERT INTO {_CRITERIA} (
goal_id, revision, criterion_id, verified, consecutive_failures, criterion_json, evidence_refs_json
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE verified=VALUES(verified), consecutive_failures=VALUES(consecutive_failures),
criterion_json=VALUES(criterion_json), evidence_refs_json=VALUES(evidence_refs_json)"""
_UPSERT_CONTROL = f"""INSERT INTO {_CONTROLS} (command_id, goal_id, action, status, contract_json)
VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE status=VALUES(status), contract_json=VALUES(contract_json)"""
_UPSERT_STEERING = f"""INSERT INTO {_STEERING} (goal_id, revision, sequence_no, content, consumed)
VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE consumed=VALUES(consumed)"""
_UPSERT_SUBAGENT_LINK = f"""INSERT INTO {_SUBAGENT_LINKS} (
goal_id, revision, subagent_run_id
) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE subagent_run_id=VALUES(subagent_run_id)"""
