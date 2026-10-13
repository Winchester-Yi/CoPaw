# -*- coding: utf-8 -*-
"""Exercise saved facts through independent SWE and Monitor SQL adapters."""

import re
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "monitor" / "src"))
DDL = ROOT / "scripts/sql/token_diagnostics_tables.sql"


class RelationalDatabase:
    def __init__(self, connection, dialect):
        self.connection, self.dialect = connection, dialect

    def execute(self, sql, params=()):
        if self.dialect == "mysql":
            cursor = self.connection.cursor()
            cursor.execute(sql.replace("?", "%s"), params)
            return cursor
        values = tuple(
            value.isoformat() if isinstance(value, datetime) else value
            for value in params
        )
        return self.connection.execute(sql, values)


class RelationalCursor:
    """SQLite executes relational statements; only MySQL syntax is translated."""

    def __init__(self, db):
        self.db = db
        self.queries = []
        self.result = None

    async def execute(self, sql, params=()):
        self.queries.append((sql, params))
        assert not re.search(r"SELECT\s+\*", sql, re.I)
        assert "payload" not in sql and "analysis_summary=" not in sql
        if self.db.dialect == "mysql":
            self.result = self.db.execute(sql, params)
            self.rowcount = self.result.rowcount
            return
        sql = sql.replace(" FOR UPDATE", "").replace("%s", "?")
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE")
        sql = sql.replace("UTC_TIMESTAMP(6)", "CURRENT_TIMESTAMP")
        sql = re.sub(
            r"ON DUPLICATE KEY UPDATE",
            "ON CONFLICT DO UPDATE SET",
            sql,
        )
        sql = re.sub(r"VALUES\((\w+)\)", r"excluded.\1", sql)
        sql = sql.replace("GREATEST(", "MAX(")
        self.result = self.db.execute(sql, params)
        self.rowcount = self.result.rowcount

    async def fetchone(self):
        row = self.result.fetchone()
        return dict(row) if row else None

    async def fetchall(self):
        return [dict(row) for row in self.result.fetchall()]


@pytest.fixture(
    params=(
        ["sqlite", "mysql"]
        if os.environ.get("TEST_TOKEN_MYSQL_DSN")
        else ["sqlite"]
    ),
)
def relational_db(request):
    """Opt-in MySQL always creates/drops a fresh disposable test schema."""
    if request.param == "mysql":
        import pymysql

        dsn = urlparse(os.environ["TEST_TOKEN_MYSQL_DSN"])
        assert dsn.scheme == "mysql"
        assert dsn.path.strip("/").startswith("test_token_diagnostics")
        connection = pymysql.connect(
            host=dsn.hostname,
            port=dsn.port or 3306,
            user=unquote(dsn.username or "root"),
            password=unquote(dsn.password or ""),
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
        schema = "test_token_diagnostics_" + uuid4().hex
        db = RelationalDatabase(connection, "mysql")
        created = False
        try:
            db.execute(f"CREATE DATABASE {schema} CHARACTER SET utf8mb4")
            created = True
            connection.select_db(schema)
            for statement in DDL.read_text().split(";"):
                if statement.strip():
                    db.execute(statement)
            yield db
        finally:
            try:
                if created:
                    db.execute(f"DROP DATABASE {schema}")
            finally:
                connection.close()
        return
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db = RelationalDatabase(connection, "sqlite")
    for name, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\) ENGINE",
        DDL.read_text(),
        re.S,
    ):
        columns = []
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith(("INDEX", "UNIQUE KEY")):
                continue
            columns.append(re.sub(r"\s+COMMENT\s+'[^']*'", "", line))
        db.execute(f"CREATE TABLE {name} ({','.join(columns)})")
    yield db
    connection.close()


def test_schema_has_only_typed_commented_relations():
    text = DDL.read_text()
    assert not re.search(r"\b(JSON|ENUM|SET|LOB|BLOB|CLOB|TEXT)\b", text, re.I)
    for name, body, suffix in re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\) (ENGINE[^;]+);",
        text,
        re.S,
    ):
        assert "PRIMARY KEY" in body, name
        assert "COMMENT=" in suffix, name
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith(
                ("PRIMARY KEY", "INDEX", "UNIQUE KEY"),
            ):
                assert " COMMENT " in line, (name, line)


