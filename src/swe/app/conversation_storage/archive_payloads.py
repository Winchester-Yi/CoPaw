# -*- coding: utf-8 -*-
"""Validate immutable archive payloads before publication SQL locks."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from agentscope.message import Msg

from ...agents.memory.chat_checkpoint import (
    CheckpointEvent,
    CheckpointRecord,
    validate_checkpoint_record,
)
from .chat_mutations import load_metadata_json
from .errors import IntegrityError
from .identity import identity_bytes


@dataclass(frozen=True)
class ArchivePayloads:
    checkpoint: CheckpointRecord | None = None

    def validate_chat(self, chat):
        if (
            self.checkpoint is not None
            and identity_bytes(self.checkpoint.chat_id, 256)
            != chat["external_chat_key"]
        ):
            raise IntegrityError("checkpoint body Chat identity mismatch")


def validate_archive_plan(plan):
    archived = [item for item in plan.messages if item.layer == "archive"]
    if archived and plan.archive is None:
        raise ValueError("archive occurrences require a committed boundary")
    if plan.archive is not None and len(archived) != plan.archive.source_count:
        raise ValueError(
            "archive boundary source count differs from occurrences",
        )
    if plan.archive is not None and (
        archived[0].source_msg_key != plan.archive.first_source_msg_key
        or archived[-1].source_msg_key != plan.archive.last_source_msg_key
    ):
        raise ValueError(
            "archive boundary source identities differ from occurrences",
        )


def validate_request_plan(request, plan):
    validate_archive_plan(plan)
    if request.kind == "compact" and plan.recovery_block is None:
        raise ValueError("compact requires a complete recovery root")


def _stamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("archive timestamp must be a string")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _checkpoint(value, request, plan):
    record = CheckpointRecord.from_dict(value)
    if not validate_checkpoint_record(record).is_valid or (
        record.epoch != request.epoch
        or record.revision != plan.revision
        or record.source_revision != plan.base_revision
        or record.applied_event_sequence != plan.event_watermark
    ):
        raise ValueError("checkpoint body differs from installation metadata")
    return record


def _events(value, request, plan):
    if not isinstance(value, list) or len(value) != plan.last - plan.first + 1:
        raise ValueError("event body count differs from declared range")
    events = tuple(CheckpointEvent.from_dict(item) for item in value)
    if len({item.id for item in events}) != len(events) or any(
        not item.id or item.sequence != plan.first + index
        for index, item in enumerate(events)
    ):
        raise ValueError("event identities or sequence range are invalid")
    record = replace(
        CheckpointRecord.new(chat_id="validation", epoch=request.epoch),
        applied_event_sequence=plan.first - 1,
    )
    if not validate_checkpoint_record(record, events).is_valid:
        raise ValueError("event body epoch or facts are invalid")


def _archive(value, plan, payloads):
    from ..runner.utils import agentscope_msg_to_message

    archived = [item for item in plan.messages if item.layer == "archive"]
    originals = [payloads[item.block_name] for item in archived]
    source_ids = [item.source_message_id for item in archived]
    expected = (
        {"version": 1, "source_message_ids": source_ids}
        if all(identity is not None for identity in source_ids)
        else originals
    )
    if value != expected:
        raise ValueError("archive batch differs from its ordered occurrences")
    count = 0
    for item, raw in zip(archived, originals):
        if not isinstance(raw, dict) or (
            raw.get("id") != item.source_msg_key
            or raw.get("role") != item.role
            or _stamp(raw.get("timestamp")) != _stamp(item.message_timestamp)
        ):
            raise ValueError(
                "archive occurrence body identity differs from metadata",
            )
        count += len(agentscope_msg_to_message(Msg.from_dict(raw)))
    boundary = plan.archive
    if (
        count != boundary.display_count
        or _stamp(originals[0].get("timestamp"))
        != _stamp(boundary.first_timestamp)
        or _stamp(originals[-1].get("timestamp"))
        != _stamp(boundary.last_timestamp)
    ):
        raise ValueError("archive boundary display count or timestamps differ")


async def validate_archive_payloads(
    service,
    request,
    plan,
    index,
    *,
    sources=None,
):
    specs = {}
    for item, kind in (
        (plan.archive, "archive"),
        (plan.checkpoint, "checkpoint"),
        (plan.events, "events"),
    ):
        if item is not None:
            specs[item.block_name] = kind
    if plan.archive is not None:
        specs.update(
            (item.block_name, "archive")
            for item in plan.messages
            if item.layer == "archive" and item.source_message_id is None
        )
    payloads = dict(sources.payloads) if sources is not None else {}
    try:
        for name, kind in specs.items():
            extent = index.blocks.get(name)
            if extent is None or extent.kind != kind:
                raise ValueError(kind + " block is missing or invalid")
            raw = await service._nas_io(
                request.operation_key,
                service.bundles.read_extent,
                index.relative_path,
                extent.offset,
                extent.length,
                expected_length=index.byte_length,
            )
            payloads[name] = load_metadata_json(raw)
        checkpoint = None
        if plan.checkpoint is not None:
            checkpoint = _checkpoint(
                payloads[plan.checkpoint.block_name],
                request,
                plan.checkpoint,
            )
        if plan.events is not None:
            _events(payloads[plan.events.block_name], request, plan.events)
        if plan.archive is not None:
            _archive(payloads[plan.archive.block_name], plan, payloads)
        return ArchivePayloads(checkpoint)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise IntegrityError(
            "archive/checkpoint/event body validation failed: " + str(error),
        ) from error
