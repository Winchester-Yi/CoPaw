# -*- coding: utf-8 -*-
"""Token-only MySQL transactions; never borrow a legacy connection context."""

import json
import os
from datetime import datetime, timezone

try:
    import aiomysql
except ImportError:
    aiomysql = None

_TABLES = {
    "call": ("swe_token_model_calls", "call_id"),
    "run": ("swe_token_runs", "branch_run_id"),
    "event": ("swe_token_execution_events", "event_id"),
}


def json_value(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
        meta = {
            k: data.get(k)
            for k in (
                "branch_run_id",
                "task_run_id",
                "parent_branch_run_id",
                "source_id",
                "tenant_id",
                "user_id",
                "agent_id",
                "chat_id",
                "session_id",
                "trace_id",
                "role",
                "purpose",
            )
        }
        await cursor.execute(
            "INSERT IGNORE INTO swe_token_runs (branch_run_id,task_run_id,parent_branch_run_id,source_id,tenant_id,trace_id,started_at,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                data["branch_run_id"],
                data["task_run_id"],
                data.get("parent_branch_run_id"),
                data["source_id"],
                data["tenant_id"],
                data.get("trace_id"),
                utc_time(data.get("started_at")),
                json_value(meta),
            ),
        )
        await cursor.execute(
            "SELECT branch_run_id FROM swe_token_runs WHERE branch_run_id=%s FOR UPDATE",
            (data["branch_run_id"],),
        )
        await cursor.fetchone()

    async def _write_record(self, cursor, record):
        kind, data = record["kind"], record["data"]
        await self._ensure_run(cursor, data)
        table, key = _TABLES[kind]
        await cursor.execute(
            f"SELECT revision FROM {table} WHERE {key}=%s FOR UPDATE",
            (data[key],),
        )
        previous = await cursor.fetchone()
        if previous and previous["revision"] >= data.get("revision", 1):
            if data.get("gap_count", 0) > 0:
                await cursor.execute(
                    "UPDATE swe_token_runs SET gap_count=%s,data_revision=data_revision+1,dirty=1,analysis_state='pending' WHERE branch_run_id=%s AND gap_count<%s",
                    (
                        data["gap_count"],
                        data["branch_run_id"],
                        data["gap_count"],
                    ),
                )
            return
        if kind == "run":
            await self._write_run(cursor, data)
        elif kind == "call":
            await self._write_call(cursor, data)
        else:
            await self._write_event(cursor, data)
        await cursor.execute(
            "UPDATE swe_token_runs SET data_revision=data_revision+1, dirty=1, analysis_state='pending', trace_id=COALESCE(%s,trace_id), gap_count=GREATEST(gap_count,%s) WHERE branch_run_id=%s",
            (
                data.get("trace_id"),
                data.get("gap_count", 0),
                data["branch_run_id"],
            ),
        )

    async def _write_run(self, cursor, data):
        await cursor.execute(
            "UPDATE swe_token_runs SET revision=%s,payload=%s,execution_state=%s,ended_at=%s,expected_attempt_count=%s WHERE branch_run_id=%s",
            (
                data.get("revision", 1),
                json_value(data),
                data.get("execution_state", "running"),
                utc_time(data["ended_at"]) if data.get("ended_at") else None,
                data.get("expected_attempt_count"),
                data["branch_run_id"],
            ),
        )

    async def _write_call(self, cursor, data):
        payload = {k: v for k, v in data.items() if k != "items"}
        columns = (
            "call_id",
            "task_run_id",
            "branch_run_id",
            "source_id",
            "tenant_id",
            "logical_call_id",
            "logical_seq",
            "attempt_no",
            "revision",
            "execution_state",
            "usage_status",
            "capture_state",
            "input_tokens",
            "output_tokens",
            "model_name",
            "provider_id",
            "trace_id",
        )
        values = tuple(data.get(k) for k in columns)
        names = ",".join(columns) + ",started_at,ended_at,payload"
        updates = ",".join(f"{k}=VALUES({k})" for k in columns[1:])
        await cursor.execute(
            f"INSERT INTO swe_token_model_calls ({names}) VALUES ({','.join(['%s'] * (len(columns) + 3))}) ON DUPLICATE KEY UPDATE {updates},started_at=VALUES(started_at),ended_at=VALUES(ended_at),payload=VALUES(payload)",
            values
            + (
                utc_time(data.get("started_at")),
                utc_time(data["ended_at"]) if data.get("ended_at") else None,
                json_value(payload),
            ),
        )
        await cursor.execute(
            "DELETE FROM swe_token_input_items WHERE call_id=%s",
            (data["call_id"],),
        )
        for item in data.get("items", []):
            await cursor.execute(
                "INSERT INTO swe_token_input_items (call_id,occurrence_index,branch_run_id,task_run_id,source_id,tenant_id,category,fingerprint,estimated_tokens,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    data["call_id"],
                    item["occurrence_index"],
                    data["branch_run_id"],
                    data["task_run_id"],
                    data["source_id"],
                    data["tenant_id"],
                    item["category"],
                    item.get("fingerprint"),
                    item.get("estimated_tokens"),
                    json_value(item),
                ),
            )

    async def _write_event(self, cursor, data):
        await cursor.execute(
            "INSERT INTO swe_token_execution_events (event_id,task_run_id,branch_run_id,source_id,tenant_id,event_seq,event_type,revision,created_at,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE revision=VALUES(revision),payload=VALUES(payload)",
            (
                data["event_id"],
                data["task_run_id"],
                data["branch_run_id"],
                data["source_id"],
                data["tenant_id"],
                data.get("event_seq", 0),
                data["event_type"],
                data.get("revision", 1),
                utc_time(data.get("created_at")),
                json_value(data),
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
