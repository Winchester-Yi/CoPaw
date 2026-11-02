# -*- coding: utf-8 -*-
"""Exact, immutable identities from trusted workspace configuration."""

from dataclasses import dataclass, field
import os
import unicodedata

SERVICE_UNIT_ENV = "CMB_CAAS_SERVICEUNITID"
SCOPE_COLUMNS = ("service_unit_key", "tenant_key", "source_key", "agent_key")


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


def service_unit_bytes(value: str) -> bytes:
    encoded = identity_bytes(value, 256)
    if (
        not value
        or value != value.strip()
        or any(
            unicodedata.category(character) in {"Cc", "Cf"}
            for character in value
        )
    ):
        raise ValueError("conversation service unit is invalid")
    return encoded


def service_unit_from_env() -> str:
    value = os.environ.get(SERVICE_UNIT_ENV)
    if value is None:
        raise ValueError("CMB_CAAS_SERVICEUNITID is required")
    service_unit_bytes(value)
    return value


@dataclass(frozen=True)
class WorkspaceScope:
    """Trusted workspace identity; empty original values remain distinct."""

    tenant_id: str
    source_id: str
    agent_id: str
    service_unit_id: str = field(kw_only=True)

    def __post_init__(self):
        service_unit_bytes(self.service_unit_id)
        for value in (self.tenant_id, self.source_id, self.agent_id):
            identity_bytes(value, 256)

    @property
    def sql_values(self) -> tuple[bytes, ...]:
        return tuple(identity_bytes(value, 256) for value in self.values)

    @property
    def values(self) -> tuple[str, ...]:
        return (
            self.service_unit_id,
            self.tenant_id,
            self.source_id,
            self.agent_id,
        )

    @property
    def fields(self) -> dict[str, bytes]:
        return dict(zip(SCOPE_COLUMNS, self.sql_values))

    @classmethod
    def from_values(cls, values):
        if not isinstance(values, (tuple, list)) or len(values) != 4:
            raise ValueError(
                "conversation scope requires four explicit identities",
            )
        service, tenant, source, agent = (
            (
                bytes(value).decode("utf-8")
                if isinstance(value, (bytes, bytearray))
                else value
            )
            for value in values
        )
        return cls(tenant, source, agent, service_unit_id=service)

    @classmethod
    def from_row(cls, row):
        return cls.from_values([row[column] for column in SCOPE_COLUMNS])


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
