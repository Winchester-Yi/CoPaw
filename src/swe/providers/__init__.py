# -*- coding: utf-8 -*-
"""Provider management — models, registry + persistent store."""

from __future__ import annotations

from .provider import ModelInfo, Provider, ProviderInfo
from .provider_manager import ProviderManager, ActiveModelsInfo

__all__ = [
    "ActiveModelsInfo",
    "ModelInfo",
    "Provider",
    "ProviderManager",
    "ProviderInfo",
]


def __getattr__(name: str):
    if name in {"ProviderManager", "ActiveModelsInfo"}:
        from .provider_manager import (
            ActiveModelsInfo as _ActiveModelsInfo,
            ProviderManager as _ProviderManager,
        )

        exports = {
            "ProviderManager": _ProviderManager,
            "ActiveModelsInfo": _ActiveModelsInfo,
        }
        return exports[name]
    raise AttributeError(name)
