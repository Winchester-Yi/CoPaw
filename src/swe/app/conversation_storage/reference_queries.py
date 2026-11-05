# -*- coding: utf-8 -*-
"""Fixed, bounded reference queries; callers retain transaction authority."""

from enum import Enum

from .errors import IntegrityError, StorageUnavailable

MAX_REFERENCE_IDS = 256
MAX_QUERY_BYTES = 64 * 1024

MESSAGE_LOCATOR_FIELDS = (
    "id",
    "service_unit_key",
    "session_id",
    "origin_chat_id",
    "turn_id",
    "source_msg_key",
    "source_occurrence_no",
    "role_key",
    "kind_key",
    "task_run_key",
    "publication_id",
    "byte_offset",
    "byte_length",
    "context_epoch",
    "message_timestamp",
    "session_order",
    "layer",
    "current_visible",
    "in_context",
    "deleted_at",
)
TURN_REFERENCE_FIELDS = (
    "id",
    "service_unit_key",
    "session_id",
    "chat_id",
    "terminal_publication_id",
    "history_status",
    "question_msg_key",
)


PUBLICATION_LOCK_FIELDS = (
    "id",
    "service_unit_key",
    "scope_kind",
    "tenant_key",
    "source_key",
    "agent_key",
    "session_id",
    "chat_id",
    "turn_id",
    "kind",
    "operation_key",
    "state",
    "gc_state",
    "relative_path",
    "byte_length",
)


CHAT_CHECK_FIELDS = (
    "id",
    "service_unit_key",
    "tenant_key",
    "source_key",
    "agent_key",
    "session_id",
    "metadata_revision",
    "expert_binding_revision",
    "content_meta_publication_id",
    "content_meta_offset",
    "content_meta_length",
    "user_key",
    "channel_key",
    "deleted_at",
)


class ReferenceQuery(Enum):
    """Only trusted table, column and lock shapes can reach the helper."""

    MESSAGE_PRUNE = (
        "messages",
        ("id", "service_unit_key", "session_id", "layer", "context_epoch"),
        " FOR UPDATE",
    )
    MESSAGE_PROOF = (
        "messages",
        (*MESSAGE_LOCATOR_FIELDS, "archive_chat_id", "archive_order"),
        " FOR UPDATE",
    )
    PUBLICATION_LOCK = ("publications", PUBLICATION_LOCK_FIELDS, " FOR UPDATE")
    CHAT_CHECK = ("chats", CHAT_CHECK_FIELDS, "")
    MESSAGE_LOCATOR = ("messages", MESSAGE_LOCATOR_FIELDS, "")
    MESSAGE_CURRENT = ("messages", MESSAGE_LOCATOR_FIELDS, " FOR SHARE")
    MESSAGE_OWNERSHIP = (
        "messages",
        ("id", "service_unit_key", "session_id", "publication_id"),
        "",
    )
    TURN_REFERENCE = ("turns", TURN_REFERENCE_FIELDS, "")
    PUBLICATION_OWNERSHIP = (
        "publications",
        (
            "id",
            "service_unit_key",
            "scope_kind",
            "tenant_key",
            "source_key",
            "agent_key",
            "session_id",
        ),
        "",
    )


def _query_shape(query_kind, ids, service_unit_key):
    if not isinstance(query_kind, ReferenceQuery):
        raise ValueError("Unknown reference query kind")
    identities = list(ids)
    if any(
        type(identity) is not int or not 0 < identity < 2**64
        for identity in identities
    ):
        raise ValueError(
            "Reference IDs must be positive unsigned bigint values",
        )
    if (
        not isinstance(service_unit_key, bytes)
        or not 1 <= len(service_unit_key) <= 256
    ):
        raise ValueError("Reference service key must be 1..256 bytes")
    table, columns, lock = query_kind.value
    prefix = (
        f"SELECT {','.join(columns)} FROM swe_conversation_{table} "
        "WHERE service_unit_key=%s AND id IN ("
    )
    return sorted(set(identities)), prefix, ") ORDER BY id" + lock


def _batch_size(session, prefix, suffix, service_unit_key):
    packet = session.max_allowed_packet
    budget = min(MAX_QUERY_BYTES, max(0, min(packet - 1024, packet * 9 // 10)))
    # Text protocol expands each uint64 to at most 20 digits plus a comma.
    # Binary scope may double when escaped; include quotes and command framing.
    base_bytes = (
        len((prefix + suffix).encode("utf-8")) + 2 * len(service_unit_key) + 32
    )
    size = min(MAX_REFERENCE_IDS, (budget - base_bytes) // 21)
    if size < 1:
        raise StorageUnavailable(
            "Reference query exceeds max_allowed_packet budget",
        )
    return size


async def iter_reference_rows(database, query_kind, ids, service_unit_key):
    """Yield requested IDs and narrow rows per batch on one task-owned lease.

    Consumers that can exit early must close this iterator before leaving their
    SQL stage. This helper does not start transactions or perform NAS IO.
    """
    identities, prefix, suffix = _query_shape(
        query_kind,
        ids,
        service_unit_key,
    )
    if not identities:
        return
    async with database.acquire() as session:
        size = _batch_size(session, prefix, suffix, service_unit_key)
        for start in range(0, len(identities), size):
            batch = identities[start : start + size]
            requested = set(batch)
            query = prefix + ",".join(["%s"] * len(batch)) + suffix
            result = {}
            for row in await session.fetch_all(
                query,
                (service_unit_key, *batch),
            ):
                identity = row["id"]
                if (
                    type(identity) is not int
                    or identity not in requested
                    or identity in result
                    or row["service_unit_key"] != service_unit_key
                ):
                    raise IntegrityError(
                        "Reference query returned invalid IDs or service",
                    )
                result[identity] = row
            yield batch, result


async def fetch_reference_rows(database, query_kind, ids, service_unit_key):
    """Return all narrow rows; the caller checks required IDs and authority."""
    result = {}
    async for _batch, rows in iter_reference_rows(
        database,
        query_kind,
        ids,
        service_unit_key,
    ):
        result.update(rows)
    return result
