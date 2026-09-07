# -*- coding: utf-8 -*-
"""Operation group declaration, validation and attachment helpers.

The agent can declare that several consecutive tool calls belong to one
user-visible task phase (e.g. inspect an image, recognise its text and
verify the result) by attaching a `__swe_operation_group` object to each of
those tool call arguments::

    {"command": "...", "__swe_operation_group": {"id": "inspect-image",
                                           "name": "检查图片"}}

The group is display-only:

- `extract_operation_group` removes the reserved key from the arguments
  before the tool executes, so tool functions never receive it.
- `normalize_operation_group` treats the name as untrusted display text
  and applies a deterministic safety check; on failure the group silently
  falls back to a generic safe title (R5) without blocking execution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, MutableMapping
from copy import deepcopy
from typing import Any

#: Reserved key inside tool call arguments that carries the declaration.
OPERATION_GROUP_ARG_KEY = "__swe_operation_group"

# 仅在 Tool Guard 内部清理副本上携带，绝不发送给工具函数。
OPERATION_GROUP_INTERNAL_FIELD = "_swe_operation_group"

#: Field attached to tool message data with the normalized group.
OPERATION_GROUP_FIELD = "operation_group"

#: Generic safe title used when the agent-provided name fails validation.
OPERATION_GROUP_SAFE_TITLE = "任务操作"

_GROUP_ID_MAX_LENGTH = 64
_GROUP_NAME_MAX_LENGTH = 40

# Strict, deterministic id charset (letters, digits and safe separators).
# A strict charset keeps ids free of path/command noise.
_GROUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")

# Characters that may leak paths, commands, credentials, environment
# variables or markup into a user-visible group name.
_REJECTED_NAME_CHARS = frozenset("\\/`~$={}[]<>|&;*?#!%^'\"")
_SAFE_NAME_PATTERN = re.compile(
    r"^[\w\u3400-\u9fff\s:：,，.。·、()（）-]+$",
    re.UNICODE,
)
_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:api[_ -]?key|token|secret|password)"
    r"\s*[:= ]+\S+|(?:sk|ghp|github_pat|akia)[-_]?[a-z0-9]{8,})",
)
_COMMAND_NAME_PATTERN = re.compile(
    r"(?i)^(?:whoami|pwd|ls|dir|cat|rm|curl|wget|cmd|powershell|bash|sh)"
    r"(?:\s|$)",
)

_OPERATION_GROUP_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional display-only metadata that groups this tool call with "
        "other calls of the same user-visible task phase. Stripped before "
        "execution; never read by the tool itself."
    ),
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "Stable identifier for the phase; reuse the exact same value "
                "for every tool call in the phase and use a new value for a "
                "new phase."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "Short user-facing phase name (at most 40 characters, plain "
                "text, no paths, commands, quotes, credentials or "
                "environment variables)."
            ),
        },
    },
    "required": ["id", "name"],
}


def _clean_scalar(value: Any) -> str | None:
    """Return a trimmed non-empty string or `None`."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _valid_group_id(group_id: str) -> bool:
    return (
        len(group_id) <= _GROUP_ID_MAX_LENGTH
        and bool(_GROUP_ID_PATTERN.fullmatch(group_id))
    )


def _iter_scalar_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value.strip()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_scalar_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_scalar_strings(item)


def _matches_argument_value(name: str, arguments: Any) -> bool:
    normalized_name = name.casefold()
    return any(
        len(value) >= 4 and value.casefold() in normalized_name
        for value in _iter_scalar_strings(arguments)
        if value
    )


def _valid_group_name(name: str, arguments: Any = None) -> bool:
    if not name or len(name) > _GROUP_NAME_MAX_LENGTH:
        return False
    if any(ch in _REJECTED_NAME_CHARS or ord(ch) < 32 for ch in name):
        return False
    if not _SAFE_NAME_PATTERN.fullmatch(name):
        return False
    if _SENSITIVE_NAME_PATTERN.search(name) or _COMMAND_NAME_PATTERN.search(name):
        return False
    return not _matches_argument_value(name, arguments)


def normalize_operation_group(
    raw: Any,
    *,
    arguments: Any = None,
) -> dict[str, str] | None:
    """Deterministically validate an agent-declared operation group.

    Returns `{"id": ..., "title": ...}` when the declaration is usable,
    or `None` when the id itself is unusable.  An unusable *name*
    silently falls back to :data:`OPERATION_GROUP_SAFE_TITLE`; the raw
    name never reaches the display layer.
    """
    if not isinstance(raw, dict):
        return None
    group_id = _clean_scalar(raw.get("id"))
    if group_id is None or not _valid_group_id(group_id):
        return None
    name = raw.get("name", raw.get("title"))
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not _valid_group_name(name, arguments):
        name = OPERATION_GROUP_SAFE_TITLE
    return {"id": group_id, "title": name}


