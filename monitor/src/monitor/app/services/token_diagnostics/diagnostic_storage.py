# -*- coding: utf-8 -*-
"""Scoped reads of typed B evidence using the caller's consistent snapshot."""

import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from .diagnostic_contract import validate_evaluation_state
from .store import decode_row, insert_record, select_columns

_SCOPE_FIELDS = ("task_run_id", "branch_run_id", "source_id", "tenant_id")


async def read_object(cursor, table, identity_column, identity, scope):
    await cursor.execute(
        f"SELECT {select_columns(table)} FROM {table} WHERE {identity_column}=%s AND source_id=%s AND tenant_id=%s",
        (identity, *scope),
    )
    row = await cursor.fetchone()
    return decode_row(row) if row else None


async def read_children(
    cursor,
    table,
    identity_column,
    identity,
    scope,
    order,
    limit,
):
    await cursor.execute(
        f"SELECT {select_columns(table)} FROM {table} WHERE {identity_column}=%s AND source_id=%s AND tenant_id=%s ORDER BY {order} LIMIT %s",
        (identity, *scope, limit + 1),
    )
    rows = await cursor.fetchall()
    if len(rows) > limit:
        raise ValueError("context_storage_limit")
    return [decode_row(row) for row in rows]


async def read_context(cursor, identity, scope):
    row = await read_object(
        cursor,
        "swe_token_contexts",
        "context_id",
        identity,
        scope,
    )
    if row is None:
        return None
    for key, table, order, limit in (
        (
            "occurrences",
            "swe_token_context_occurrences",
            "occurrence_index",
            2000,
        ),
        ("blocks", "swe_token_prefix_blocks", "block_index", 1024),
    ):
        row[key] = await read_children(
            cursor,
            table,
            "context_id",
            identity,
            scope,
            order,
            limit,
        )
    return row


async def read_operation(cursor, identity, scope):
    row = await read_object(
        cursor,
        "swe_token_context_operations",
        "operation_id",
        identity,
        scope,
    )
    if row is None:
        return None
    row["segments"] = await read_children(
        cursor,
        "swe_token_operation_segments",
        "operation_id",
        identity,
        scope,
        "relation_index",
        2000,
    )
    return row


def evaluation_identity(run, evaluation):
    fields = [run["source_id"], run["tenant_id"]] + [
        evaluation[key]
        for key in (
            "object_type",
            "object_id",
            "rule_id",
            "rule_version",
            "threshold_version",
            "input_revision",
            "snapshot_id",
        )
    ]
    return str(uuid5(NAMESPACE_URL, json.dumps(fields, separators=(",", ":"))))


async def append_evaluations(cursor, run, evaluations):
    """Append immutable history; caller owns transaction, branch lock and lease.

    A changed input remains historical. The lease guard is applied by Monitor's
    analysis commit, never by HTTP reads or business capture.
    """
    if len(evaluations) > 2048:
        raise ValueError("evaluation_limit")
    scope = {key: run[key] for key in _SCOPE_FIELDS}
    for evaluation in evaluations:
        validate_evaluation_state(evaluation["state"])
        identity = evaluation_identity(run, evaluation)
        head = await read_head(cursor, scope, evaluation)
        data = {
            **evaluation,
            **scope,
            "evaluation_id": identity,
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "replaces_evaluation_id": head["evaluation_id"] if head else None,
        }
        await cursor.execute(
            "SELECT evaluation_id FROM swe_token_diagnostic_evaluations WHERE evaluation_id=%s AND source_id=%s AND tenant_id=%s",
            (identity, scope["source_id"], scope["tenant_id"]),
        )
        newly_inserted = await cursor.fetchone() is None
        if newly_inserted:
            await insert_record(
                cursor,
                "swe_token_diagnostic_evaluations",
                data,
            )
            await write_evaluation_details(cursor, data)
        if (
            newly_inserted
            and run["data_revision"] == evaluation["input_revision"]
        ):
            await insert_record(
                cursor,
                "swe_token_diagnostic_heads",
                {
                    **data,
                    "updated_at": data["created_at"],
                },
            )


async def read_head(cursor, scope, evaluation):
    await cursor.execute(
        f"SELECT {select_columns('swe_token_diagnostic_heads')} FROM swe_token_diagnostic_heads WHERE source_id=%s AND tenant_id=%s AND object_type=%s AND object_id=%s AND rule_id=%s FOR UPDATE",
        (
            scope["source_id"],
            scope["tenant_id"],
            evaluation["object_type"],
            evaluation["object_id"],
            evaluation["rule_id"],
        ),
    )
    return await cursor.fetchone()


async def write_evaluation_details(cursor, data):
    for value in data["values"]:
        await insert_record(
            cursor,
            "swe_token_diagnostic_values",
            {**data, **value},
        )

    for reason in set(data["gaps"]):
        await insert_record(
            cursor,
            "swe_token_diagnostic_gaps",
            {**data, "reason_code": reason},
        )
    for index, ref in enumerate(data["refs"]):
        await insert_record(
            cursor,
            "swe_token_diagnostic_refs",
            {**data, **ref, "reference_index": index},
        )
    for finding in data["findings"]:
        identity = str(
            uuid5(
                NAMESPACE_URL,
                data["evaluation_id"] + finding["instance_key"],
            ),
        )
        await insert_record(
            cursor,
            "swe_token_diagnostic_findings",
            {
                **data,
                **finding,
                "finding_id": identity,
            },
        )


async def write_prefix_comparison(cursor, data):
    """Keep the complete bounded candidate snapshot immutable on retries."""
    candidates = data.get("candidates", [])
    if len(candidates) > 100 or data["candidate_count"] != len(candidates):
        raise ValueError("prefix_candidate_limit")
    identity = str(
        uuid5(
            NAMESPACE_URL,
            json.dumps(
                [
                    data["source_id"],
                    data["tenant_id"],
                    data["current_context_id"],
                    data["current_context_revision"],
                    data["snapshot_id"],
                ],
            ),
        ),
    )
    scope = {key: data[key] for key in _SCOPE_FIELDS}
    previous = await read_object(
        cursor,
        "swe_token_prefix_comparisons",
        "comparison_id",
        identity,
        (data["source_id"], data["tenant_id"]),
    )
    if previous:
        return identity
    header = {**data, "comparison_id": identity}
    await insert_record(cursor, "swe_token_prefix_comparisons", header)
    for index, candidate in enumerate(candidates):
        await insert_record(
            cursor,
            "swe_token_prefix_candidates",
            {
                **candidate,
                **scope,
                "comparison_id": identity,
                "candidate_index": index,
            },
        )
    return identity
