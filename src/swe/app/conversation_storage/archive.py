# -*- coding: utf-8 -*-
"""Atomic archive/checkpoint/event/reset metadata installed by publication commit."""

from dataclasses import asdict, dataclass, fields

from .chat_mutations import utc_datetime
from .errors import ConflictError, IntegrityError
from .identity import identity_bytes


def _number(value, *, positive=False):
    if type(value) is not int or value < int(positive):
        raise ValueError("archive metadata integer is out of bounds")


def _block(value):
    identity_bytes(value, 256)
    if not value:
        raise ValueError("archive block must be nonempty")


class _Plan:
    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {
            item.name for item in fields(cls)
        }:
            raise ValueError("unknown archive installation field")
        return cls(**value)


@dataclass(frozen=True)
class ArchiveInstall(_Plan):
    boundary_key: str
    block_name: str
    source_count: int
    display_count: int
    first_source_msg_key: str
    last_source_msg_key: str
    first_timestamp: str | None = None
    last_timestamp: str | None = None

    def __post_init__(self):
        identity_bytes(self.boundary_key, 128)
        _block(self.block_name)
        _number(self.source_count, positive=True)
        _number(self.display_count, positive=True)
        for value in (self.first_source_msg_key, self.last_source_msg_key):
            identity_bytes(value, 1024)
        for value in (self.first_timestamp, self.last_timestamp):
            if value is not None:
                utc_datetime(value)


@dataclass(frozen=True)
class CheckpointInstall(_Plan):
    block_name: str
    base_revision: int
    revision: int
    event_watermark: int
    source_high: int

    def __post_init__(self):
        _block(self.block_name)
        for value in (
            self.base_revision,
            self.revision,
            self.event_watermark,
            self.source_high,
        ):
            _number(value)
        if self.revision != self.base_revision + 1:
            raise ValueError("checkpoint revision must advance by one")


@dataclass(frozen=True)
class EventInstall(_Plan):
    block_name: str
    first: int
    last: int

    def __post_init__(self):
        _block(self.block_name)
        _number(self.first, positive=True)
        _number(self.last, positive=True)
        if self.last < self.first:
            raise ValueError("event range must be ordered")


@dataclass(frozen=True)
class ContextReset(_Plan):
    base_context_epoch: int
    base_checkpoint_epoch: int

    def __post_init__(self):
        _number(self.base_context_epoch)
        _number(self.base_checkpoint_epoch)


def _extent(index, block_name, kind):
    extent = index.blocks.get(block_name)
    if extent is None or extent.kind != kind:
        raise IntegrityError(
            "archive installation block is missing or invalid",
        )
    return extent


def _validate_epoch(request, session, chat, reset):
    if chat is None or session is None or request.epoch != chat["epoch"]:
        raise ConflictError("archive checkpoint epoch changed")
    if reset is not None and (
        reset.base_context_epoch != session["context_epoch"]
        or reset.base_checkpoint_epoch != chat["epoch"]
    ):
        raise ConflictError("reset epoch changed")


async def install_archive(
    db,
    request,
    publication,
    index,
    associations,
    *,
    archive=None,
    checkpoint=None,
    events=None,
    reset=None,
    prune_ids=(),
):
    session, chat, _ = associations
    if (
        archive is None
        and checkpoint is None
        and events is None
        and reset is None
    ):
        if session is None:
            raise IntegrityError("Session pruning requires a locked Session")
        await _prune(
            db,
            request,
            prune_ids,
            context_epoch=session["context_epoch"],
        )
        return
    _validate_epoch(request, session, chat, reset)
    if reset is not None:
        if any(value is not None for value in (archive, checkpoint, events)):
            raise IntegrityError(
                "context reset cannot publish a compact boundary",
            )
        await _reset(db, request, session, chat)
        return
    values = {}
    event_high = chat["event_high"]
    if events is not None:
        if events.first != event_high + 1:
            raise ConflictError("event range must be continuous")
        extent = _extent(index, events.block_name, "events")
        values.update(
            event_first=events.first,
            event_last=events.last,
            event_offset=extent.offset,
            event_length=extent.length,
        )
        event_high = events.last
    if checkpoint is not None:
        values.update(
            _checkpoint_values(checkpoint, chat, session, index, event_high),
        )
    if archive is not None:
        values.update(_archive_values(archive, chat, index))
    if values:
        values["retained_history"] = 1
        await db.execute(
            "UPDATE swe_conversation_publications SET "
            + ",".join(f"{key}=%s" for key in values)
            + " WHERE id=%s",
            (*values.values(), publication["id"]),
        )
    if events is not None:
        await db.execute(
            "UPDATE swe_conversation_chats SET event_high=%s WHERE id=%s",
            (event_high, request.chat_id),
        )
    if checkpoint is not None:
        await db.execute(
            "UPDATE swe_conversation_chats SET active_checkpoint_publication_id=%s,checkpoint_revision=%s WHERE id=%s",
            (publication["id"], checkpoint.revision, request.chat_id),
        )
    if archive is not None:
        await db.execute(
            "UPDATE swe_conversation_chats SET next_boundary_order=next_boundary_order+1 WHERE id=%s",
            (request.chat_id,),
        )
    await _prune(db, request, prune_ids)


