# -*- coding: utf-8 -*-
"""Token-only MySQL transactions; never borrow a legacy connection context."""

import os
from datetime import datetime, timezone

from .contract import validate_schema_version
from .storage_schema import (
    COLUMNS,
    DEFAULTS,
    RUN_WRITE_COLUMNS,
    USAGE_DETAIL_FIELDS,
)

try:
    import aiomysql
except ImportError:
    aiomysql = None

_TABLES = {
    "call": ("swe_token_model_calls", "call_id"),
    "run": ("swe_token_runs", "branch_run_id"),
    "event": ("swe_token_execution_events", "event_id"),
    "context": ("swe_token_contexts", "context_id"),
    "context_operation": ("swe_token_context_operations", "operation_id"),
}


def scalar_values(columns, data):
    values = []
    for column in columns:
        value = data.get(column, DEFAULTS.get(column))
        if column.endswith("_at"):
            default_required = column in {"started_at", "created_at"}
            # Explicitly unknown event starts are observations, not branch defaults.
            if (
                column == "started_at"
                and ("event_id" in columns or "operation_id" in columns)
                and column in data
            ):
                default_required = False
            value = utc_time(value) if value or default_required else None
        values.append(value)
    return tuple(values)


async def insert_record(cursor, table, data, ignore=False):
    columns = COLUMNS[table]
    marks = ",".join(["%s"] * len(columns))
    updates = ",".join(f"{column}=VALUES({column})" for column in columns)
    suffix = "" if ignore else f" ON DUPLICATE KEY UPDATE {updates}"
    prefix = "INSERT IGNORE" if ignore else "INSERT"
    await cursor.execute(
        f"{prefix} INTO {table} ({','.join(columns)}) VALUES ({marks}){suffix}",
        scalar_values(columns, data),
    )


