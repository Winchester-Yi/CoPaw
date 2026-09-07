# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from swe.app.runner.operation_group import (
    OPERATION_GROUP_ARG_KEY,
    OPERATION_GROUP_FIELD,
    OPERATION_GROUP_SAFE_TITLE,
    attach_operation_group,
    clean_tool_call_operation_group,
    extract_operation_group,
    inject_operation_group_schema,
    normalize_operation_group,
    restore_operation_group_argument,
    schema_parameters_without_operation_group,
)


def test_normalize_accepts_valid_declaration() -> None:
    group = normalize_operation_group(
        {"id": "inspect-image", "name": "检查图片、识别文字"},
    )

    assert group == {
        "id": "inspect-image",
        "title": "检查图片、识别文字",
    }


def test_normalize_accepts_title_alias() -> None:
    group = normalize_operation_group(
        {"id": "verify", "title": "校验结果"},
    )

    assert group == {"id": "verify", "title": "校验结果"}


def test_normalize_rejects_unsafe_id() -> None:
    for raw in (
        {"id": "a b", "name": "name"},
        {"id": "/tmp/group", "name": "name"},
        {"id": "a" * 65, "name": "name"},
        {"id": 42, "name": "name"},
        {"id": "", "name": "name"},
        {"name": "name"},
        None,
        "not-a-dict",
    ):
        assert normalize_operation_group(raw) is None


def test_normalize_falls_back_to_safe_title_for_unsafe_name() -> None:
    for name in (
        "/tmp/demo",
        "rm -rf /tmp/demo",
        "key=secret",
        "C:/Users/demo",
        "quote'x",
        "x" * 41,
        "",
        "  ",
    ):
        group = normalize_operation_group({"id": "g1", "name": name})
        assert group is not None
        assert group["id"] == "g1"
        assert group["title"] == OPERATION_GROUP_SAFE_TITLE


def test_normalize_allows_cjk_and_safe_punctuation() -> None:
    group = normalize_operation_group(
        {"id": "g2", "name": "检查图片、识别文字：验证"},
    )

    assert group is not None
    assert group["title"] == "检查图片、识别文字：验证"


def test_extract_from_dict_strips_reserved_key() -> None:
    arguments = {
        "command": "ls",
        OPERATION_GROUP_ARG_KEY: {"id": "shell", "name": "环境检查"},
    }

    group, cleaned = extract_operation_group(arguments)

    assert group == {"id": "shell", "title": "环境检查"}
    assert cleaned == {"command": "ls"}
    assert OPERATION_GROUP_ARG_KEY not in cleaned
    assert OPERATION_GROUP_ARG_KEY in arguments


def test_extract_does_not_consume_a_business_operation_group_argument() -> None:
    arguments = {
        "operation_group": {"business": "keep-me"},
        "command": "ls",
    }

    group, cleaned = extract_operation_group(arguments)

    assert group is None
    assert cleaned is arguments
    assert cleaned["operation_group"] == {"business": "keep-me"}


def test_extract_from_dict_without_key_returns_same_object() -> None:
    arguments = {"command": "ls"}

    group, cleaned = extract_operation_group(arguments)

    assert group is None
    assert cleaned is arguments


def test_extract_from_json_string_round_trips_without_key() -> None:
    arguments = json.dumps(
        {
            "command": "ls",
            OPERATION_GROUP_ARG_KEY: {"id": "shell", "name": "环境检查"},
        },
        ensure_ascii=False,
    )

    group, cleaned = extract_operation_group(arguments)

    assert group == {"id": "shell", "title": "环境检查"}
    parsed = json.loads(cleaned)
    assert parsed == {"command": "ls"}
    assert OPERATION_GROUP_ARG_KEY not in parsed


def test_extract_from_json_string_without_key_keeps_original() -> None:
    arguments = '{"command": "ls"}'

    group, cleaned = extract_operation_group(arguments)

    assert group is None
    assert cleaned is arguments