def _extract_from_dict(
    parsed: dict[str, Any],
) -> tuple[dict[str, str] | None, Any, bool]:
    """Extract the reserved key from a parsed argument dict in place.

    Returns `(group, cleaned_arguments)`; *cleaned_arguments* is the
    original dict when the reserved key was absent.
    """
    if OPERATION_GROUP_ARG_KEY not in parsed:
        return None, parsed, False
    cleaned = dict(parsed)
    raw = cleaned.pop(OPERATION_GROUP_ARG_KEY)
    group = normalize_operation_group(raw, arguments=cleaned)
    return group, cleaned, True


def extract_operation_group(arguments: Any) -> tuple[dict[str, str] | None, Any]:
    """Extract and remove the reserved group field from arguments.

    Returns (group, cleaned_arguments).  JSON-string arguments are
    re-serialised only when the reserved key was present, so ordinary
    arguments round-trip unchanged.
    """
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return None, arguments
        if not isinstance(parsed, dict):
            return None, arguments
        group, cleaned, removed = _extract_from_dict(parsed)
        if not removed:
            return group, arguments
        return group, json.dumps(cleaned, ensure_ascii=False)
    if isinstance(arguments, dict):
        group, cleaned, _removed = _extract_from_dict(arguments)
        return group, cleaned
    return None, arguments


def attach_operation_group(
    data: MutableMapping[str, Any],
    arguments: Any,
) -> dict[str, str] | None:
    """Attach the normalized group to tool message *data* in place.

    Also rewrites `data["arguments"]` when the reserved key was present,
    so the display payload never exposes the declaration metadata.
    """
    group, cleaned = extract_operation_group(arguments)
    if group is not None:
        data[OPERATION_GROUP_FIELD] = group
    if cleaned is not arguments and cleaned is not None:
        data["arguments"] = cleaned
    return group


def clean_tool_call_operation_group(
    tool_call: dict[str, Any],
) -> dict[str, Any]:
    """返回仅执行参数被清理的工具调用副本。"""
    input_value = tool_call.get("input", {})
    group, cleaned = extract_operation_group(input_value)
    if cleaned is input_value:
        return tool_call
    cleaned_call = dict(tool_call)
    cleaned_call["input"] = cleaned
    if group is not None:
        cleaned_call[OPERATION_GROUP_INTERNAL_FIELD] = group
    return cleaned_call


def inject_operation_group_schema(tool_schema: Any) -> None:
    """Add the optional reserved group property to a tool JSON schema.

    Mutates *tool_schema* in place (`{"type": "function", "function":
    {...}}` shape).  No-op for unknown shapes so MCP/source tool schemas
    that do not follow the convention are left untouched.
    """
    if not isinstance(tool_schema, dict):
        return
    function_block = tool_schema.get("function")
    if not isinstance(function_block, dict):
        return
    parameters = function_block.get("parameters")
    if not isinstance(parameters, dict):
        return
    properties = parameters.setdefault("properties", {})
    if OPERATION_GROUP_ARG_KEY in properties:
        return
    properties[OPERATION_GROUP_ARG_KEY] = deepcopy(
        _OPERATION_GROUP_SCHEMA_PROPERTY,
    )


def schema_parameters_without_operation_group(parameters: Any) -> Any:
    """返回移除展示保留字段后的 schema 副本。"""
    if not isinstance(parameters, dict):
        return parameters
    cleaned = deepcopy(parameters)
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        properties.pop(OPERATION_GROUP_ARG_KEY, None)
    return cleaned


def restore_operation_group_argument(
    tool_call: dict[str, Any],
    group: Any,
) -> dict[str, Any]:
    """将已校验的操作组重新附加到审批重放副本。"""
    normalized = normalize_operation_group(group)
    if normalized is None:
        return tool_call
    restored = dict(tool_call)
    tool_input = restored.get("input")
    restored_input = dict(tool_input) if isinstance(tool_input, dict) else {}
    restored_input[OPERATION_GROUP_ARG_KEY] = {
        "id": normalized["id"],
        "name": normalized["title"],
    }
    restored["input"] = restored_input
    return restored
