# -*- coding: utf-8 -*-
"""Configuration for the independent metadata event emitter."""

import os
import re
from dataclasses import dataclass

from ..__version__ import __version__
from ..env_defaults import VALID_ENVS

_RESOURCE_FIELDS = (
    "service_name",
    "service_version",
    "deployment_environment",
)
_RESOURCE_LABEL = re.compile(r"[A-Za-z0-9_.+-]{1,64}")


def project_resource(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: label
        for key in _RESOURCE_FIELDS
        if isinstance(label := value.get(key), str)
        and _RESOURCE_LABEL.fullmatch(label)
    }


def deployment_resource() -> dict[str, str]:
    """Capture the deployment's service attributes for a new local event."""
    environment = os.environ.get("SWE_ENV")
    return project_resource(
        {
            "service_name": "swe",
            "service_version": __version__,
            "deployment_environment": (
                environment if environment in VALID_ENVS else None
            ),
        },
    )


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def capture_enabled() -> bool:
    return os.environ.get("SWE_AGENTOBS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class EmitterConfig:
    max_event_bytes: int = 24 * 1024
    max_queue_events: int = 4096
    max_queue_bytes: int = 8 * 1024 * 1024
    shutdown_timeout: float = 1.0

    @classmethod
    def from_environment(cls) -> "EmitterConfig":
        return cls(
            max_event_bytes=_positive_int(
                "SWE_AGENTOBS_MAX_EVENT_BYTES",
                24 * 1024,
            ),
            max_queue_events=_positive_int("SWE_AGENTOBS_QUEUE_EVENTS", 4096),
            max_queue_bytes=_positive_int(
                "SWE_AGENTOBS_QUEUE_BYTES",
                8 * 1024 * 1024,
            ),
        )

    def __post_init__(self) -> None:
        if (
            min(
                self.max_event_bytes,
                self.max_queue_events,
                self.max_queue_bytes,
            )
            <= 0
        ):
            raise ValueError("emitter limits must be positive")
        object.__setattr__(
            self,
            "shutdown_timeout",
            min(1.0, max(0.0, self.shutdown_timeout)),
        )
