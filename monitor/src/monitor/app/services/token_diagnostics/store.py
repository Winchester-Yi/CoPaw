# -*- coding: utf-8 -*-
"""Own Monitor pool and leases for independent Token records."""

import asyncio
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import aiomysql

from ...database.config import get_database_config
from .storage_schema import (
    BOOLEAN_FIELDS,
    COLUMNS,
    EVIDENCE_FIELDS,
    USAGE_DETAIL_FIELDS,
)


def decode_row(row):
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat() + (
                "Z" if value.tzinfo is None else ""
            )
        elif key in BOOLEAN_FIELDS and value is not None:
            result[key] = bool(value)
    if "logical_call_id" in result:
        result["usage_details"] = {
            field: result.pop(field)
            for field in USAGE_DETAIL_FIELDS
            if result.get(field) is not None
        }
        for field in USAGE_DETAIL_FIELDS:
            result.pop(field, None)
    return result


def select_columns(table):
    return ",".join(COLUMNS[table])


def scope_key(row, identity="branch_run_id"):
    return row[identity], row["source_id"], row["tenant_id"]


def group_rows(rows, key=scope_key):
    grouped = defaultdict(list)
    for row in rows[:10000]:
        grouped[key(row)].append(row)
    return grouped


def rule_key(row):
    return scope_key(row) + (row["rule_id"], row["rule_version"])


def finding_scope(row):
    return scope_key(row, "finding_id")


def restore_rule(rule, missing):
    return {
        "rule_id": rule["rule_id"],
        "rule_version": rule["rule_version"],
        "state": rule["state"],
        "missing_fields": [
            field["field_name"] for field in missing.get(rule_key(rule), [])
        ],
    }


def restore_finding(row, references):
    evidence = {}
    for field in EVIDENCE_FIELDS[row["rule_id"]]:
        if field in references:
            evidence[field] = references[field].get(finding_scope(row), [])
        else:
            evidence[field] = row[field]
    scalars = set().union(*EVIDENCE_FIELDS.values()) - references.keys()
    for field in scalars:
        row.pop(field, None)
    row["evidence"] = evidence


