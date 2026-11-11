# -*- coding: utf-8 -*-
"""Source 级系统配置模型与默认值。"""

from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
    model_serializer,
)

from .registry import (
    build_default_source_system_config_payload,
    merge_source_system_config_with_defaults,
    normalize_registered_setting_values,
)


class SourceSystemConfig(BaseModel):
    """Source 系统配置载荷，业务 key 由具体使用方自行约定。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _validate_object(cls, data: Any, info: ValidationInfo) -> Any:
        """只接受 JSON object，避免数组或标量让调用方语义不明确。"""
        if not isinstance(data, dict):
            raise ValueError("source system config must be a JSON object")
        validate_cross_ranges = not (
            info.context
            and info.context.get("skip_tool_result_cross_validation")
        )
        return normalize_registered_setting_values(
            data,
            validate_cross_ranges=validate_cross_ranges,
        )

    def as_dict(self) -> dict[str, Any]:
        """返回普通 dict，供合并默认配置和序列化使用。"""
        return self.model_dump(mode="json")

    def merged_with_defaults(self) -> "SourceSystemConfig":
        """将 source 覆盖合并到内置默认配置。"""
        return SourceSystemConfig.model_validate(
            merge_source_system_config_with_defaults(self.as_dict()),
            context={"skip_tool_result_cross_validation": True},
        )


DEFAULT_SOURCE_SYSTEM_CONFIG = SourceSystemConfig.model_validate(
    build_default_source_system_config_payload(),
)


class _SchedulerSyncResponse(BaseModel):
    """保存响应可带同步受理结果，普通只读响应维持既有字段集合。"""

    scheduler_sync: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _serialize_sync(self, handler):
        result = handler(self)
        if self.scheduler_sync is None:
            result.pop("scheduler_sync", None)
        return result


class SourceSystemConfigRecord(_SchedulerSyncResponse):
    """Source 系统配置持久化记录。"""

    source_id: str = Field(..., min_length=1, max_length=64)
    config: SourceSystemConfig
    version: int = Field(default=1, ge=1)
    updated_by: str | None = Field(default=None, max_length=128)
    updated_at: datetime | None = None


class EffectiveSourceSystemConfig(BaseModel):
    """运行时合成后的 source 系统配置。"""

    source_id: str = Field(..., min_length=1, max_length=64)
    config: SourceSystemConfig
    raw_config: SourceSystemConfig | None = Field(default=None, exclude=True)
    version: int = Field(default=0, ge=0)
    is_default: bool = False
    stale: bool = False
    last_error: str | None = None
    updated_by: str | None = Field(default=None, max_length=128)
    updated_at: datetime | None = None


class CurrentSourceSystemConfigResponse(_SchedulerSyncResponse):
    """当前请求 source 的原始配置响应。"""

    source_id: str = Field(..., min_length=1, max_length=64)
    config: SourceSystemConfig
    version: int = Field(default=0, ge=0)
    is_default: bool = False
    updated_by: str | None = Field(default=None, max_length=128)
    updated_at: datetime | None = None


class SourceSystemConfigUpsert(BaseModel):
    """Source 系统配置创建或更新请求。"""

    model_config = ConfigDict(extra="forbid")

    config: SourceSystemConfig
    updated_by: str | None = Field(default=None, max_length=128)


class CurrentSourceSystemConfigUpdateRequest(BaseModel):
    """当前请求 source 的原始配置更新请求。"""

    model_config = ConfigDict(extra="forbid")

    config: SourceSystemConfig
