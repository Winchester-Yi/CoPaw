# -*- coding: utf-8 -*-
"""Scope checked direct recovery body references with bounded reader pins."""

from dataclasses import dataclass, field
import json

from .errors import IntegrityError
from .identity import WorkspaceScope
from .retention import ReaderPins, RetentionStore
from .session_projection import hydrate_state, is_projection
from .projection_cache import active, cache_key, cached, remember


def ref_key(ref):
    return json.dumps(ref, sort_keys=True, separators=(",", ":"))


def references(envelope):
    refs = [
        entry["ref"]
        for series in envelope["lists"]
        for entry in series["entries"]
    ]
    return refs + [item["ref"] for item in envelope["turns"]]


def _text(value):
    return (
        bytes(value).decode()
        if isinstance(value, (bytes, bytearray))
        else value
    )


@dataclass
class ProjectionReferences:
    rows: dict = field(default_factory=dict)
    payloads: dict = field(default_factory=dict)
    publications: dict = field(default_factory=dict)
    batch: object = None
    turns: dict = field(default_factory=dict)

    @property
    def publication_ids(self):
        return sorted(self.publications)

    async def finish(self, retentions):
        if self.batch is not None:
            await self.batch.finish()
            self.batch = None

    async def revalidate(self, db):
        for identity, expected in sorted(self.rows.items()):
            row = await db.fetch_one(
                "SELECT * FROM swe_conversation_messages WHERE id=%s FOR SHARE",
                (identity,),
            )
            if row is None or any(
                row[key] != expected[key]
                for key in (
                    "session_id",
                    "publication_id",
                    "byte_offset",
                    "byte_length",
                    "layer",
                    "source_msg_key",
                    "role_key",
                    "context_epoch",
                    "current_visible",
                    "in_context",
                    "deleted_at",
                )
            ):
                raise IntegrityError(
                    "recovery projection occurrence locator changed",
                )
        for identity, expected in sorted(self.turns.items()):
            row = await db.fetch_one(
                "SELECT * FROM swe_conversation_turns WHERE id=%s",
                (identity,),
            )
            if row is None or any(
                row[key] != expected[key]
                for key in (
                    "session_id",
                    "chat_id",
                    "terminal_publication_id",
                    "history_status",
                    "question_msg_key",
                )
            ):
                raise IntegrityError("cold Turn projection authority changed")


def _same_locator(row, request, ref):
    return (
        row is not None
        and row["session_id"] == request.session_id
        and row["deleted_at"] is None
        and (row["publication_id"], row["byte_offset"], row["byte_length"])
        == (ref["publication_id"], ref["offset"], ref["length"])
    )


async def _snapshot_rows(service, request, refs, snapshot):
    for ref in refs:
        if "message_id" not in ref:
            if "turn_id" in ref:
                turn = await service.db.fetch_one(
                    "SELECT * FROM swe_conversation_turns WHERE id=%s",
                    (ref["turn_id"],),
                )
                if (
                    turn is None
                    or turn["session_id"] != request.session_id
                    or turn["terminal_publication_id"] != ref["publication_id"]
                ):
                    raise IntegrityError(
                        "cold Turn reference association differs",
                    )
                snapshot.turns[turn["id"]] = turn
            continue
        row = await service.db.fetch_one(
            "SELECT * FROM swe_conversation_messages WHERE id=%s",
            (ref["message_id"],),
        )
        if not _same_locator(row, request, ref):
            raise IntegrityError(
                "recovery projection reference does not match Session locator",
            )
        snapshot.rows[row["id"]] = row


def _cached_bodies(request, envelope):
    cached_values = cached(
        (
            *(request.scope.sql_values),
            request.session_id,
            envelope.get("identity", {}).get("base_state_revision"),
        ),
    )
    cached_bodies = {}
    if cached_values is not None:
        for (_, body), values in cached_values.items():
            for value in values:
                cached_bodies[ref_key(value)] = json.loads(body)
    return cached_bodies


