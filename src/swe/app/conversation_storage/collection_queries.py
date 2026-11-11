# -*- coding: utf-8 -*-
"""Fixed collection statements with bounded request bytes and task-owned leases."""

from enum import Enum

from .errors import StorageUnavailable
from .reference_queries import MAX_QUERY_BYTES, MAX_REFERENCE_IDS

_SCOPE = (
    "service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s"
)
_BOUNDARY_FIELDS = (
    "id,service_unit_key,chat_id,boundary_key,boundary_order,archive_display_count,"
    "archive_first_source_msg_key,archive_last_source_msg_key,committed_at,"
    "archive_first_timestamp,archive_last_timestamp,archive_last_order"
)


class CollectionQuery(Enum):
    """SQL identifiers, predicates and lock types are application-owned."""

    ARCHIVED_KEYS = (
        "SELECT DISTINCT source_msg_key FROM swe_conversation_messages "
        "WHERE service_unit_key=%s AND archive_chat_id=%s AND deleted_at IS NULL "
        "AND source_msg_key IN (",
        ")",
        1024,
    )
    BOUNDARIES = (
        f"SELECT {_BOUNDARY_FIELDS} FROM swe_conversation_publications "
        "WHERE service_unit_key=%s AND chat_id=%s AND state='committed' "
        "AND boundary_key IS NOT NULL AND id IN (",
        ") ORDER BY boundary_order",
        None,
    )
    DELETE_CANDIDATES = (
        f"SELECT id,session_id FROM swe_conversation_chats WHERE {_SCOPE} "
        "AND external_chat_key IN (",
        ") AND deleted_at IS NULL ORDER BY id",
        128,
    )
    SESSION_LOCK = (
        "SELECT id FROM swe_conversation_sessions WHERE service_unit_key=%s AND id IN (",
        ") ORDER BY id FOR UPDATE",
        None,
    )
    CHAT_LOCK = (
        "SELECT id,session_id,external_chat_key,external_session_key,user_key,channel_key FROM swe_conversation_chats WHERE service_unit_key=%s AND id IN (",
        ") ORDER BY id FOR UPDATE",
        None,
    )
    DELETE_CHATS = (
        "UPDATE swe_conversation_chats SET deleted_at=UTC_TIMESTAMP(6),"
        f"metadata_revision=metadata_revision+1 WHERE {_SCOPE} AND id IN (",
        ") AND deleted_at IS NULL",
        None,
    )
    SHARE_TURNS = (
        "SELECT id,service_unit_key,session_id,chat_id,question_msg_key,state,history_status "
        "FROM swe_conversation_turns WHERE service_unit_key=%s AND session_id=%s "
        "AND question_msg_key IN (",
        ") ORDER BY id FOR UPDATE",
        256,
    )
    SHARE_OCCURRENCES = (
        "SELECT m.* FROM swe_conversation_messages m WHERE m.service_unit_key=%s AND ("
        "(m.archive_chat_id=%s AND m.deleted_at IS NULL AND m.archive_order<=%s) OR ("
        "m.session_id=%s AND m.current_visible=1 AND m.deleted_at IS NULL "
        "AND (m.layer IN ('memory','task','error') OR (m.layer='anchor' "
        "AND (m.origin_chat_id=%s OR m.origin_chat_id IS NULL))) "
        "AND m.session_order<=%s)) AND m.source_msg_key IN (",
        ") ORDER BY m.archive_order,m.session_order,m.id",
        256,
    )
    PRUNE = (
        "UPDATE swe_conversation_messages SET in_context=0,current_visible=0 "
        "WHERE service_unit_key=%s AND id IN (",
        ")",
        None,
    )


def _bound(value):
    if isinstance(value, bytes):
        return 2 * len(value) + 3
    if type(value) is int:
        return len(str(value)) + 1
    raise ValueError("collection parameters must be bytes or integers")


def _batches(query, values, params, packet):
    prefix, suffix, key_limit = query.value
    keys = list(values)
    if key_limit is None:
        valid = all(type(key) is int and 0 < key < 2**64 for key in keys)
    else:
        valid = all(
            isinstance(key, bytes) and len(key) <= key_limit for key in keys
        )
    if not valid:
        raise ValueError("invalid collection identity")
    keys = sorted(set(keys))
    budget = min(MAX_QUERY_BYTES, max(0, min(packet - 1024, packet * 9 // 10)))
    base = (
        len((prefix + suffix).encode())
        + sum(_bound(value) for value in params)
        + 32
    )
    # Validate every singleton before the first statement, including mutations.
    if any(base + _bound(key) > budget for key in keys):
        raise StorageUnavailable(
            "Collection query exceeds max_allowed_packet budget",
        )
    batch = []
    size = base
    for key in keys:
        cost = _bound(key)
        if batch and (len(batch) >= MAX_REFERENCE_IDS or size + cost > budget):
            yield batch
            batch, size = [], base
        batch.append(key)
        size += cost
    if batch:
        yield batch


async def _run(database, query, values, params, *, mutation):
    if (
        not isinstance(query, CollectionQuery)
        or query.value[0].startswith("UPDATE ") != mutation
    ):
        raise ValueError("invalid collection statement kind")
    values = list(values)
    if not values:
        return 0 if mutation else []
    prefix, suffix, _limit = query.value
    if prefix.count("%s") != len(params):
        raise ValueError("invalid collection scope parameters")
    result = 0 if mutation else []
    async with database.acquire() as session:
        if mutation and not session._in_transaction:
            raise RuntimeError(
                "collection updates require the caller transaction",
            )
        for batch in _batches(
            query,
            values,
            params,
            session.max_allowed_packet,
        ):
            sql = prefix + ",".join(["%s"] * len(batch)) + suffix
            arguments = (*params, *batch)
            if mutation:
                result += await session.execute(sql, arguments)
            else:
                result.extend(await session.fetch_all(sql, arguments))
    return result


async def query_collection(database, query, values, params):
    """Read all requested keys; callers retain ordering and authority checks."""
    return await _run(database, query, values, params, mutation=False)


async def update_collection(database, query, values, params):
    """Execute batches in the caller's transaction, returning changed rows."""
    return await _run(database, query, values, params, mutation=True)
