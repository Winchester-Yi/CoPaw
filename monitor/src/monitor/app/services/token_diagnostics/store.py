# -*- coding: utf-8 -*-
"""Own Monitor pool and leases for independent Token records."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import aiomysql

from ...database.config import get_database_config


def decode_row(row):
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    result = {**payload, **{k: v for k, v in row.items() if k != "payload"}}
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat() + (
                "Z" if value.tzinfo is None else ""
            )
    return result


def valid_claim(row, token):
    if not row or row.get("claim_token") != token:
        return False
    lease = row.get("lease_until")
    if isinstance(lease, str):
        lease = datetime.fromisoformat(lease.replace("Z", "+00:00"))
    if not isinstance(lease, datetime):
        return False
    if lease.tzinfo:
        lease = lease.astimezone(timezone.utc).replace(tzinfo=None)
    return lease > datetime.now(timezone.utc).replace(tzinfo=None)


class TokenStore:
    def __init__(self, pool=None):
        self.pool = pool
        self._pool_lock = asyncio.Lock()

    async def ensure_pool(self):
        async with self._pool_lock:
            if self.pool is None:
                config = get_database_config()
                self.pool = await aiomysql.create_pool(
                    host=config.host,
                    port=config.port,
                    user=config.user,
                    password=config.password,
                    db=config.database,
                    minsize=1,
                    maxsize=2,
                    charset="utf8mb4",
                    autocommit=True,
                    connect_timeout=3,
                )

    @asynccontextmanager
    async def transaction(self):
        await self.ensure_pool()
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor(aiomysql.DictCursor) as cursor:
                    yield cursor
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def rows(self, query, params=()):
        await self.ensure_pool()
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, params)
                return [decode_row(row) for row in await cursor.fetchall()]

    async def claim_next(self):
        candidates = await self.rows(
            "SELECT branch_run_id,source_id,tenant_id FROM swe_token_runs WHERE dirty=1 AND (lease_until IS NULL OR lease_until<UTC_TIMESTAMP(6)) ORDER BY started_at LIMIT 50",
        )
        for candidate in candidates:
            token = str(uuid4())
            async with self.transaction() as cursor:
                await cursor.execute(
                    "UPDATE swe_token_runs SET claim_token=%s,lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 30 SECOND),analysis_state='running' WHERE branch_run_id=%s AND dirty=1 AND (lease_until IS NULL OR lease_until<UTC_TIMESTAMP(6))",
                    (token, candidate["branch_run_id"]),
                )
                if cursor.rowcount:
                    return {**candidate, "claim_token": token}
        return None

    async def renew(self, claim):
        async with self.transaction() as cursor:
            await cursor.execute(
                "UPDATE swe_token_runs SET lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 30 SECOND) WHERE branch_run_id=%s AND claim_token=%s AND lease_until>UTC_TIMESTAMP(6)",
                (claim["branch_run_id"], claim["claim_token"]),
            )
            return bool(cursor.rowcount)

    async def analysis_data(self, claim):
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT * FROM swe_token_runs WHERE branch_run_id=%s FOR UPDATE",
                (claim["branch_run_id"],),
            )
            run = decode_row(await cursor.fetchone())
            data = {"run": run, "truncated": False}
            for key, table, limit, order in (
                (
                    "calls",
                    "swe_token_model_calls",
                    256,
                    "logical_seq,attempt_no",
                ),
                (
                    "items",
                    "swe_token_input_items",
                    10000,
                    "call_id,occurrence_index",
                ),
                ("events", "swe_token_execution_events", 1000, "event_seq"),
            ):
                await cursor.execute(
                    f"SELECT * FROM {table} WHERE branch_run_id=%s ORDER BY {order} LIMIT %s",
                    (claim["branch_run_id"], limit + 1),
                )
                rows = [decode_row(row) for row in await cursor.fetchall()]
                data["truncated"] = data["truncated"] or len(rows) > limit
                data[key] = rows[:limit]
            return data

    async def commit_analysis(self, claim, revision, result):
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT * FROM swe_token_runs WHERE branch_run_id=%s FOR UPDATE",
                (claim["branch_run_id"],),
            )
            row = await cursor.fetchone()
            if not valid_claim(row, claim["claim_token"]):
                return False
            for rule in result["rules"]:
                if rule["state"] == "evaluated":
                    await cursor.execute(
                        "UPDATE swe_token_findings SET state='resolved' WHERE branch_run_id=%s AND rule_id=%s AND rule_version=%s",
                        (
                            claim["branch_run_id"],
                            rule["rule_id"],
                            rule["rule_version"],
                        ),
                    )
            for issue in result["findings"]:
                from .rules import key_of

                identity = key_of(
                    [
                        claim["source_id"],
                        claim["tenant_id"],
                        claim["branch_run_id"],
                        issue["rule_id"],
                        issue["rule_version"],
                        issue["finding_key"],
                    ]
                )
                await cursor.execute(
                    "INSERT INTO swe_token_findings (finding_id,task_run_id,branch_run_id,source_id,tenant_id,rule_id,rule_version,finding_key,observed_revision,state,updated_at,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',UTC_TIMESTAMP(6),%s) ON DUPLICATE KEY UPDATE observed_revision=VALUES(observed_revision),state='active',updated_at=VALUES(updated_at),payload=VALUES(payload)",
                    (
                        identity,
                        row["task_run_id"],
                        claim["branch_run_id"],
                        claim["source_id"],
                        claim["tenant_id"],
                        issue["rule_id"],
                        issue["rule_version"],
                        issue["finding_key"],
                        revision,
                        json.dumps(issue, ensure_ascii=False),
                    ),
                )
            incomplete = any(
                rule["state"] == "insufficient_evidence"
                for rule in result["rules"]
            )
            state = "insufficient_evidence" if incomplete else "ready"
            dirty = int(row["data_revision"] > revision)
            await cursor.execute(
                "UPDATE swe_token_runs SET analyzed_revision=%s,analysis_summary=%s,analysis_state=%s,analysis_error=NULL,dirty=%s,claim_token=NULL,lease_until=NULL WHERE branch_run_id=%s",
                (
                    revision,
                    json.dumps(result["rules"]),
                    "pending" if dirty else state,
                    dirty,
                    claim["branch_run_id"],
                ),
            )
            return True

    async def fail_analysis(self, claim, code):
        async with self.transaction() as cursor:
            await cursor.execute(
                "UPDATE swe_token_runs SET analysis_state='failed',analysis_error=%s,claim_token=NULL,lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 30 SECOND) WHERE branch_run_id=%s AND claim_token=%s",
                (code[:64], claim["branch_run_id"], claim["claim_token"]),
            )

    async def cleanup(self, retention_days):
        # Delete a bounded set of ended branches. Active leases and descendants
        # keep the whole task tree alive, even after its main branch has ended.
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT r.branch_run_id FROM swe_token_runs r WHERE r.ended_at<DATE_SUB(UTC_TIMESTAMP(6),INTERVAL %s DAY) AND (r.lease_until IS NULL OR r.lease_until<UTC_TIMESTAMP(6)) AND NOT EXISTS (SELECT 1 FROM swe_token_runs child WHERE child.task_run_id=r.task_run_id AND (child.ended_at IS NULL OR child.ended_at>=DATE_SUB(UTC_TIMESTAMP(6),INTERVAL %s DAY) OR child.lease_until>UTC_TIMESTAMP(6))) ORDER BY r.parent_branch_run_id IS NULL,r.ended_at LIMIT 20 FOR UPDATE",
                (retention_days, retention_days),
            )
            identities = [
                row["branch_run_id"] for row in await cursor.fetchall()
            ]
            if not identities:
                return 0
            marks = ",".join(["%s"] * len(identities))
            for table in (
                "swe_token_findings",
                "swe_token_input_items",
                "swe_token_model_calls",
                "swe_token_execution_events",
                "swe_token_runs",
            ):
                await cursor.execute(
                    f"DELETE FROM {table} WHERE branch_run_id IN ({marks})",
                    tuple(identities),
                )
            return len(identities)

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
