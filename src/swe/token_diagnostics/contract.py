# -*- coding: utf-8 -*-
"""Stable metadata shared by token diagnostic producers."""

import hashlib
import json
import uuid

SCHEMA_VERSION = "1.0"
B_SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, B_SCHEMA_VERSION})
TELEMETRY_SOURCE = "swe_token_diagnostics"


def context_capture_enabled(config) -> bool:
    """The B switch only takes effect while overall capture is enabled."""
    return config.enabled is True and config.context_enabled is True


def schema_version(config) -> str:
    """Select the additive contract without changing legacy producers."""
    return (
        B_SCHEMA_VERSION if context_capture_enabled(config) else SCHEMA_VERSION
    )


def validate_schema_version(version) -> str:
    if (
        not isinstance(version, str)
        or version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise ValueError("Unsupported token diagnostic schema version")
    return version


def execution_identity(metadata):
    """Return execution identity independently of session and Trace."""
    identity_source = metadata.get("identity_source")
    execution_id = metadata.get("execution_id")
    if execution_id:
        execution_id = str(execution_id)
        if len(execution_id) > 256:
            execution_id = hashlib.sha256(execution_id.encode()).hexdigest()
        return execution_id, identity_source or "provided"
    turn = metadata.get("answer_turn_identity")
    if turn:
        values = [
            (
                turn.get(key)
                if isinstance(turn, dict)
                else getattr(turn, key, None)
            )
            for key in ("chat_id", "msgid", "turn_id")
        ]
        if all(values):
            return (
                hashlib.sha256(json.dumps(values).encode()).hexdigest(),
                "answer_turn",
            )
    if metadata.get("execution_origin") == "scheduled" and metadata.get(
        "cron_persistence_key",
    ):
        return str(metadata["cron_persistence_key"]), "cron_execution"
    return str(uuid.uuid4()), "token_generated"


def scoped_run_id(
    source_id,
    tenant_id,
    identity_source,
    execution_id,
    execution_origin="interactive",
):
    seed = json.dumps(
        [
            source_id,
            tenant_id,
            identity_source,
            execution_id,
            execution_origin,
        ],
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