def test_independent_storage_contracts_match_schema():
    from swe.token_diagnostics import storage_schema as swe_schema
    from monitor.app.services.token_diagnostics import (
        storage_schema as monitor_schema,
    )

    assert swe_schema.COLUMNS == monitor_schema.COLUMNS
    assert swe_schema.DEFAULTS == monitor_schema.DEFAULTS
    assert swe_schema.EVIDENCE_FIELDS == monitor_schema.EVIDENCE_FIELDS
    for table, columns in swe_schema.COLUMNS.items():
        body = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\) ENGINE",
            DDL.read_text(),
            re.S,
        )[1]
        defined = tuple(
            line.strip().split()[0]
            for line in body.splitlines()
            if line.strip()
            and not line.strip().startswith(
                ("PRIMARY KEY", "INDEX", "UNIQUE KEY"),
            )
        )
        assert columns == defined


def facts(**updates):
    return {
        "branch_run_id": "branch",
        "task_run_id": "task",
        "source_id": "source",
        "tenant_id": "tenant",
        "started_at": "2026-10-12T12:00:00Z",
        "revision": 2,
        "execution_state": "completed",
        "user_id": "user",
        "agent_id": "agent",
        "chat_id": "chat",
        "session_id": "session",
        "role": "main",
        "purpose": "query",
        "execution_origin": "http",
        "execution_id": "e" * 256,
        "identity_source": "request",
        "agent_version": "0.1",
        "phase_id": "phase",
        "max_input_tokens": 12000,
        "schema_version": "1.0",
        "telemetry_source": "runtime",
        **updates,
    }


