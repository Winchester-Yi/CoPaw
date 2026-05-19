# -*- coding: utf-8 -*-
"""猜你想问服务与 source 配置解析测试。"""

from __future__ import annotations

import pytest

from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)
from swe.app.suggestions.service import (
    SUGGESTION_PROMPT_TEMPLATE,
    SuggestionService,
    generate_suggestions,
)
from swe.app.suggestions.source_config import (
    DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG,
    FOLLOW_UP_SUGGESTIONS_CONFIG_KEY,
    get_follow_up_suggestions_config,
)


def _effective(config: dict) -> EffectiveSourceSystemConfig:
    return EffectiveSourceSystemConfig(
        source_id="source-a",
        config=SourceSystemConfig.model_validate(config),
    )


def test_follow_up_suggestions_config_defaults_when_missing() -> None:
    config = get_follow_up_suggestions_config(_effective({}))
    assert config.enabled is True
    assert (
        config.prompt_template
        == DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.prompt_template
    )


def test_follow_up_suggestions_config_reads_nested_values() -> None:
    config = get_follow_up_suggestions_config(
        _effective(
            {
                FOLLOW_UP_SUGGESTIONS_CONFIG_KEY: {
                    "enabled": False,
                    "prompt_template": "自定义模板 {max_count}",
                },
            },
        ),
    )
    assert config.enabled is False
    assert config.prompt_template == "自定义模板 {max_count}"


def test_follow_up_suggestions_config_ignores_invalid_shape() -> None:
    config = get_follow_up_suggestions_config(
        _effective({FOLLOW_UP_SUGGESTIONS_CONFIG_KEY: "invalid"}),
    )
    assert config.enabled is True
    assert (
        config.prompt_template
        == DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.prompt_template
    )


def test_follow_up_suggestions_config_blank_prompt_keeps_enabled() -> None:
    config = get_follow_up_suggestions_config(
        _effective(
            {
                FOLLOW_UP_SUGGESTIONS_CONFIG_KEY: {
                    "enabled": False,
                    "prompt_template": "   ",
                },
            },
        ),
    )
    assert config.enabled is False
    assert (
        config.prompt_template
        == DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.prompt_template
    )


def test_follow_up_suggestions_config_invalid_dict_payload_returns_default() -> (
    None
):
    config = get_follow_up_suggestions_config(
        _effective(
            {
                FOLLOW_UP_SUGGESTIONS_CONFIG_KEY: {
                    "enabled": False,
                    "prompt_template": ["bad"],
                },
            },
        ),
    )
    assert config.enabled is True
    assert (
        config.prompt_template
        == DEFAULT_FOLLOW_UP_SUGGESTIONS_CONFIG.prompt_template
    )


@pytest.mark.asyncio
async def test_generate_suggestions_uses_custom_prompt_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_messages: list[dict[str, str]] = []

    async def fake_model(messages):
        captured_messages.extend(messages)
        return type("Response", (), {"text": '["问题A", "问题B"]'})()

    async def fake_get_model():
        return fake_model

    monkeypatch.setattr(SuggestionService, "get_model", fake_get_model)

    suggestions = await generate_suggestions(
        user_message="用户问题",
        assistant_response="助手回答",
        prompt_template=(
            "自定义提示：" "{user_message}|{assistant_response}|{max_count}"
        ),
    )

    assert suggestions == ["问题A", "问题B"]
    assert captured_messages[1]["content"] == "自定义提示：用户问题|助手回答|3"


@pytest.mark.asyncio
async def test_generate_suggestions_falls_back_when_custom_prompt_template_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_messages: list[dict[str, str]] = []

    async def fake_model(messages):
        captured_messages.extend(messages)
        return type("Response", (), {"text": '["问题A", "问题B"]'})()

    async def fake_get_model():
        return fake_model

    monkeypatch.setattr(SuggestionService, "get_model", fake_get_model)

    suggestions = await generate_suggestions(
        user_message="用户问题",
        assistant_response="助手回答",
        prompt_template="坏模板 {missing_key}",
    )

    assert suggestions == ["问题A", "问题B"]
    assert captured_messages[1][
        "content"
    ] == SUGGESTION_PROMPT_TEMPLATE.format(
        max_count=3,
        user_message="用户问题",
        assistant_response="助手回答",
    )
