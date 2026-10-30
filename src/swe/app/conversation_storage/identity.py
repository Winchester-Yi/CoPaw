# -*- coding: utf-8 -*-
"""Exact, immutable identities from trusted workspace configuration."""

from dataclasses import dataclass


def identity_bytes(value: str, limit: int) -> bytes:
    """Validate a SQL binary identity without normalizing its original value."""
    if not isinstance(value, str):
        raise TypeError("conversation identity must be a string")
    encoded = value.encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("conversation identity exceeds byte limit")
    return encoded


def chat_key_bytes(value: str) -> bytes:
    return identity_bytes(value, 128)


def runtime_turn_key_bytes(value: str) -> bytes:
    return identity_bytes(value, 128)


def question_msg_key_bytes(value: str) -> bytes:
    return identity_bytes(value, 256)


@dataclass(frozen=True)
class WorkspaceScope:
    """Trusted workspace identity; empty original values remain distinct."""

    tenant_id: str
    source_id: str
    agent_id: str

    def __post_init__(self):
        for value in (self.tenant_id, self.source_id, self.agent_id):
            identity_bytes(value, 256)

    @property
    def sql_values(self) -> tuple[bytes, bytes, bytes]:
        return (
            identity_bytes(self.tenant_id, 256),
            identity_bytes(self.source_id, 256),
            identity_bytes(self.agent_id, 256),
        )


@dataclass(frozen=True)
class SessionIdentity:
    """Complete Session key; filesystem encodings cannot supply these fields."""

    scope: WorkspaceScope
    user_id: str
    channel: str
    session_id: str

    def __post_init__(self):
        if not isinstance(self.scope, WorkspaceScope):
            raise TypeError("Session scope must be a WorkspaceScope")
        identity_bytes(self.user_id, 256)
        identity_bytes(self.channel, 64)
        identity_bytes(self.session_id, 1024)

    @property
    def sql_values(self) -> tuple[bytes, ...]:
        return self.scope.sql_values + (
            identity_bytes(self.user_id, 256),
            identity_bytes(self.channel, 64),
            identity_bytes(self.session_id, 1024),
        )
