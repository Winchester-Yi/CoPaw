"""Published workflow invocation configuration."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class WorkflowValueMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["literal", "env", "runtime", "secret"]
    key: str = ""
    value: Any = None
    prefix: str = ""

    @model_validator(mode="after")
    def validate_source(self) -> "WorkflowValueMapping":
        if self.source != "literal" and not self.key.strip():
            raise ValueError("mapped workflow value requires key")
        return self


class WorkflowSuccessRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: list[str | int] = Field(min_length=1)
    equals: Any


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    skill_id: str = Field(min_length=1, max_length=200)
    url: str
    method: Literal["GET", "POST", "PUT", "PATCH"] = "POST"
    headers: dict[str, WorkflowValueMapping] = Field(default_factory=dict)
    path_params: dict[str, WorkflowValueMapping] = Field(default_factory=dict)
    query: dict[str, WorkflowValueMapping] = Field(default_factory=dict)
    body: dict[str, Any] | list[Any] | None = None
    success_rule: WorkflowSuccessRule | None = None
    result_fields: dict[str, list[str | int]] = Field(min_length=1)
    renderer_key: str = "direct"
    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=60, ge=1, le=7200)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("workflow url must be an absolute HTTP URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError(
                "workflow url cannot contain credentials or fragment"
            )
        if parsed.query or "{" in parsed.netloc or "}" in parsed.netloc:
            raise ValueError("workflow url query or host is invalid")
        return value

    @model_validator(mode="after")
    def validate_method_body(self) -> "WorkflowConfig":
        if self.method == "GET" and self.body is not None:
            raise ValueError("GET workflow cannot have a JSON body")
        placeholders = set(
            re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", self.url)
        )
        if placeholders != set(self.path_params):
            raise ValueError(
                "workflow path_params must match URL placeholders"
            )
        credential_headers = {
            "authorization",
            "cookie",
            "x-header-cookie",
            "x-api-key",
        }
        for name, mapping in self.headers.items():
            if (
                name.casefold() in credential_headers
                and mapping.source == "literal"
            ):
                raise ValueError(
                    "workflow credential header must use target secret",
                )
        return self
