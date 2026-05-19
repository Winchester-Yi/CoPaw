# -*- coding: utf-8 -*-
"""猜你想问功能的 source 系统配置解析器。"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from swe.app.source_system_config.models import EffectiveSourceSystemConfig
from swe.app.source_system_config.runtime import (
    get_current_source_system_config,
)
from swe.app.suggestions.service import SUGGESTION_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)

FOLLOW_UP_SUGGESTIONS_CONFIG_KEY = "follow_up_suggestions"


class FollowUpSuggestionsSourceConfig(BaseModel):
    """猜你想问的 source 级配置。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    prompt_template: str = Field(
        default=SUGGESTION_PROMPT_TEMPLATE,
        min_length=1,
        max_length=8000,
    )


DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG = FollowUpSuggestionsSourceConfig()


def get_follow_up_suggestions_config(
    effective_config: EffectiveSourceSystemConfig | None = None,
) -> FollowUpSuggestionsSourceConfig:
    """读取并解析猜你想问配置，异常时回退默认值。"""
    resolved_config = effective_config or get_current_source_system_config()
    if resolved_config is None:
        return DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.model_copy(
            deep=True,
        )

    raw_config = resolved_config.config.as_dict()
    if FOLLOW_UP_SUGGESTIONS_CONFIG_KEY not in raw_config:
        return DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.model_copy(
            deep=True,
        )

    nested_config = raw_config[FOLLOW_UP_SUGGESTIONS_CONFIG_KEY]
    if not isinstance(nested_config, dict):
        logger.warning(
            "Invalid follow_up_suggestions config type: %s",
            type(nested_config).__name__,
        )
        return DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.model_copy(
            deep=True,
        )

    try:
        parsed_config = FollowUpSuggestionsSourceConfig.model_validate(
            nested_config,
        )
    except ValidationError as exc:
        logger.warning(
            "Invalid follow_up_suggestions config payload, fallback default: %s",
            exc,
        )
        return DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.model_copy(
            deep=True,
        )

    if not parsed_config.prompt_template.strip():
        parsed_config.prompt_template = SUGGESTION_PROMPT_TEMPLATE

    return parsed_config
