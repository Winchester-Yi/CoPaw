# -*- coding: utf-8 -*-
"""Small immutable usage values; unknown counts remain null."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UsageSnapshot:
    input_tokens: int | None = None
    output_tokens: int | None = None
    details: dict = field(default_factory=dict)
    finish_reason: str | None = None

    @property
    def status(self) -> str:
        values = (self.input_tokens, self.output_tokens)
        if all(value is not None for value in values):
            return "reported"
        if any(value is not None for value in values):
            return "partial"
        return "unavailable"

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_status": self.status,
            "usage_details": self.details,
            "finish_reason": self.finish_reason,
            "usage_origin": (
                "provider" if self.status != "unavailable" else "unknown"
            ),
            "usage_accuracy": (
                "reported" if self.status != "unavailable" else "unavailable"
            ),
        }