def utc_time(value=None):
    if value:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TokenStore:
    def __init__(self, pool=None):
        self.pool = pool

    async def ensure_pool(self):
        if self.pool is not None:
            return
        if aiomysql is None:
            raise RuntimeError("token_database_driver_unavailable")
        host = os.environ.get("SWE_DB_HOST", "")
        if not host:
            raise RuntimeError("token_database_unconfigured")
        password = os.environ.get("SWE_DB_ACCESS", "")
        password = password[4:] if password.startswith("BEE_") else password
        self.pool = await aiomysql.create_pool(
            host=host,
            port=int(os.environ.get("SWE_DB_PORT", 3306)),
            user=os.environ.get("SWE_DB_USER", "root"),
            password=password,
            db=os.environ.get("SWE_DB_NAME", "swe"),
            charset="utf8mb4",
            minsize=1,
            maxsize=2,
            autocommit=False,
            connect_timeout=3,
        )

    async def write_batch(self, records):
        await self.ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    for record in records:
                        await self._write_record(cursor, record)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def _ensure_run(self, cursor, data):
        # Calls/events may arrive before the initial branch snapshot.
        columns = RUN_WRITE_COLUMNS
        await cursor.execute(
            f"INSERT IGNORE INTO swe_token_runs ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})",
            scalar_values(
                columns,
                {
                    **data,
                    "revision": 0,
                    "execution_state": "running",
                    "ended_at": None,
                    "expected_attempt_count": None,
                },
            ),
        )
        await cursor.execute(
            "SELECT branch_run_id,source_id,tenant_id,task_run_id FROM swe_token_runs WHERE branch_run_id=%s FOR UPDATE",
            (data["branch_run_id"],),
        )
        row = await cursor.fetchone()
        if row and any(
            row.get(key, data[key]) != data[key]
            for key in ("source_id", "tenant_id", "task_run_id")
        ):
            raise ValueError("token_record_scope_mismatch")

    async def _write_record(self, cursor, record):
        kind, data = record["kind"], record["data"]
        validate_schema_version(data.get("schema_version", "1.0"))
        if kind == "context":
            data = {**data, "revision": data["context_revision"]}
        await self._ensure_run(cursor, data)
        table, key = _TABLES[kind]
        await cursor.execute(
            f"SELECT revision,source_id,tenant_id,task_run_id,branch_run_id FROM {table} WHERE {key}=%s FOR UPDATE",
            (data[key],),
        )
        previous = await cursor.fetchone()
        if previous and any(
            previous.get(column, data[column]) != data[column]
            for column in (
                "source_id",
                "tenant_id",
                "task_run_id",
                "branch_run_id",
            )
        ):
            raise ValueError("token_record_scope_mismatch")
        if previous and previous["revision"] >= data.get("revision", 1):
            await self._write_replay_gaps(cursor, data)
            return
        if kind == "run":
            await self._write_run(cursor, data)
        elif kind == "call":
            await self._write_call(cursor, data)
        elif kind in {"context", "context_operation"}:
            from .context_storage import write_context, write_operation

            if kind == "context":
                await write_context(cursor, data)
            else:
                await write_operation(cursor, data)
        else:
            await self._write_event(cursor, data)
        await cursor.execute(
            "UPDATE swe_token_runs SET data_revision=data_revision+1, dirty=1, analysis_state='pending', trace_id=COALESCE(%s,trace_id), gap_count=GREATEST(gap_count,%s), gap_tracking_limited=GREATEST(gap_tracking_limited,%s) WHERE branch_run_id=%s",
            (
                data.get("trace_id"),
                data.get("gap_count", 0),
                int(data.get("gap_tracking_limited", False)),
                data["branch_run_id"],
            ),
        )

    async def _write_replay_gaps(self, cursor, data):
        count = data.get("gap_count", 0)
        limited = int(data.get("gap_tracking_limited", False))
        if count or limited:
            await cursor.execute(
                "UPDATE swe_token_runs SET gap_count=GREATEST(gap_count,%s),gap_tracking_limited=GREATEST(gap_tracking_limited,%s),data_revision=data_revision+1,dirty=1,analysis_state='pending' WHERE branch_run_id=%s AND (gap_count<%s OR gap_tracking_limited<%s)",
                (count, limited, data["branch_run_id"], count, limited),
            )

    async def _write_run(self, cursor, data):
        columns = tuple(
            column for column in RUN_WRITE_COLUMNS if column != "branch_run_id"
        )
        assignments = ",".join(
            (
                "trace_id=COALESCE(%s,trace_id)"
                if column == "trace_id"
                else f"{column}=%s"
            )
            for column in columns
        )
        await cursor.execute(
            f"UPDATE swe_token_runs SET {assignments} WHERE branch_run_id=%s",
            scalar_values(columns, data) + (data["branch_run_id"],),
        )
        if not data.get("capture_fragment_id"):
            await cursor.execute(
                "DELETE FROM swe_token_expected_children WHERE branch_run_id=%s",
                (data["branch_run_id"],),
            )
        await self._write_children(cursor, data)
        await self._aggregate_fragments(cursor, data)

    async def _write_children(self, cursor, data):
        for index, child in enumerate(data.get("expected_child_branches", [])):
            await insert_record(
                cursor,
                "swe_token_expected_children",
                {
                    **data,
                    "occurrence_index": index,
                    "child_branch_run_id": child,
                },
                ignore=True,
            )

    async def _write_call(self, cursor, data):
        details = data.get("usage_details") or {}
        flattened = {
            field: details.get(field) for field in USAGE_DETAIL_FIELDS
        }
        await insert_record(
            cursor,
            "swe_token_model_calls",
            {**data, **flattened},
        )
        await cursor.execute(
            "DELETE FROM swe_token_input_items WHERE call_id=%s",
            (data["call_id"],),
        )
        for item in data.get("items", []):
            await insert_record(
                cursor,
                "swe_token_input_items",
                {**data, **item},
            )

    async def _write_event(self, cursor, data):
        await insert_record(cursor, "swe_token_execution_events", data)
        if data.get("event_type") == "accounting_fragment":
            await self._write_children(cursor, data)
            await self._aggregate_fragments(cursor, data)

    async def _aggregate_fragments(self, cursor, data):
        await cursor.execute(
            "SELECT expected_attempt_count,started_at,ended_at,execution_state FROM swe_token_execution_events WHERE branch_run_id=%s AND source_id=%s AND tenant_id=%s AND event_type='accounting_fragment' ORDER BY event_id LIMIT 1001 FOR UPDATE",
            (data["branch_run_id"], data["source_id"], data["tenant_id"]),
        )
        fragments = await cursor.fetchall()
        if not fragments:
            return
        if len(fragments) > 1000:
            await cursor.execute(
                "UPDATE swe_token_runs SET expected_attempt_count=NULL,ended_at=NULL,execution_state='unknown',gap_tracking_limited=1 WHERE branch_run_id=%s AND source_id=%s AND tenant_id=%s",
                (data["branch_run_id"], data["source_id"], data["tenant_id"]),
            )
            return
        expected = [row["expected_attempt_count"] for row in fragments]
        starts = [row["started_at"] for row in fragments if row["started_at"]]
        ends = [row["ended_at"] for row in fragments if row["ended_at"]]
        states = {row["execution_state"] for row in fragments}
        state = "completed"
        for candidate in ("running", "error", "cancelled"):
            if candidate in states:
                state = candidate
                break
        await cursor.execute(
            "UPDATE swe_token_runs SET expected_attempt_count=%s, started_at=%s, ended_at=%s, execution_state=%s WHERE branch_run_id=%s AND source_id=%s AND tenant_id=%s",
            (
                None if None in expected else sum(expected),
                min(starts) if starts else None,
                max(ends) if ends and "running" not in states else None,
                state,
                data["branch_run_id"],
                data["source_id"],
                data["tenant_id"],
            ),
        )

    def abort(self):
        if self.pool is not None:
            self.pool.terminate()

    async def close(self):
        if self.pool is not None:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