async def snapshot_projection(
    service,
    request,
    envelope,
    index,
    *,
    current_id,
    retentions=None,
):
    snapshot = ProjectionReferences()
    if envelope is None:
        return snapshot
    refs = references(envelope)
    cached_bodies = _cached_bodies(request, envelope)
    retentions = retentions or RetentionStore(service.db, request.scope)
    snapshot.batch = ReaderPins(retentions)
    try:
        async with service.db.transaction():
            await _snapshot_rows(service, request, refs, snapshot)
            ids = sorted(
                {
                    ref["publication_id"]
                    for ref in refs
                    if "publication_id" in ref
                },
            )
            if any(identity >= current_id for identity in ids):
                raise IntegrityError(
                    "recovery dependencies must point to older publications",
                )
            if ids:
                await snapshot.batch.pin(ids)
                snapshot.publications = {
                    row["id"]: row for row in snapshot.batch.publications
                }
        for ref in refs:
            if ref_key(ref) in cached_bodies:
                snapshot.payloads[ref_key(ref)] = cached_bodies[ref_key(ref)]
                continue
            if "publication_id" not in ref:
                extent = index.blocks.get(ref["block_name"])
                raw = await service._nas_io(
                    request.operation_key,
                    service.bundles.read_extent,
                    index.relative_path,
                    extent.offset,
                    extent.length,
                    expected_length=index.byte_length,
                )
            else:
                publication = snapshot.publications[ref["publication_id"]]
                if publication["session_id"] != request.session_id:
                    raise IntegrityError(
                        "recovery projection dependency Session differs",
                    )
                if "message_id" in ref:
                    offset, length = ref["offset"], ref["length"]
                else:
                    prior = await snapshot.batch.read(
                        service.bundles.inspect,
                        _text(publication["relative_path"]),
                        expected_length=publication["byte_length"],
                    )
                    block = prior.blocks.get(ref["block_name"])
                    if block is None or block.kind != "turn":
                        raise IntegrityError(
                            "cold Turn must reference an independent Turn body",
                        )
                    offset, length = block.offset, block.length
                raw = await snapshot.batch.read(
                    service.bundles.read_extent,
                    _text(publication["relative_path"]),
                    offset,
                    length,
                    expected_length=publication["byte_length"],
                )
            snapshot.payloads[ref_key(ref)] = json.loads(raw)
            if "message_id" in ref:
                value = snapshot.payloads[ref_key(ref)]
                row = snapshot.rows[ref["message_id"]]
                if (
                    not isinstance(value, dict)
                    or value.get("id") != _text(row["source_msg_key"])
                    or (value.get("role") or "assistant")
                    != _text(row["role_key"])
                ):
                    raise IntegrityError(
                        "recovery projection body identity mismatch",
                    )
            elif "turn_id" in ref:
                turn = snapshot.turns[ref["turn_id"]]
                if (
                    ref["block_name"]
                    != "turn:" + _text(turn["question_msg_key"])
                    or snapshot.payloads[ref_key(ref)].get("status")
                    != turn["history_status"]
                ):
                    raise IntegrityError(
                        "cold Turn independent body identity differs",
                    )
        hydrate_state(envelope, lambda ref: snapshot.payloads[ref_key(ref)])
        return snapshot
    except BaseException:
        await snapshot.finish(retentions)
        raise


async def hydrate_projection(adapter, envelope, session, publication, batch):
    if not is_projection(envelope):
        return envelope
    from types import SimpleNamespace

    refs = references(envelope)
    source = SimpleNamespace(
        operation_key=_text(publication["operation_key"]),
        scope=adapter.scope,
        session_id=session["id"],
    )
    index = await batch.read(
        adapter.bundles.inspect,
        _text(publication["relative_path"]),
        expected_length=publication["byte_length"],
    )
    snapshot = await snapshot_projection(
        adapter.publications,
        source,
        envelope,
        index,
        current_id=publication["id"],
        retentions=adapter.retentions,
    )
    try:
        return hydrate_state(
            envelope,
            lambda ref: snapshot.payloads[ref_key(ref)],
        )
    finally:
        await snapshot.finish(adapter.retentions)


async def refresh_projection_cache(service, row, envelope, snapshot):
    if envelope is None or not active():
        return
    from collections import defaultdict
    from .projection_publication import canonical

    values = defaultdict(list)
    index = None
    for series in envelope["lists"]:
        layer = (
            "memory"
            if len(series["path"]) > 1
            else "task" if series["path"][0] == "task_messages" else "error"
        )
        for entry in series["entries"]:
            ref = entry["ref"]
            payload = snapshot.payloads[ref_key(ref)]
            if "message_id" in ref:
                resolved = ref
            else:
                if index is None:
                    index = await service._nas_io(
                        _text(row["operation_key"]),
                        service.bundles.inspect,
                        _text(row["relative_path"]),
                        expected_length=row["byte_length"],
                    )
                extent = index.blocks[ref["block_name"]]
                message = await service.db.fetch_one(
                    "SELECT * FROM swe_conversation_messages WHERE publication_id=%s AND byte_offset=%s AND byte_length=%s AND session_id=%s ORDER BY id LIMIT 1",
                    (
                        row["id"],
                        extent.offset,
                        extent.length,
                        row["session_id"],
                    ),
                )
                if message is None:
                    raise IntegrityError(
                        "committed projection delta occurrence is missing",
                    )
                resolved = {
                    "publication_id": row["id"],
                    "message_id": message["id"],
                    "offset": extent.offset,
                    "length": extent.length,
                }
            values[(layer, canonical(payload))].append(resolved)
    remember(cache_key(row, row["base_state_revision"] + 1), values)
