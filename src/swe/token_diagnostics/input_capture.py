# -*- coding: utf-8 -*-
"""Capture the prepared input's numbers and identities, never its content."""

import json

from .provenance import canonical, find_origin, fingerprint

_MEDIA_TYPES = {"image", "image_url", "audio", "input_audio", "video", "file"}
_ROLE_CATEGORY = {
    "system": "system",
    "user": "user",
    "assistant": "assistant",
    "tool": "tool_result",
}


def contains_media(value):
    if isinstance(value, dict):
        return value.get("type") in _MEDIA_TYPES or any(
            contains_media(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(contains_media(child) for child in value)
    return False


def _parts(messages, tools):
    for index, message in enumerate(messages):
        role = message.get("role", "")
        content = message.get("content", "")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            category = _ROLE_CATEGORY.get(role, "unclassified")
            operation = message.get("tool_call_id")
            media = contains_media(block)
            if isinstance(block, dict) and block.get("type") == "tool_result":
                category, operation = "tool_result", block.get(
                    "tool_use_id",
                ) or block.get("id")
            value = (
                block.get("text")
                if isinstance(block, dict) and block.get("type") == "text"
                else block
            )
            yield category, str(
                operation or "",
            ), f"message:{index}", value, media
        if message.get("tool_calls"):
            yield "assistant", "", f"tool-arguments:{index}", message[
                "tool_calls"
            ], False
    for index, tool in enumerate(tools or []):
        name = (tool.get("function") or tool).get("name", str(index))
        yield "tools", "", f"tool-schema:{name}"[:256], tool, False


def capture_inputs(messages, tools, scope, config) -> dict:
    items, expected, total, payload_bytes = [], 0, 0, 0
    complete = True
    modality = "text"
    for category, operation, source, value, media in _parts(messages, tools):
        if media:
            modality = "multimodal"
        expected += 1
        if len(items) >= config.max_items:
            complete = False
            continue
        text = "" if media else canonical(value)
        byte_count = len(text.encode("utf-8"))
        estimate = None if media else int(byte_count / config.divisor + 0.5)
        identity = None if media else fingerprint(text, scope, config)
        origin = (find_origin(identity) if identity else None) or {}
        item = {
            "occurrence_index": expected - 1,
            "category": origin.get("category", category),
            "source_ref": origin.get("source_ref", source),
            "producer_operation_id": operation[:64] or None,
            "fingerprint": identity,
            "key_version": config.key_version,
            "estimated_tokens": estimate,
            "retained_bytes": None if media else byte_count,
            "attribution_quality": (
                "explicit" if origin or operation else "role"
            ),
        }
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if payload_bytes + size > config.max_record_bytes:
            complete = False
            continue
        payload_bytes += size
        items.append(item)
        total += estimate or 0
        complete = complete and not media
    return {
        "items": items,
        "expected_item_count": expected,
        "estimated_input_tokens": total,
        "capture_state": "complete" if complete else "partial",
        "fingerprint_available": bool(config.fingerprint_key),
        "estimator_version": f"utf8-density-v1:{config.divisor}",
        "assembly_stage": "messages_only",
        "prompt_format_confidence": "message_approx",
        "context_builder_version": "prepared-messages-v1",
        "prompt_modality": modality,
    }
