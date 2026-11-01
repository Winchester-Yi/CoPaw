# -*- coding: utf-8 -*-
"""Prepare and validate immutable recovery projection installation deltas."""

from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime, timezone
import json

from .bundle import BundleBlock
from .chat_mutations import load_metadata_json
from .errors import IntegrityError
from .message_installs import MessageInstall
from .retention import ReaderPins, RetentionStore
from .identity import WorkspaceScope
from .projection_cache import cached, remember, cache_key
from .session_projection import (
    project_state,
    hydrate_state,
    is_projection,
    FORMAT,
    VERSION,
)

IDENTITY_FIELDS = (
    "operation_key",
    "scope_kind",
    "tenant_key",
    "source_key",
    "agent_key",
    "session_id",
    "chat_id",
    "turn_id",
    "base_state_revision",
    "base_history_revision",
    "epoch",
)


def _text(value):
    return (
        bytes(value).decode()
        if isinstance(value, (bytes, bytearray))
        else value
    )


def identity(row):
    return {key: _text(row[key]) for key in IDENTITY_FIELDS}


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _stamp(value):
    if value is None:
        return None
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat()


async def _existing(service, row):
    known = cached(cache_key(row))
    if known is not None:
        result = defaultdict(deque)
        for key, value in known.items():
            result[key].extend(value)
        return result
    result = defaultdict(deque)
    if row["session_id"] is None or not hasattr(service.db, "fetch_all"):
        remember(cache_key(row), result)
        return result
    retentions = RetentionStore(
        service.db,
        WorkspaceScope(
            _text(row["tenant_key"]),
            _text(row["source_key"]),
            _text(row["agent_key"]),
        ),
    )
    batch = ReaderPins(retentions)
    try:
        async with service.db.transaction():
            rows = await service.db.fetch_all(
                "SELECT * FROM swe_conversation_messages WHERE session_id=%s AND layer IN ('memory','task','error') AND current_visible=1 AND deleted_at IS NULL ORDER BY session_order",
                (row["session_id"],),
            )
            await batch.pin([item["publication_id"] for item in rows])
        if not rows:
            return result
        publications = {item["id"]: item for item in batch.publications}
        for item in rows:
            publication = publications[item["publication_id"]]
            if publication["session_id"] != row["session_id"]:
                raise IntegrityError(
                    "projection source Session association mismatch",
                )
            raw = await batch.read(
                service.bundles.read_extent,
                _text(publication["relative_path"]),
                item["byte_offset"],
                item["byte_length"],
                expected_length=publication["byte_length"],
            )
            value = load_metadata_json(raw)
            result[(item["layer"], canonical(value))].append(
                {
                    "publication_id": item["publication_id"],
                    "message_id": item["id"],
                    "offset": item["byte_offset"],
                    "length": item["byte_length"],
                },
            )
        remember(cache_key(row), result)
        return result
    finally:
        await batch.finish()


async def prepare_recovery_blocks(service, row, plan, blocks):
    if plan.recovery_block is None:
        return blocks
    root = next(
        (item for item in blocks if item.name == plan.recovery_block),
        None,
    )
    if root is None or not isinstance(root.content, bytes):
        return blocks
    state = load_metadata_json(root.content)
    if is_projection(state):
        raise IntegrityError(
            "writers must provide hydrated complete recovery state",
        )
    from .session_projection import LISTS, _parent

    has_history = any(
        (parent := _parent(state, path)) is not None
        and bool(parent.get(path[-1]))
        for path, _, _ in LISTS
    )
    available = (
        await _existing(service, row) if has_history else defaultdict(deque)
    )
    supplied = {item.name: item for item in blocks}
    context = await service.db.fetch_one(
        "SELECT context_epoch FROM swe_conversation_sessions WHERE id=%s",
        (row["session_id"],),
    )
    epoch = context["context_epoch"]
    for message in plan.messages:
        if (
            message.layer not in ("memory", "task", "error")
            or message.block_name not in supplied
        ):
            continue
        body = load_metadata_json(supplied[message.block_name].content)
        available[(message.layer, canonical(body))].append(
            {
                "operation_key": _text(row["operation_key"]),
                "block_name": message.block_name,
            },
        )

    def resolve(layer, body):
        values = available.get((layer, canonical(body)))
        return values.popleft() if values else None

    frozen_turns = await _frozen_turns(service, row, state)
    projection = project_state(
        state,
        operation_key=_text(row["operation_key"]),
        resolve_message=resolve,
        resolve_turn=lambda key, value: frozen_turns.get(key),
    )
    if not projection.envelope["lists"] and not projection.envelope["turns"]:
        return blocks
    installations = []
    for name, layer, body in projection.messages:
        source = body.get("id")
        role = body.get("role") or "assistant"
        installations.append(
            MessageInstall(
                name,
                layer,
                role,
                source_msg_key=source,
                turn_id=row["turn_id"],
                context_epoch=epoch,
                in_context=layer == "memory",
                message_timestamp=_stamp(body.get("timestamp")),
            ).to_dict(),
        )
    envelope = {
        **projection.envelope,
        "identity": identity(row),
        "installations": installations,
        "dependency_ids": list(projection.dependency_ids),
    }
    envelope["prune_ids"] = (
        await removed_occurrences(service, row, envelope)
        if has_history or projection.envelope["lists"]
        else []
    )
    turn_blocks = []
    if plan.turn_install is not None:
        turns = state.get("turn_states", {})
        for key, value in turns.items():
            if key in frozen_turns:
                continue
            if isinstance(value, dict):
                turn_blocks.append(
                    BundleBlock(
                        "turn:" + key,
                        "turn",
                        canonical(value).encode(),
                    ),
                )
    replacement = BundleBlock(
        root.name,
        root.kind,
        canonical(envelope).encode(),
    )
    return (
        [replacement if item is root else item for item in blocks]
        + list(projection.blocks)
        + turn_blocks
    )


