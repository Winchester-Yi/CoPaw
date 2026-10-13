# -*- coding: utf-8 -*-
"""Cooperative bounded client representation; retain only numbers and HMACs.

Tools precede message blocks. JSON boundaries and fixed record labels frame
each occurrence. Scope and source IDs never enter the represented content.
The 5ms deadline is cooperative, checked before items and every 256B block.
Canonical schema/argument/result JSON is model text, including its keys and
punctuation. Plain text counts original UTF-8 bytes, excluding JSON escapes.
Role, protocol type and record framing contribute encoded bytes only.
"""

import hashlib
import hmac
import json
import math
from time import monotonic

from .context_contract import (
    CONTEXT_CAPTURE_SECONDS,
    CONTEXT_MAX_BLOCKS,
    CONTEXT_MAX_BYTES,
    CONTEXT_MAX_ITEMS,
    CapturedContext,
    ContextOccurrence,
    PREFIX_BLOCK_BYTES,
    REPRESENTATION_VERSION,
    context_item_limit,
)
from .provenance import find_source

_MEDIA = {"image", "image_url", "audio", "input_audio", "video", "file"}
_CONTENT_TYPES = {"text", "tool_use", "tool_result"}
_ROLES = {
    "system": "system",
    "user": "user",
    "assistant": "assistant",
    "tool": "tool_result",
}


class _CaptureStop(Exception):
    """A controlled gap, without input or error messages attached."""


def _string_parts(value, text=True, json_text=False):
    yield b'"', int(json_text)
    for start in range(0, len(value), 64):
        chunk = value[start : start + 64]
        if any(ord(c) < 32 or c in '\\"' for c in chunk):
            for char in chunk:
                encoded = json.dumps(char, ensure_ascii=False)[1:-1].encode()
                size = len(encoded) if json_text else len(char.encode())
                yield encoded, size if text else 0
        else:
            encoded = json.dumps(chunk, ensure_ascii=False)[1:-1].encode()
            yield encoded, len(encoded) if text else 0
    yield b'"', int(json_text)


def _sorted_keys(value, ignored):
    if len(value) > CONTEXT_MAX_ITEMS:
        raise _CaptureStop("item_limit")
    size = 0
    for key in value:
        if type(key) is not str:
            raise _CaptureStop("unsupported_content")
        size += len(key)
        if size > CONTEXT_MAX_BYTES:
            raise _CaptureStop("byte_limit")
    return [key for key in sorted(value) if key not in ignored]


def _protocol_type(value, media):
    modality = value.get("type")
    if media and modality in _MEDIA:
        raise _CaptureStop("media")
    if (
        media == "block"
        and modality is not None
        and modality not in _CONTENT_TYPES
    ):
        raise _CaptureStop("unsupported_content")
    if media == "calls" and modality != "function":
        raise _CaptureStop("unsupported_content")
    return modality


def _dict_parts(value, media, depth):
    modality = _protocol_type(value, media)
    ignored = (
        {"id", "tool_use_id", "tool_call_id", "type"}
        if media in ("block", "calls") and modality is not None
        else set()
    )
    yield b"{", 1
    for index, key in enumerate(_sorted_keys(value, ignored)):
        if index:
            yield b",", 1
        yield from _string_parts(key, json_text=True)
        yield b":", 1
        child_media = media
        if media in ("block", "calls"):
            child_media = (
                "block"
                if modality == "tool_result" and key == "content"
                else True
            )
        yield from _canonical_parts(value[key], child_media, depth + 1, True)
    yield b"}", 1


def _canonical_parts(value, media=False, depth=0, json_text=False):
    if depth > 64:
        raise _CaptureStop("unsupported_content")
    if type(value) is str:
        yield from _string_parts(value, json_text=json_text)
    elif type(value) is dict:
        yield from _dict_parts(value, media, depth)
    elif type(value) is list:
        yield b"[", 1
        for index, child in enumerate(value):
            if index:
                yield b",", 1
            yield from _canonical_parts(child, media, depth + 1, True)
        yield b"]", 1
    elif value is None or type(value) in (bool, int, float):
        if type(value) is float and not math.isfinite(value):
            raise _CaptureStop("unsupported_content")
        encoded = json.dumps(value).encode()
        yield encoded, len(encoded)
    else:
        raise _CaptureStop("unsupported_content")


def _hasher(scope, config, label):
    digest = hmac.new(
        config.fingerprint_key.encode(),
        digestmod=hashlib.sha256,
    )
    # Length framing prevents collisions between scope components/version.
    for value in (REPRESENTATION_VERSION, config.key_version, *scope, label):
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest


