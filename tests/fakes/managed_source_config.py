# -*- coding: utf-8 -*-
"""内部执行 HTTP 测试的配置存储替身，配置解析仍用真实模型。"""

from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)


class ManagedSourceConfigService:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    async def resolve_config(self, source_id):
        self.calls.append(source_id)
        return EffectiveSourceSystemConfig(
            source_id=source_id,
            version=7,
            config=SourceSystemConfig.model_validate(
                self.overrides.get(source_id, {}),
            ).merged_with_defaults(),
        )