def _checkpoint_values(plan, chat, session, index, event_high):
    if plan.base_revision != chat["checkpoint_revision"]:
        raise ConflictError("checkpoint revision changed")
    if (
        plan.event_watermark > event_high
        or plan.source_high > session["history_high"]
    ):
        raise IntegrityError("checkpoint watermark exceeds committed prefix")
    extent = _extent(index, plan.block_name, "checkpoint")
    return {
        "checkpoint_base_revision": plan.base_revision,
        "checkpoint_revision": plan.revision,
        "checkpoint_event_watermark": plan.event_watermark,
        "checkpoint_source_high": plan.source_high,
        "checkpoint_offset": extent.offset,
        "checkpoint_length": extent.length,
    }


def _archive_values(plan, chat, index):
    extent = _extent(index, plan.block_name, "archive")
    return {
        "boundary_key": identity_bytes(plan.boundary_key, 128),
        "boundary_order": chat["next_boundary_order"],
        "archive_first_order": chat["history_high"] + 1,
        "archive_last_order": chat["history_high"] + plan.source_count,
        "archive_source_count": plan.source_count,
        "archive_display_count": plan.display_count,
        "archive_first_source_msg_key": identity_bytes(
            plan.first_source_msg_key,
            1024,
        ),
        "archive_last_source_msg_key": identity_bytes(
            plan.last_source_msg_key,
            1024,
        ),
        "archive_first_timestamp": (
            utc_datetime(plan.first_timestamp)
            if plan.first_timestamp
            else None
        ),
        "archive_last_timestamp": (
            utc_datetime(plan.last_timestamp) if plan.last_timestamp else None
        ),
        "archive_offset": extent.offset,
        "archive_length": extent.length,
    }


async def _prune(db, request, prune_ids, *, context_epoch=None):
    if not prune_ids:
        return
    if len(prune_ids) > 4096 or any(
        type(value) is not int or value <= 0 for value in prune_ids
    ):
        raise ValueError("prune IDs must be a bounded positive set")
    prune_ids = tuple(sorted(set(prune_ids)))
    placeholders = ",".join(["%s"] * len(prune_ids))
    rows = await db.fetch_all(
        f"SELECT id,session_id,layer,context_epoch FROM swe_conversation_messages WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE",
        tuple(sorted(set(prune_ids))),
    )
    if len(rows) != len(set(prune_ids)) or any(
        row["session_id"] != request.session_id
        or row["layer"] == "archive"
        or (
            context_epoch is not None and row["context_epoch"] != context_epoch
        )
        for row in rows
    ):
        raise IntegrityError(
            "pruned occurrence does not belong to current Session",
        )
    await db.execute(
        f"UPDATE swe_conversation_messages SET in_context=0,current_visible=0 WHERE id IN ({placeholders})",
        tuple(sorted(set(prune_ids))),
    )


async def _reset(db, request, session, chat):
    await db.execute(
        "UPDATE swe_conversation_messages SET in_context=0,current_visible=0 WHERE session_id=%s AND layer<>'archive'",
        (request.session_id,),
    )
    await db.execute(
        "UPDATE swe_conversation_sessions SET context_epoch=context_epoch+1,context_usage_invalid=1 WHERE id=%s",
        (request.session_id,),
    )
    await db.execute(
        "UPDATE swe_conversation_chats SET epoch=epoch+1,event_high=0,checkpoint_revision=0,active_checkpoint_publication_id=NULL,history_revision=history_revision+1 WHERE id=%s",
        (request.chat_id,),
    )
