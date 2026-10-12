# -*- coding: utf-8 -*-
"""Bounded diagnostic capture configuration; enablement follows Tracing."""

import os
from dataclasses import dataclass, field


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DiagnosticConfig:
    enabled: bool = False
    fingerprint_key: str = field(default="", repr=False)
    key_version: str = "v1"
    divisor: float = 3.75
    max_items: int = 2000
    max_record_bytes: int = 512 * 1024
    max_records: int = 1000
    max_bytes: int = 16 * 1024 * 1024
    batch_size: int = 64
    flush_interval: float = 1.0
    write_timeout: float = 3.0
    retry_age: float = 60.0
    shutdown_timeout: float = 3.0
    retention_days: int = 30

    @classmethod
    def from_env(cls):
        from ..constant import TRACING_ENABLED

        return cls(
            enabled=TRACING_ENABLED,
            fingerprint_key=os.environ.get(
                "SWE_TOKEN_DIAGNOSTICS_FINGERPRINT_KEY",
                "",
            ),
            key_version=os.environ.get(
                "SWE_TOKEN_DIAGNOSTICS_KEY_VERSION", "v1"
            )[:32],
            max_records=env_int("SWE_TOKEN_DIAGNOSTICS_MAX_RECORDS", 1000),
            max_bytes=env_int(
                "SWE_TOKEN_DIAGNOSTICS_MAX_BYTES", 16 * 1024 * 1024
            ),
            retention_days=env_int("SWE_TOKEN_DIAGNOSTICS_RETENTION_DAYS", 30),
        )
