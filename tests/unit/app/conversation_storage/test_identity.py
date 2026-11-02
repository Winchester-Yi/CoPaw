# -*- coding: utf-8 -*-
from dataclasses import FrozenInstanceError

import pytest

from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
    chat_key_bytes,
    identity_bytes,
    question_msg_key_bytes,
    runtime_turn_key_bytes,
)


@pytest.mark.parametrize(
    "value,limit",
    [("", 256), (" A ", 256), ("a", 256), ("中" * 85, 256)],
)
def test_identity_preserves_exact_utf8(value, limit):
    assert identity_bytes(value, limit) == value.encode("utf-8")


def test_identity_rejects_byte_overflow_and_missing_values():
    with pytest.raises(ValueError, match="byte limit"):
        identity_bytes("中" * 86, 256)
    with pytest.raises(TypeError, match="string"):
        identity_bytes(None, 256)


def test_session_identity_is_exact_and_immutable():
    scope = WorkspaceScope(
        "Tenant",
        "source",
        "agent",
        service_unit_id="schema-test-service",
    )
    identity = SessionIdentity(scope, "", "console", "s_a_b")
    assert identity.sql_values == (
        b"schema-test-service",
        b"Tenant",
        b"source",
        b"agent",
        b"",
        b"console",
        b"s_a_b",
    )
    assert identity != SessionIdentity(
        WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
        "",
        "console",
        "s_a_b",
    )
    with pytest.raises(FrozenInstanceError):
        identity.user_id = "alice"


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "x" * 257),
        ("channel", "x" * 65),
        ("session_id", "x" * 1025),
    ],
)
def test_session_identity_enforces_schema_limits(field, value):
    values = {
        "scope": WorkspaceScope(
            "",
            "",
            "",
            service_unit_id="schema-test-service",
        ),
        "user_id": "",
        "channel": "",
        "session_id": "",
    }
    values[field] = value
    with pytest.raises(ValueError, match="byte limit"):
        SessionIdentity(**values)


@pytest.mark.parametrize(
    "validator,limit",
    [
        (chat_key_bytes, 128),
        (runtime_turn_key_bytes, 128),
        (question_msg_key_bytes, 256),
    ],
)
def test_external_identity_keys_enforce_binary_column_limits(validator, limit):
    value = "中" * (limit // 3 + 1)
    with pytest.raises(ValueError, match="byte limit"):
        validator(value)
    assert validator("A") == b"A"