async def _frozen_turns(service, row, state):
    result = {}
    if not isinstance(state.get("turn_states"), dict) or not hasattr(
        service.db,
        "fetch_all",
    ):
        return result
    turns = await service.db.fetch_all(
        "SELECT * FROM swe_conversation_turns WHERE session_id=%s AND terminal_publication_id IS NOT NULL ORDER BY id",
        (row["session_id"],),
    )
    retentions = RetentionStore(
        service.db,
        WorkspaceScope(
            _text(row["tenant_key"]),
            _text(row["source_key"]),
            _text(row["agent_key"]),
        ),
    )
    for turn in turns:
        key = _text(turn["question_msg_key"])
        value = state["turn_states"].get(key)
        if (
            not isinstance(value, dict)
            or value.get("status") != turn["history_status"]
        ):
            continue
        async with service.db.transaction():
            batch = await retentions.pin(
                [turn["terminal_publication_id"]],
                kind="reader",
            )
        try:
            publication = batch.publications[0]
            if (
                publication["session_id"] != row["session_id"]
                or publication["turn_id"] != turn["id"]
            ):
                raise IntegrityError(
                    "cold Turn publication association differs",
                )
            index = await batch.read(
                service.bundles.inspect,
                _text(publication["relative_path"]),
                expected_length=publication["byte_length"],
            )
            block = index.blocks.get("turn:" + key)
            if block is None or block.kind != "turn":
                continue
            raw = await batch.read(
                service.bundles.read_extent,
                index.relative_path,
                block.offset,
                block.length,
                expected_length=index.byte_length,
            )
            if load_metadata_json(raw) == value:
                result[key] = {
                    "publication_id": publication["id"],
                    "operation_key": _text(publication["operation_key"]),
                    "block_name": block and "turn:" + key,
                    "turn_id": turn["id"],
                }
        finally:
            await retentions.finish_reader(batch)
    return result


def _validate_ref_shape(ref):
    allowed = (
        {"publication_id", "message_id", "offset", "length"}
        if "message_id" in ref
        else (
            {"publication_id", "operation_key", "block_name", "turn_id"}
            if "publication_id" in ref
            else {"operation_key", "block_name"}
        )
    )
    if set(ref) != allowed:
        raise IntegrityError(
            "projection reference fields are not whitelisted",
        )


def _validate_additions(additions, existing_names, request):
    if len(additions) > 4096 or any(
        item.layer not in ("memory", "task", "error")
        or item.block_name in existing_names
        or item.source_message_id is not None
        or item.turn_id != request.turn_id
        for item in additions
    ):
        raise IntegrityError(
            "recovery projection installer is not an allowed delta",
        )
    if len({item.block_name for item in additions}) != len(additions):
        raise IntegrityError("projection delta block names repeat")