def _content_value(value):
    if type(value) is dict and value.get("type") == "text":
        return value.get("text", "")
    return value


def source_content_digest(value, scope, config, category=None):
    """Bounded registration digest; incomplete content has no identity."""
    if (
        not config.enabled
        or not config.context_enabled
        or not config.fingerprint_key
    ):
        return None
    if category != "tools" and type(value) is dict and "role" in value:
        value = value.get("content", "")
        if type(value) is list and len(value) == 1:
            value = value[0]
    deadline, size = monotonic() + CONTEXT_CAPTURE_SECONDS, 0
    try:
        if not _valid_scope(scope):
            return None
        digest = _hasher(scope, config, "content")
        payload = value if category == "tools" else _content_value(value)
        mode = False if category == "tools" else _payload_mode(value)
        for data, _ in _canonical_parts(payload, mode):
            if monotonic() >= deadline or size + len(data) > CONTEXT_MAX_BYTES:
                return None
            size += len(data)
            digest.update(data)
    except Exception:
        return None
    return digest.hexdigest()


def _payload_mode(value):
    if type(value) is dict and value.get("type") in _CONTENT_TYPES:
        return "block"
    if (
        type(value) is list
        and value
        and type(value[0]) is dict
        and value[0].get("type") == "function"
    ):
        return "calls"
    return True


def _items(messages, tools):
    for tool in tools or ():
        yield "tools", "", tool, tool
    for message in messages:
        if type(message) is not dict:
            raise _CaptureStop("unsupported_content")
        role = message.get("role", "")
        if type(role) is not str:
            raise _CaptureStop("unsupported_content")
        content = message.get("content", "")
        if content is None:
            content = ""
        if type(content) is list:
            blocks = content or ("",)
        else:
            blocks = (content,)
        for block in blocks:
            category = _ROLES.get(role, "unclassified")
            if type(block) is dict and block.get("type") == "tool_result":
                category = "tool_result"
            yield category, role, block, message if len(blocks) == 1 else block
        if message.get("tool_calls"):
            yield "assistant", role, message["tool_calls"], message[
                "tool_calls"
            ]


class _Builder:
    def __init__(self, result, scope, config):
        self.result, self.scope, self.config = result, scope, config
        self.deadline = monotonic() + CONTEXT_CAPTURE_SECONDS
        self.pending = bytearray()
        self.encoded = self.text = self.block_text = 0
        self.previous = bytes(32)

    def check(self):
        if monotonic() >= self.deadline:
            raise _CaptureStop("time_budget")
        if self.encoded >= CONTEXT_MAX_BYTES:
            raise _CaptureStop("byte_limit")

    def block(self):
        if not self.pending:
            return
        blocks = self.result["blocks"]
        if len(blocks) >= CONTEXT_MAX_BLOCKS:
            raise _CaptureStop("byte_limit")
        digest = _hasher(self.scope, self.config, "prefix")
        digest.update(len(blocks).to_bytes(8, "big"))
        digest.update(self.previous)
        digest.update(self.pending)
        self.previous = digest.digest()
        blocks.append(
            {
                "block_index": len(blocks),
                "chained_digest": digest.hexdigest(),
                "encoded_start": self.encoded - len(self.pending),
                "encoded_end": self.encoded,
                "text_start": self.block_text,
                "text_end": self.text,
            },
        )
        self.pending.clear()
        self.block_text = self.text

    def feed(self, data, text_bytes, digest, structure):
        offset = 0
        while offset < len(data):
            if not self.pending:
                self.check()
            length = min(
                PREFIX_BLOCK_BYTES - len(self.pending),
                len(data) - offset,
                CONTEXT_MAX_BYTES - self.encoded,
            )
            part = data[offset : offset + length]
            digest.update(part)
            structure.update(part)
            self.pending.extend(part)
            self.encoded += length
            offset += length
            # Raw UTF-8 is one-to-one. Escapes count the original character
            # when its final encoded byte is retained.
            self.text += (
                length
                if text_bytes == len(data)
                else text_bytes if offset == len(data) else 0
            )
            if len(self.pending) == PREFIX_BLOCK_BYTES:
                self.block()

    def occurrence(self, item):
        category, role, value, identity = item
        occurrence: ContextOccurrence = {
            "occurrence_index": len(self.result["occurrences"]),
            "source_object_id": None,
            "source_version": None,
            "category": category,
            "attribution": "structure" if category == "tools" else "role",
            "content_digest": None,
            "structure_digest": None,
            "text_start": self.text,
            "text_end": None,
            "encoded_start": self.encoded,
            "encoded_end": None,
            "token_start": None,
            "token_end": None,
        }
        self.result["occurrences"].append(occurrence)
        content = _hasher(self.scope, self.config, "content")
        structure = _hasher(self.scope, self.config, "structure")
        try:
            self._represent(category, role, value, content, structure)
        except _CaptureStop:
            occurrence["text_end"] = self.text
            occurrence["encoded_end"] = self.encoded
            raise
        occurrence.update(
            text_end=self.text,
            encoded_end=self.encoded,
            content_digest=content.hexdigest(),
            structure_digest=structure.hexdigest(),
            token_start=int(occurrence["text_start"] / self.config.divisor),
            token_end=int(self.text / self.config.divisor),
        )
        source = find_source(
            identity,
            content.hexdigest(),
            self.scope,
            self.config.key_version,
        )
        if source:
            occurrence.update(source, attribution="explicit")

    def _represent(self, category, role, value, content, structure):
        # Content digest excludes occurrence position, role, and source IDs.
        framing = _hasher(self.scope, self.config, "unused")
        self.feed(
            b"tool:" if category == "tools" else b"message:",
            0,
            framing,
            structure,
        )
        for data, _ in _string_parts(role, False):
            self.feed(data, 0, framing, structure)
        self.feed(b":", 0, framing, structure)
        label, payload, mode = _payload(value, category)
        self.feed(label.encode() + b":", 0, framing, structure)
        for data, text_bytes in _canonical_parts(payload, mode):
            self.feed(data, text_bytes, content, structure)
        self.feed(b";", 0, framing, structure)


