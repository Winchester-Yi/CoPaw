# -*- coding: utf-8 -*-
"""Atomic typed context/operation writes within the Token batch transaction."""

from .context_contract import CONTEXT_MAX_BLOCKS, CONTEXT_MAX_ITEMS
from .store import insert_record


async def replace_children(cursor, data, table, identity, values):
    await cursor.execute(
        f"DELETE FROM {table} WHERE {identity}=%s AND source_id=%s AND tenant_id=%s",
        (data[identity], data["source_id"], data["tenant_id"]),
    )
    for value in values:
        await insert_record(cursor, table, {**data, **value})


def validate_children(data, children, identity, index_column):
    for index, child in enumerate(children):
        if child.get(index_column) != index or any(
            child.get(key, data[key]) != data[key]
            for key in (
                identity,
                "source_id",
                "tenant_id",
                "task_run_id",
                "branch_run_id",
            )
        ):
            raise ValueError("context_child_contract")


async def write_context(cursor, data):
    occurrences, blocks = data.get("occurrences", []), data.get("blocks", [])
    for children, index_column in (
        (occurrences, "occurrence_index"),
        (blocks, "block_index"),
    ):
        validate_children(data, children, "context_id", index_column)
    if (
        len(occurrences) > CONTEXT_MAX_ITEMS
        or len(blocks) > CONTEXT_MAX_BLOCKS
    ):
        raise ValueError("context_child_limit")
    if data["capture_state"] == "complete" and (
        data.get("expected_item_count") != len(occurrences)
        or data.get("encoded_bytes")
        != (blocks[-1]["encoded_end"] if blocks else 0)
    ):
        raise ValueError("context_incomplete_children")
    await insert_record(cursor, "swe_token_contexts", data)
    await replace_children(
        cursor,
        data,
        "swe_token_context_occurrences",
        "context_id",
        occurrences,
    )
    await replace_children(
        cursor,
        data,
        "swe_token_prefix_blocks",
        "context_id",
        blocks,
    )


async def write_operation(cursor, data):
    segments = data.get("segments", [])
    if len(segments) > CONTEXT_MAX_ITEMS:
        raise ValueError("operation_segment_limit")
    validate_children(data, segments, "operation_id", "relation_index")
    await insert_record(cursor, "swe_token_context_operations", data)
    await replace_children(
        cursor,
        data,
        "swe_token_operation_segments",
        "operation_id",
        segments,
    )