@pytest.mark.asyncio
async def test_write_then_read_restores_runtime_facts_and_scoped_children(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore as Writer
    from monitor.app.services.token_diagnostics.store import (
        TokenStore as Reader,
    )
    from monitor.app.services.token_diagnostics.query_service import (
        TokenQueryService,
    )

    cursor = RelationalCursor(relational_db)

    class Store(Reader):
        @asynccontextmanager
        async def transaction(self):
            yield cursor

        async def rows(self, query, params=()):
            await cursor.execute(query, params)
            return await self._read_rows(cursor, query)

    writer = Writer()
    run = facts(
        expected_child_branches=["child-b", "child-a"],
        expected_attempt_count=1,
        ended_at="2026-10-12T12:01:00Z",
        gap_tracking_limited=True,
    )
    call = facts(
        call_id="call",
        logical_call_id="logical",
        logical_seq=4,
        attempt_no=2,
        step_id="step",
        context_id="context",
        parent_step_id="branch",
        assembled_at="2026-10-12T12:00:00Z",
        assembly_stage="messages_only",
        prompt_format_confidence="message_approx",
        context_builder_version="1.0",
        prompt_modality="multimodal",
        usage_origin="provider",
        usage_accuracy="reported",
        max_output_tokens=400,
        compaction_epoch=2,
        estimator_version="utf8-density-v1:4",
        fingerprint_available=False,
        expected_item_count=2,
        capture_state="partial",
        estimated_input_tokens=300,
        usage_status="partial",
        input_tokens=300,
        output_tokens=None,
        provider_id=None,
        model_name="model",
        error_code="CancelledError",
        finish_reason="length",
        external_usage_unavailable=True,
        usage_details={
            "cache_read_tokens": 10,
            "cache_write_tokens": 20,
            "reasoning_tokens": 30,
        },
        items=[
            {
                "occurrence_index": 0,
                "category": "tool_result",
                "source_ref": "tool:1",
                "producer_operation_id": "op",
                "fingerprint": "a" * 64,
                "key_version": "v1",
                "estimated_tokens": 300,
                "retained_bytes": 1200,
                "attribution_quality": "explicit",
            },
        ],
    )
    event = facts(
        event_id="event",
        event_seq=1,
        event_type="tool_completed",
        step_id="tool-step",
        parent_step_id="branch",
        ended_at="2026-10-12T12:01:00Z",
        tool_name="search",
        producer_operation_id="op",
        tool_kind="read",
        idempotency_class="idempotent",
        is_cacheable=True,
        data_version="v2",
        arguments_fingerprint="a" * 64,
        result_fingerprint="b" * 64,
        key_version="v1",
        polling=False,
        progress_version="p2",
        result_status="success",
        compaction_epoch=2,
    )
    for kind, data in (("run", run), ("call", call), ("event", event)):
        await writer._write_record(cursor, {"kind": kind, "data": data})
    service = TokenQueryService(Store())
    saved = await service.get_run("task", ("source", "tenant"))
    assert (
        saved["run"]["expected_child_branches"]
        == run["expected_child_branches"]
    )
    for key in run.keys() - {"revision", "ended_at", "started_at"}:
        assert saved["run"][key] == run[key], key
    saved_call = saved["calls"][0]
    for key in call.keys() - {"items", "started_at", "assembled_at"}:
        assert saved_call[key] == call[key], key
    assert saved_call["assembled_at"].startswith("2026-10-12")
    saved_item = (await service.inputs("call", ("source", "tenant")))["items"][
        0
    ]
    for key, value in call["items"][0].items():
        assert saved_item[key] == value
    claim = {"branch_run_id": "branch"}
    snapshot = await Store().analysis_data(claim)
    for key in event.keys() - {"started_at", "ended_at"}:
        assert snapshot["events"][0][key] == event[key], key
    assert (
        snapshot["run"]["expected_child_branches"]
        == run["expected_child_branches"]
    )
    assert await service.get_run("task", ("source", "other")) is None
    assert (await service.inputs("call", ("source", "other")))["items"] == []
    # An old replay cannot replace relations or advance the branch revision.
    revision = snapshot["run"]["data_revision"]
    await writer._write_record(
        cursor,
        {"kind": "call", "data": {**call, "items": []}},
    )
    assert (await Store().analysis_data(claim))["run"][
        "data_revision"
    ] == revision
    assert (
        len((await service.inputs("call", ("source", "tenant")))["items"]) == 1
    )


@pytest.mark.asyncio
async def test_analysis_restores_all_five_rules_evidence_and_missing_fields(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore as Writer
    from monitor.app.services.token_diagnostics.store import (
        TokenStore as Reader,
    )
    from monitor.app.services.token_diagnostics.rules import finding, RULE_IDS
    from monitor.app.services.token_diagnostics.query_service import (
        TokenQueryService,
    )

    cursor = RelationalCursor(relational_db)

    class Store(Reader):
        @asynccontextmanager
        async def transaction(self):
            yield cursor

        async def rows(self, query, params=()):
            await cursor.execute(query, params)
            return await self._read_rows(cursor, query)

    await Writer()._write_record(
        cursor,
        {"kind": "run", "data": facts(expected_child_branches=[])},
    )
    lease = (datetime.now(timezone.utc) + timedelta(seconds=60)).replace(
        tzinfo=None,
    )
    relational_db.execute(
        "UPDATE swe_token_runs SET claim_token=?,lease_until=?,data_revision=8",
        ("lease", lease),
    )
    evidence = [
        {
            "call_ids": ["a", "b", "c"],
            "producer_operation_id": "op",
            "occurrences": 3,
            "minimum_occurrences": 3,
            "threshold_tokens": 8000,
        },
        {
            "call_ids": ["a"],
            "source_refs": [None, "system:1", "system:1"],
            "occurrences": 3,
            "threshold_tokens": 1000,
        },
        {
            "operation_ids": ["op1", "op2", "op3"],
            "tool_name": "search",
            "occurrences": 3,
            "minimum_occurrences": 3,
        },
        {
            "call_ids": ["a", "b"],
            "initial_tokens": 4000,
            "current_tokens": 12000,
            "growth_ratio": 3.0,
            "phase_known": False,
            "threshold_tokens": 8000,
        },
        {
            "call_ids": ["a"],
            "output_tokens": None,
            "output_limit": 200,
            "finish_reason": "length",
            "threshold_ratio": 0.9,
        },
    ]
    issues = [
        finding(rule, rule, "title", "action", values, 8000)
        for rule, values in zip(RULE_IDS, evidence)
    ]
    rules = [
        {
            "rule_id": rule,
            "rule_version": 1,
            "state": "insufficient_evidence" if index == 0 else "evaluated",
            "missing_fields": (
                ["fingerprint", "complete_input_capture"] if index == 0 else []
            ),
        }
        for index, rule in enumerate(RULE_IDS)
    ]
    claim = {
        "branch_run_id": "branch",
        "source_id": "source",
        "tenant_id": "tenant",
        "claim_token": "lease",
    }
    assert await Store().commit_analysis(
        claim,
        7,
        {"findings": issues, "rules": rules},
    )
    result = await TokenQueryService(Store()).get_run(
        "task",
        ("source", "tenant"),
    )
    assert result["run"]["analysis_summary"] == rules
    assert result["run"]["analysis_state"] == "pending"
    assert result["run"]["dirty"] == 1
    by_rule = {issue["rule_id"]: issue for issue in result["findings"]}
    for issue in issues:
        for key, value in issue.items():
            assert by_rule[issue["rule_id"]][key] == value, (
                issue["rule_id"],
                key,
            )
    # Replaced lease must not alter findings or evaluations.
    assert not await Store().commit_analysis(
        {**claim, "claim_token": "other"},
        8,
        {"findings": [], "rules": []},
    )
    assert (
        len(
            await TokenQueryService(Store()).findings(
                "task",
                ("source", "tenant"),
            ),
        )
        == 5
    )


@pytest.mark.asyncio
async def test_replay_persists_new_gap_limits_once_and_late_run_keeps_trace(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore

    cursor = RelationalCursor(relational_db)
    store = TokenStore()
    initial = facts(trace_id="trace", expected_child_branches=["old-child"])
    await store._write_record(cursor, {"kind": "run", "data": initial})
    await store._write_record(
        cursor,
        {"kind": "run", "data": {**initial, "gap_tracking_limited": True}},
    )
    row = dict(
        relational_db.execute(
            "SELECT data_revision,gap_tracking_limited FROM swe_token_runs",
        ).fetchone(),
    )
    assert row == {"data_revision": 2, "gap_tracking_limited": 1}
    await store._write_record(
        cursor,
        {"kind": "run", "data": {**initial, "gap_tracking_limited": True}},
    )
    assert (
        dict(
            relational_db.execute(
                "SELECT data_revision FROM swe_token_runs",
            ).fetchone(),
        )["data_revision"]
        == 2
    )
    await store._write_record(
        cursor,
        {
            "kind": "run",
            "data": {
                **initial,
                "revision": 3,
                "trace_id": None,
                "expected_child_branches": ["new-child"],
            },
        },
    )
    row = dict(
        relational_db.execute(
            "SELECT trace_id,gap_tracking_limited FROM swe_token_runs",
        ).fetchone(),
    )
    assert row == {"trace_id": "trace", "gap_tracking_limited": 1}
    children = relational_db.execute(
        "SELECT child_branch_run_id FROM swe_token_expected_children",
    ).fetchall()
    assert [dict(child)["child_branch_run_id"] for child in children] == [
        "new-child",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["call", "event"])
async def test_child_before_run_does_not_invent_terminal_branch_lifecycle(
    relational_db,
    kind,
):
    from swe.token_diagnostics.store import TokenStore

    cursor = RelationalCursor(relational_db)
    store = TokenStore()
    terminal = facts(
        call_id="call",
        logical_call_id="logical",
        logical_seq=1,
        event_id="event",
        event_type="tool_completed",
        ended_at="2026-10-12T12:01:00Z",
        expected_attempt_count=99,
    )
    await store._write_record(cursor, {"kind": kind, "data": terminal})
    row = dict(
        relational_db.execute(
            "SELECT revision,execution_state,ended_at,expected_attempt_count,execution_origin FROM swe_token_runs",
        ).fetchone(),
    )
    assert row == {
        "revision": 0,
        "execution_state": "running",
        "ended_at": None,
        "expected_attempt_count": None,
        "execution_origin": "http",
    }
    await store._write_record(
        cursor,
        {
            "kind": "run",
            "data": facts(
                ended_at="2026-10-12T12:02:00Z",
                expected_attempt_count=1,
            ),
        },
    )
    row = dict(
        relational_db.execute(
            "SELECT revision,execution_state,expected_attempt_count FROM swe_token_runs",
        ).fetchone(),
    )
    assert row == {
        "revision": 2,
        "execution_state": "completed",
        "expected_attempt_count": 1,
    }


@pytest.mark.asyncio
async def test_compaction_unknown_start_stays_null_after_sql_persistence(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore

    cursor = RelationalCursor(relational_db)
    event = facts(
        event_id="compaction",
        event_type="compaction",
        started_at=None,
        created_at="2026-10-12T12:01:00Z",
        ended_at="2026-10-12T12:01:00Z",
    )
    await TokenStore()._write_record(cursor, {"kind": "event", "data": event})
    saved = dict(
        relational_db.execute(
            "SELECT started_at,ended_at FROM swe_token_execution_events",
        ).fetchone(),
    )
    assert saved["started_at"] is None
    assert saved["ended_at"] is not None
    # The required branch placeholder still receives its legacy timestamp fallback.
    run = dict(
        relational_db.execute(
            "SELECT started_at FROM swe_token_runs",
        ).fetchone(),
    )
    assert run["started_at"] is not None