def test_extract_from_invalid_json_returns_unchanged() -> None:
    arguments = "not-json{"

    group, cleaned = extract_operation_group(arguments)

    assert group is None
    assert cleaned is arguments


def test_attach_operation_group_updates_display_payload() -> None:
    data = {
        "name": "execute_shell_command",
        "arguments": json.dumps(
            {
                "command": "pwd",
                OPERATION_GROUP_ARG_KEY: {
                    "id": "shell",
                    "name": "环境检查",
                },
            },
        ),
    }

    group = attach_operation_group(data, data["arguments"])

    assert group == {"id": "shell", "title": "环境检查"}
    assert data[OPERATION_GROUP_FIELD] == group
    parsed = json.loads(data["arguments"])
    assert parsed == {"command": "pwd"}


def test_attach_operation_group_skips_when_absent() -> None:
    data = {"name": "grep_search", "arguments": '{"pattern": "x"}'}

    group = attach_operation_group(data, data["arguments"])

    assert group is None
    assert OPERATION_GROUP_FIELD not in data
    assert data["arguments"] == '{"pattern": "x"}'


def test_inject_operation_group_schema_adds_optional_property() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "execute_shell_command",
            "parameters": {"type": "object", "properties": {"command": {}}},
        },
    }

    inject_operation_group_schema(schema)

    parameters = schema["function"]["parameters"]
    assert OPERATION_GROUP_ARG_KEY in parameters["properties"]
    assert "id" in parameters["properties"][OPERATION_GROUP_ARG_KEY][
        "properties"
    ]


def test_inject_operation_group_schema_ignores_unknown_shapes() -> None:
    for schema in (None, {}, {"type": "function"}, {"function": {}}):
        inject_operation_group_schema(schema)  # should not raise


def test_sensitive_or_argument_derived_titles_fall_back() -> None:
    unsafe_titles = (
        "Bearer abcdefghijklmnop",
        "sk-proj-abcdefghijklmno",
        "token abcdefghijklmnop",
        "whoami",
    )
    for title in unsafe_titles:
        arguments = {
            "command": title,
            OPERATION_GROUP_ARG_KEY: {"id": "g1", "name": title},
        }

        group, _cleaned = extract_operation_group(arguments)

        assert group == {"id": "g1", "title": OPERATION_GROUP_SAFE_TITLE}


def test_clean_tool_call_preserves_original_and_tracks_group() -> None:
    from swe.app.runner.operation_group import OPERATION_GROUP_INTERNAL_FIELD

    tool_call = {
        "id": "tool-1",
        "name": "execute_shell_command",
        "input": {
            "command": "echo ok",
            OPERATION_GROUP_ARG_KEY: {"id": "inspect", "name": "检查图片"},
        },
    }

    cleaned = clean_tool_call_operation_group(tool_call)

    assert OPERATION_GROUP_ARG_KEY in tool_call["input"]
    assert cleaned["input"] == {"command": "echo ok"}
    assert cleaned[OPERATION_GROUP_INTERNAL_FIELD] == {
        "id": "inspect",
        "title": "检查图片",
    }


def test_schema_comparison_ignores_reserved_display_property() -> None:
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
    }
    decorated = {
        "type": "function",
        "function": {
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        },
    }
    inject_operation_group_schema(decorated)

    assert schema_parameters_without_operation_group(
        decorated["function"]["parameters"],
    ) == parameters


def test_restore_operation_group_argument_uses_a_copy() -> None:
    tool_call = {
        "id": "tool-1",
        "name": "execute_shell_command",
        "input": {"command": "echo ok"},
    }

    restored = restore_operation_group_argument(
        tool_call,
        {"id": "inspect", "title": "检查图片"},
    )

    assert OPERATION_GROUP_ARG_KEY not in tool_call["input"]
    assert restored["input"][OPERATION_GROUP_ARG_KEY] == {
        "id": "inspect",
        "name": "检查图片",
    }