def _payload(value, category):
    if category == "tools":
        return "schema", value, False
    if type(value) not in (str, dict, list):
        raise _CaptureStop("unsupported_content")
    mode = _payload_mode(value)
    label = "calls" if mode == "calls" else "text"
    if type(value) is dict:
        modality = _protocol_type(value, "block")
        if modality == "text" and type(value.get("text")) is not str:
            raise _CaptureStop("unsupported_content")
        label = modality or "object"
    return label, _content_value(value), mode


def _valid_scope(scope):
    return (
        type(scope) is tuple
        and len(scope) == 2
        and type(scope[0]) is str
        and 0 < len(scope[0]) <= 64
        and type(scope[1]) is str
        and 0 < len(scope[1]) <= 128
    )


def _valid_divisor(divisor):
    if type(divisor) not in (int, float):
        return False
    try:
        return (
            math.isfinite(divisor)
            and divisor > 0
            and CONTEXT_MAX_BYTES / divisor <= 2**63 - 1
        )
    except (OverflowError, ZeroDivisionError):
        return False


def capture_context(
    messages,
    tools,
    scope,
    config,
    *,
    context_id,
    context_role,
) -> CapturedContext:
    """Capture metadata only; unknown modality and limits preserve gaps."""
    if type(context_role) is not str or context_role not in {
        "prepared",
        "before",
        "after",
    }:
        raise ValueError("Unsupported context role")
    divisor_valid = _valid_divisor(config.divisor)
    result: CapturedContext = {
        "context_id": context_id,
        "context_role": context_role,
        "representation_version": REPRESENTATION_VERSION,
        "estimator_version": (
            f"utf8-density-v1:{config.divisor}"
            if divisor_valid
            else "utf8-density-v1:unavailable"
        ),
        "key_version": config.key_version,
        "capture_state": "unavailable",
        "capture_reason": None,
        "text_bytes": None,
        "encoded_bytes": None,
        "expected_item_count": None,
        "occurrences": [],
        "blocks": [],
    }
    if not config.enabled or not config.context_enabled:
        result["capture_reason"] = "disabled"
        return result
    if not config.fingerprint_key:
        result["capture_reason"] = "missing_key"
        return result
    if not _valid_scope(scope) or not divisor_valid:
        result["capture_reason"] = "capture_error"
        return result
    builder = _Builder(result, scope, config)
    try:
        iterator = iter(_items(messages, tools))
        while True:
            builder.check()
            if len(result["occurrences"]) >= context_item_limit(config):
                raise _CaptureStop("item_limit")
            try:
                item = next(iterator)
            except StopIteration:
                break
            builder.occurrence(item)
        result["capture_state"] = "complete"
        result["expected_item_count"] = len(result["occurrences"])
    except _CaptureStop as gap:
        result["capture_state"] = "partial"
        result["capture_reason"] = gap.args[0]
    except Exception:
        result["capture_state"] = "partial"
        result["capture_reason"] = "capture_error"
    try:
        builder.block()
    except Exception:
        result["capture_state"] = "partial"
        result["capture_reason"] = "capture_error"
    result.update(text_bytes=builder.text, encoded_bytes=builder.encoded)
    return result