async def effective_recovery_plan(service, request, row, plan, index):
    if plan.recovery_block is None:
        return plan, None
    extent = index.blocks.get(plan.recovery_block)
    if extent is None or extent.kind != "recovery":
        raise IntegrityError("recovery projection block is missing")
    raw = await service._nas_io(
        request.operation_key,
        service.bundles.read_extent,
        index.relative_path,
        extent.offset,
        extent.length,
        expected_length=index.byte_length,
    )
    envelope = load_metadata_json(raw)
    if not is_projection(envelope):
        return plan, None
    if envelope.get("identity") != identity(row):
        raise IntegrityError(
            "recovery projection publication identity mismatch",
        )
    if not isinstance(envelope.get("installations"), list) or not isinstance(
        envelope.get("dependency_ids"),
        list,
    ):
        raise IntegrityError(
            "recovery projection installation metadata is invalid",
        )
    additions = tuple(
        MessageInstall.from_dict(item) for item in envelope["installations"]
    )
    existing_names = {item.block_name for item in plan.messages}
    _validate_additions(additions, existing_names, request)
    names = {item.block_name for item in additions} | existing_names
    refs = [
        entry["ref"]
        for series in envelope["lists"]
        for entry in series["entries"]
    ]
    refs.extend(item["ref"] for item in envelope["turns"])
    targets = set()
    for ref in refs:
        _validate_ref_shape(ref)
        if "publication_id" in ref:
            if (
                type(ref["publication_id"]) is not int
                or ref["publication_id"] <= 0
            ):
                raise IntegrityError(
                    "projection dependency identity is invalid",
                )
            targets.add(ref["publication_id"])
        else:
            if (
                ref.get("operation_key") != request.operation_key
                or ref.get("block_name") not in names
            ):
                raise IntegrityError(
                    "projection same-publication reference is not installed",
                )
            block = index.blocks.get(ref["block_name"])
            if block is None or block.kind not in (
                "memory",
                "message",
                "task",
                "error",
            ):
                raise IntegrityError(
                    "projection reference block kind is invalid",
                )
    if targets != set(envelope["dependency_ids"]):
        raise IntegrityError(
            "projection dependencies differ from explicit references",
        )
    if {item.block_name for item in additions} - {
        ref.get("block_name") for ref in refs if "publication_id" not in ref
    }:
        raise IntegrityError("unreferenced projection Message installer")
    for item in additions:
        block = index.blocks.get(item.block_name)
        if block is None or block.kind != item.layer:
            raise IntegrityError("projection delta body block kind mismatch")
        payload = await service._nas_io(
            request.operation_key,
            service.bundles.read_extent,
            index.relative_path,
            block.offset,
            block.length,
            expected_length=index.byte_length,
        )
        body = load_metadata_json(payload)
        if (
            not isinstance(body, dict)
            or body.get("id") != item.source_msg_key
            or (body.get("role") or "assistant") != item.role
        ):
            raise IntegrityError("projection delta body identity mismatch")
        if (
            item.origin_chat_id is not None
            or item.archive_chat_id is not None
            or not item.current_visible
            or item.in_context != (item.layer == "memory")
            or item.message_timestamp != _stamp(body.get("timestamp"))
        ):
            raise IntegrityError(
                "projection delta typed fields differ from allowed shape",
            )
    pruned = envelope.get("prune_ids", [])
    if (
        not isinstance(pruned, list)
        or len(pruned) > 4096
        or any(type(value) is not int or value <= 0 for value in pruned)
    ):
        raise IntegrityError("projection prune occurrence IDs are invalid")
    if sorted(set(pruned)) != await removed_occurrences(
        service,
        row,
        envelope,
    ):
        raise IntegrityError(
            "projection prune set differs from replaced root occurrences",
        )
    effective = replace(
        plan,
        messages=(*plan.messages, *additions),
        prune_ids=tuple(sorted(set(plan.prune_ids) | set(pruned))),
    )
    return effective, envelope


async def removed_occurrences(service, row, envelope):
    """Compare only explicitly replaced paths in the previous committed root."""
    from .projection_references import references

    retentions = RetentionStore(
        service.db,
        WorkspaceScope(
            _text(row["tenant_key"]),
            _text(row["source_key"]),
            _text(row["agent_key"]),
        ),
    )
    async with service.db.transaction():
        session = await service.db.fetch_one(
            "SELECT * FROM swe_conversation_sessions WHERE id=%s",
            (row["session_id"],),
        )
        if session is None or session.get("recovery_publication_id") is None:
            return []
        batch = await retentions.pin(
            [session["recovery_publication_id"]],
            kind="reader",
        )
    try:
        publication = batch.publications[0]
        if publication["session_id"] != row["session_id"]:
            raise IntegrityError("previous recovery root Session mismatch")
        previous_index = await batch.read(
            service.bundles.inspect,
            _text(publication["relative_path"]),
            expected_length=publication["byte_length"],
        )
        raw = await batch.read(
            service.bundles.read_extent,
            previous_index.relative_path,
            session["recovery_offset"],
            session["recovery_length"],
            expected_length=previous_index.byte_length,
        )
        previous = load_metadata_json(raw)
        if not is_projection(previous):
            return []
        paths = {tuple(series["path"]) for series in envelope["lists"]}
        retained = {
            ref.get("message_id")
            for ref in references(envelope)
            if "message_id" in ref
        }
        removed = []
        for series in previous["lists"]:
            if tuple(series["path"]) not in paths:
                continue
            for entry in series["entries"]:
                ref = entry["ref"]
                if "message_id" in ref:
                    identity = ref["message_id"]
                else:
                    extent = previous_index.blocks.get(ref["block_name"])
                    if (
                        ref.get("operation_key")
                        != _text(publication["operation_key"])
                        or extent is None
                    ):
                        raise IntegrityError(
                            "previous same-publication recovery locator is invalid",
                        )
                    prior = await service.db.fetch_one(
                        "SELECT * FROM swe_conversation_messages WHERE publication_id=%s AND byte_offset=%s AND byte_length=%s AND session_id=%s ORDER BY id LIMIT 1",
                        (
                            publication["id"],
                            extent.offset,
                            extent.length,
                            row["session_id"],
                        ),
                    )
                    if prior is None:
                        raise IntegrityError(
                            "previous recovery occurrence was not installed",
                        )
                    identity = prior["id"]
                if identity not in retained:
                    removed.append(identity)
        return sorted(set(removed))
    finally:
        await retentions.finish_reader(batch)
