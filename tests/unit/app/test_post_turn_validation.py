# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest

from swe.app import post_turn_validation as validation


def test_parse_suggestion_only_incomplete_as_completed() -> None:
    result = validation._parse_validation_result(
        json.dumps(
            {
                "completed": False,
                "reason": "还可以补充猜你想问，引导用户继续提问",
                "follow_up_prompt": "生成 3 个猜你想问的后续问题。",
            },
            ensure_ascii=False,
        ),
    )

    assert result.completed is True
    assert result.follow_up_prompt == ""


def test_parse_real_incomplete_still_requires_confirmation() -> None:
    result = validation._parse_validation_result(
        json.dumps(
            {
                "completed": False,
                "reason": "文件还没有修改完成",
                "follow_up_prompt": "继续修改文件并运行测试。",
            },
            ensure_ascii=False,
        ),
    )

    assert result.completed is False
    assert result.follow_up_prompt == "继续修改文件并运行测试。"


def test_strip_trailing_suggestion_section_before_validation() -> None:
    cleaned = validation._strip_suggestion_sections(
        "任务已经处理完成。\n\n猜你想问：\n- 如何验证结果？\n- 下一步怎么做？",
    )

    assert cleaned == "任务已经处理完成。"


@pytest.mark.asyncio
async def test_validate_task_completion_does_not_send_suggestions_to_model(
    monkeypatch,
) -> None:
    captured = {}

    async def fake_model(messages):
        captured["prompt"] = messages[-1]["content"]

        class FakeResponse:
            text = (
                '{"completed": true, "reason": "done", "follow_up_prompt": ""}'
            )

        return FakeResponse()

    monkeypatch.setattr(
        validation,
        "create_model_and_formatter",
        lambda agent_id=None: (fake_model, None),
    )

    result = await validation.validate_task_completion(
        user_message="帮我整理结论",
        assistant_response=(
            "结论已经整理完成。\n\n"
            "猜你想问：\n"
            "- 怎么落地？\n"
            "- 风险是什么？"
        ),
    )

    assert result.completed is True
    assistant_section = captured["prompt"].split("助手最新回答（摘要）：", 1)[
        1
    ]
    assert "猜你想问：" not in assistant_section
    assert "怎么落地" not in captured["prompt"]


@pytest.mark.asyncio
async def test_validate_task_completion_accumulates_streaming_chunks(
    monkeypatch,
) -> None:
    class FakeStreamResponse:
        def __aiter__(self):
            async def _gen():
                for text in (
                    '{"completed": true',
                    ', "reason": "done"',
                    ', "follow_up_prompt": ""}',
                ):
                    yield type(
                        "Chunk",
                        (),
                        {
                            "content": [
                                {"type": "text", "text": text},
                            ],
                        },
                    )()

            return _gen()

    async def fake_model(_messages):
        return FakeStreamResponse()

    monkeypatch.setattr(
        validation,
        "create_model_and_formatter",
        lambda agent_id=None: (fake_model, None),
    )

    result = await validation.validate_task_completion(
        user_message="帮我整理结论",
        assistant_response="结论已经整理完成。",
    )

    assert result.completed is True
    assert result.reason == "done"