async def insert_record(cursor, table, data):
    columns = COLUMNS[table]
    updates = ",".join(f"{column}=VALUES({column})" for column in columns)
    await cursor.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE {updates}",
        tuple(data.get(column) for column in columns),
    )


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
        # Base facts and typed relations must describe the same revision.
        async with self.transaction() as cursor:
            await cursor.execute(query, params)
            return await self._read_rows(cursor, query)

    async def _read_rows(self, cursor, query):
        rows = [decode_row(row) for row in await cursor.fetchall()]
        if not rows:
            return rows
        table = re.search(r"\bFROM\s+(swe_token_\w+)", query, re.I)
        if (
            table
            and table[1] == "swe_token_runs"
            and "analysis_state" in rows[0]
        ):
            await self._attach_runs(cursor, rows)
        elif table and table[1] == "swe_token_findings":
            await self._attach_findings(cursor, rows)
        return rows

    async def _related_rows(
        self,
        cursor,
        table,
        rows,
        identity="branch_run_id",
    ):
        marks = ",".join(["(%s,%s,%s)"] * len(rows))
        params = tuple(
            value for row in rows for value in scope_key(row, identity)
        )
        await cursor.execute(
            f"SELECT {select_columns(table)} FROM {table} WHERE ({identity},source_id,tenant_id) IN ({marks}) ORDER BY occurrence_index LIMIT 10001",
            params,
        )
        return [decode_row(row) for row in await cursor.fetchall()]

    async def _attach_runs(self, cursor, rows):
        children = await self._related_rows(
            cursor,
            "swe_token_expected_children",
            rows,
        )
        rules = await self._related_rows(
            cursor,
            "swe_token_rule_evaluations",
            rows,
        )
        missing = await self._related_rows(
            cursor,
            "swe_token_rule_missing_fields",
            rows,
        )
        limited = any(
            len(values) > 10000 for values in (children, rules, missing)
        )
        child_groups, rule_groups = group_rows(children), group_rows(rules)
        missing_groups = group_rows(missing, rule_key)
        for row in rows:
            key = scope_key(row)
            row["expected_child_branches"] = [
                child["child_branch_run_id"] for child in child_groups[key]
            ]
            row["analysis_summary"] = [
                restore_rule(rule, missing_groups) for rule in rule_groups[key]
            ]
            if limited:
                row["gap_tracking_limited"] = True

    async def _attach_findings(self, cursor, rows):
        references, limited = {}, False
        for name, table, column in (
            ("call_ids", "swe_token_finding_calls", "call_id"),
            (
                "operation_ids",
                "swe_token_finding_operations",
                "producer_operation_id",
            ),
            ("source_refs", "swe_token_finding_sources", "source_ref"),
        ):
            related = await self._related_rows(
                cursor,
                table,
                rows,
                "finding_id",
            )
            limited = limited or len(related) > 10000
            references[name] = {
                key: [ref[column] for ref in values]
                for key, values in group_rows(related, finding_scope).items()
            }
        for row in rows:
            restore_finding(row, references)
            if limited:
                row["evidence_limited"] = True

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
                f"SELECT {select_columns('swe_token_runs')} FROM swe_token_runs WHERE branch_run_id=%s FOR UPDATE",
                (claim["branch_run_id"],),
            )
            run = decode_row(await cursor.fetchone())
            await self._attach_runs(cursor, [run])
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
                    f"SELECT {select_columns(table)} FROM {table} WHERE branch_run_id=%s ORDER BY {order} LIMIT %s",
                    (claim["branch_run_id"], limit + 1),
                )
                rows = [decode_row(row) for row in await cursor.fetchall()]
                data["truncated"] = data["truncated"] or len(rows) > limit
                data[key] = rows[:limit]
            return data

    async def commit_analysis(self, claim, revision, result):
        async with self.transaction() as cursor:
            await cursor.execute(
                f"SELECT {select_columns('swe_token_runs')} FROM swe_token_runs WHERE branch_run_id=%s FOR UPDATE",
                (claim["branch_run_id"],),
            )
            row = await cursor.fetchone()
            if not valid_claim(row, claim["claim_token"]):
                return False
            await self._write_evaluations(
                cursor,
                claim,
                revision,
                result["rules"],
            )
            for issue in result["findings"]:
                await self._write_finding(
                    cursor,
                    {**row, **claim},
                    revision,
                    issue,
                )
            incomplete = any(
                rule["state"] == "insufficient_evidence"
                for rule in result["rules"]
            )
            state = "insufficient_evidence" if incomplete else "ready"
            dirty = int(row["data_revision"] > revision)
            await cursor.execute(
                "UPDATE swe_token_runs SET analyzed_revision=%s,analysis_state=%s,analysis_error=NULL,dirty=%s,claim_token=NULL,lease_until=NULL WHERE branch_run_id=%s",
                (
                    revision,
                    "pending" if dirty else state,
                    dirty,
                    claim["branch_run_id"],
                ),
            )
            return True

    async def _write_evaluations(self, cursor, claim, revision, rules):
        for table in (
            "swe_token_rule_missing_fields",
            "swe_token_rule_evaluations",
        ):
            await cursor.execute(
                f"DELETE FROM {table} WHERE branch_run_id=%s",
                (claim["branch_run_id"],),
            )
        for rule_index, rule in enumerate(rules):
            data = {
                **claim,
                **rule,
                "observed_revision": revision,
                "occurrence_index": rule_index,
            }
            await insert_record(cursor, "swe_token_rule_evaluations", data)
            for index, field in enumerate(rule["missing_fields"]):
                await insert_record(
                    cursor,
                    "swe_token_rule_missing_fields",
                    {**data, "occurrence_index": index, "field_name": field},
                )
            if rule["state"] == "evaluated":
                await cursor.execute(
                    "UPDATE swe_token_findings SET state='resolved' WHERE branch_run_id=%s AND rule_id=%s AND rule_version=%s",
                    (
                        claim["branch_run_id"],
                        rule["rule_id"],
                        rule["rule_version"],
                    ),
                )

    async def _write_finding(self, cursor, run, revision, issue):
        from .rules import key_of

        identity = key_of(
            [
                run["source_id"],
                run["tenant_id"],
                run["branch_run_id"],
                issue["rule_id"],
                issue["rule_version"],
                issue["finding_key"],
            ],
        )
        data = {
            **run,
            **issue,
            **issue["evidence"],
            "finding_id": identity,
            "observed_revision": revision,
            "state": "active",
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        await insert_record(cursor, "swe_token_findings", data)
        for name, table, column in (
            ("call_ids", "swe_token_finding_calls", "call_id"),
            (
                "operation_ids",
                "swe_token_finding_operations",
                "producer_operation_id",
            ),
            ("source_refs", "swe_token_finding_sources", "source_ref"),
        ):
            await cursor.execute(
                f"DELETE FROM {table} WHERE finding_id=%s",
                (identity,),
            )
            for index, value in enumerate(issue["evidence"].get(name, [])):
                await insert_record(
                    cursor,
                    table,
                    {**data, "occurrence_index": index, column: value},
                )

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
                "swe_token_finding_calls",
                "swe_token_finding_operations",
                "swe_token_finding_sources",
                "swe_token_rule_missing_fields",
                "swe_token_rule_evaluations",
                "swe_token_expected_children",
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
