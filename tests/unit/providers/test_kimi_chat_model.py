# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from swe.providers.kimi_chat_model import KimiChatModel


class KimiHarnessChatModel(KimiChatModel):
    async def _iter_base_stream_responses(  # type: ignore[override]
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Any = None,
    ):
        _ = (start_datetime, response, structured_model)
        for parsed in self._test_parsed_responses:
            yield parsed

    async def parse_stream_for_test(
        self,
        parsed_responses: list[Any],
    ) -> list[Any]:
        self._test_parsed_responses = parsed_responses
        results = []
        async for response in self._iter_kimi_stream_responses(
            datetime.now(),
            object(),
        ):
            results.append(response)
        return results


def _response(content: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(content=content)


async def test_kimi_chat_model_extracts_complete_think_blocks() -> None:
    model = KimiHarnessChatModel(
        "dummy",
        api_key="sk-test",
        stream=True,
    )

    responses = await model.parse_stream_for_test(
        [
            _response(
                [{"type": "text", "text": "<think>abc</think>tail"}],
            ),
        ],
    )

    assert responses[0].content == [
        {"type": "thinking", "thinking": "abc"},
        {"type": "text", "text": "tail"},
    ]


async def test_kimi_chat_model_extracts_closing_only_think_blocks() -> None:
    model = KimiHarnessChatModel(
        "dummy",
        api_key="sk-test",
        stream=True,
    )

    responses = await model.parse_stream_for_test(
        [
            _response(
                [{"type": "text", "text": "abc</think>"}],
            ),
        ],
    )

    assert responses[0].content == [
        {"type": "thinking", "thinking": "abc"},
    ]


async def test_kimi_chat_model_keeps_plain_text_unchanged() -> None:
    model = KimiHarnessChatModel(
        "dummy",
        api_key="sk-test",
        stream=True,
    )

    responses = await model.parse_stream_for_test(
        [
            _response(
                [{"type": "text", "text": "plain text"}],
            ),
        ],
    )

    assert responses[0].content == [
        {"type": "text", "text": "plain text"},
    ]
