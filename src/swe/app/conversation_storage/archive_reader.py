# -*- coding: utf-8 -*-
"""Bounded indexed checkpoint and evidence reads with explicit reader pins."""

from ...agents.memory.conversation_archive import (
    ConversationArchiveStore,
)
from .bundle import BundleStore
from .chat_mutations import load_metadata_json
from .checkpoint_reader import CheckpointReads
from .errors import IntegrityError
from .identity import chat_key_bytes
from .retention import RetentionStore


class EvidenceSelectors:
    """Reuse the existing pure evidence matching contract without a file store."""

    _parse_evidence_time_range = staticmethod(
        ConversationArchiveStore._parse_evidence_time_range,
    )
    _message_in_time_range = staticmethod(
        ConversationArchiveStore._message_in_time_range,
    )
    prepare = ConversationArchiveStore._prepare_evidence_query
    matches = ConversationArchiveStore._matches_evidence_query


def _decode(cls, payload):
    try:
        return cls.from_dict(payload)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise IntegrityError(
            "indexed archive body schema is invalid",
        ) from error


def text(value):
    return (
        bytes(value).decode()
        if isinstance(value, (bytes, bytearray))
        else value
    )


class ArchiveReader(CheckpointReads):
    def __init__(self, database, scope, bundles):
        self.db, self.scope, self.bundles = database, scope, bundles
        self.retentions = RetentionStore(database, scope)

    async def chat(self, chat_id):
        row = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_chats WHERE service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s AND external_chat_key=%s AND deleted_at IS NULL",
            (*self.scope.sql_values, chat_key_bytes(chat_id)),
        )
        if row is None:
            raise KeyError(chat_id)
        return row

    async def payload(self, batch, publication, prefix):
        raw = await batch.read(
            self.bundles.read_extent,
            BundleStore.validate_publication_locator(publication),
            publication[prefix + "_offset"],
            publication[prefix + "_length"],
            expected_length=publication["byte_length"],
        )
        return load_metadata_json(raw)

    async def evidence(
        self,
        chat_id,
        *,
        epoch,
        refs,
        query=None,
        kinds=None,
        time_range=None,
        limit=10,
    ):
        from agentscope.message import Msg

        if limit <= 0:
            return []
        selector = EvidenceSelectors()
        normalized = selector.prepare(
            refs,
            query,
            kinds,
            time_range,
        )
        if not normalized.requested and not normalized.semantic_query:
            return []
        chat = await self.chat(chat_id)
        if chat["epoch"] != epoch:
            return []
        recovered, after = [], 0
        while len(recovered) < min(limit, 10):
            batch = None
            async with self.db.transaction():
                rows = await self.db.fetch_all(
                    "SELECT m.* FROM swe_conversation_messages m JOIN swe_conversation_publications a ON a.id=m.archive_publication_id WHERE m.archive_chat_id=%s AND m.deleted_at IS NULL AND a.epoch=%s AND a.state='committed' AND m.archive_order>%s ORDER BY m.archive_order LIMIT 64",
                    (chat["id"], epoch, after),
                )
                if not rows:
                    break
                batch = await self.retentions.pin(
                    [row["publication_id"] for row in rows],
                    kind="reader",
                )
            try:
                publications = {row["id"]: row for row in batch.publications}
                for row in rows:
                    publication = publications[row["publication_id"]]
                    if (
                        row["session_id"] != chat["session_id"]
                        or publication["session_id"] != chat["session_id"]
                    ):
                        raise IntegrityError(
                            "evidence Session association mismatch",
                        )
                    raw = await batch.read(
                        self.bundles.read_extent,
                        text(publication["relative_path"]),
                        row["byte_offset"],
                        row["byte_length"],
                        expected_length=publication["byte_length"],
                    )
                    payload = load_metadata_json(raw)
                    msg = _decode(Msg, payload)
                    if msg.id != text(
                        row["source_msg_key"],
                    ) or msg.role != text(row["role_key"]):
                        raise IntegrityError("evidence body identity mismatch")
                    if selector.matches(
                        msg,
                        requested=normalized.requested,
                        semantic_query=normalized.semantic_query,
                        kind_filter=normalized.kind_filter,
                        time_bounds=normalized.time_bounds,
                    ):
                        recovered.append(msg)
                        if len(recovered) >= min(limit, 10):
                            break
                after = rows[-1]["archive_order"]
            finally:
                await self.retentions.finish_reader(batch)
        current = await self.chat(chat_id)
        if (
            current["epoch"] != epoch
            or current["session_id"] != chat["session_id"]
        ):
            return []
        return recovered
