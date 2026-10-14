# -*- coding: utf-8 -*-
"""File-backed configuration for optional Jev decisions."""

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


class JevFeatureConfig(BaseModel):
    enabled: bool = False


class JevSkillConfig(JevFeatureConfig):
    threshold: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def require_threshold(self):
        if self.enabled and self.threshold is None:
            raise ValueError("Enabled skill selection requires threshold")
        return self


class JevSearchConfig(JevSkillConfig):
    top_k: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def require_top_k(self):
        if self.enabled and self.top_k is None:
            raise ValueError("Enabled search filtering requires top_k")
        return self


class JevConfig(BaseModel):
    endpoint: str = ""
    model: str = ""
    auth_token_env: str = ""
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
    )
    skill_selection: JevSkillConfig = Field(default_factory=JevSkillConfig)
    search_filtering: JevSearchConfig = Field(default_factory=JevSearchConfig)
    subagent_model_selection: JevFeatureConfig = Field(
        default_factory=JevFeatureConfig,
    )

    @model_validator(mode="after")
    def require_service(self):
        enabled = any(
            (
                self.skill_selection.enabled,
                self.search_filtering.enabled,
                self.subagent_model_selection.enabled,
            )
        )
        if enabled:
            parsed = urlsplit(self.endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or self.timeout_seconds is None
            ):
                raise ValueError(
                    "Enabled Jev requires an HTTP endpoint without credentials "
                    "and timeout_seconds",
                )
        return self
