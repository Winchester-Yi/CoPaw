# -*- coding: utf-8 -*-
"""Token evidence must preserve unknowns without saving source content."""

import json
from types import SimpleNamespace

import pytest


def implementation():
    try:
        from swe.token_diagnostics.config import DiagnosticConfig
        from swe.token_diagnostics.input_capture import capture_inputs
        from swe.token_diagnostics.usage import extract_usage
    except ModuleNotFoundError:
        pytest.fail("Token diagnostics capture implementation is missing")
    return DiagnosticConfig, capture_inputs, extract_usage


def test_usage_distinguishes_missing_partial_and_explicit_zero():
    _, _, extract = implementation()
    assert extract(SimpleNamespace()).status == "unavailable"
    zero = extract(
        SimpleNamespace(usage={"input_tokens": 0, "output_tokens": 0})
    )
    assert zero.status == "reported"
    assert (zero.input_tokens, zero.output_tokens) == (0, 0)
    partial = extract(SimpleNamespace(usage={"input_tokens": 12}))
    assert partial.status == "partial"
    assert partial.output_tokens is None


def test_cache_and_reasoning_subitems_do_not_increase_reported_totals():
    _, _, extract = implementation()
    result = extract(
        SimpleNamespace(
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 20},
            }
        )
    )
    assert result.input_tokens == 120
    assert result.output_tokens == 30
    assert result.details == {"cache_read_tokens": 80, "reasoning_tokens": 20}


@pytest.mark.parametrize("value", [-1, True, "12", 2.5])
def test_invalid_provider_counts_are_not_reported_as_zero(value):
    _, _, extract = implementation()
    result = extract(
        SimpleNamespace(usage={"input_tokens": value, "output_tokens": 4})
    )
    assert result.input_tokens is None
    assert result.status == "partial"


def test_input_evidence_has_scoped_fingerprints_without_original_content():
    config_cls, capture, _ = implementation()
    config = config_cls(fingerprint_key="secret", enabled=True)
    message = {
        "role": "tool",
        "tool_call_id": "tool-1",
        "content": "PRIVATE customer balance 9123",
    }
    a = capture([message], [], "source:a", config)
    repeat = capture([message], [], "source:a", config)
    b = capture([message], [], "source:b", config)
    assert a["items"][0]["category"] == "tool_result"
    assert a["items"][0]["producer_operation_id"] == "tool-1"
    assert a["items"][0]["fingerprint"] == repeat["items"][0]["fingerprint"]
    assert a["items"][0]["fingerprint"] != b["items"][0]["fingerprint"]
    assert "PRIVATE" not in json.dumps(a)


def test_missing_key_and_media_never_create_fake_complete_fingerprints():
    config_cls, capture, _ = implementation()
    data = capture(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:secret"}}
                ],
            }
        ],
        [],
        "s:t",
        config_cls(enabled=True),
    )
    assert data["capture_state"] == "partial"
    assert data["items"][0]["estimated_tokens"] is None
    assert data["items"][0]["fingerprint"] is None
    assert "data:secret" not in json.dumps(data)


def test_capture_limits_mark_partial_and_do_not_mutate_input():
    config_cls, capture, _ = implementation()
    messages = [{"role": "user", "content": str(i)} for i in range(5)]
    before = json.dumps(messages)
    data = capture(messages, [], "s:t", config_cls(enabled=True, max_items=2))
    assert data["expected_item_count"] == 5
    assert len(data["items"]) == 2
    assert data["capture_state"] == "partial"
    assert json.dumps(messages) == before


def test_anthropic_cache_write_without_cache_read_is_counted_once():
    from swe.token_diagnostics.usage import extract_usage

    result = extract_usage(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_creation_input_tokens": 30,
            }
        }
    )
    assert result.input_tokens == 40
    assert result.output_tokens == 2


def test_agentscope_usage_metadata_keeps_numeric_subitems():
    from agentscope.model import ChatResponse
    from agentscope.model._model_usage import ChatUsage
    from swe.token_diagnostics.usage import extract_usage

    usage = ChatUsage(
        120,
        30,
        0.5,
        metadata={
            "prompt_tokens_details": {"cached_tokens": 80},
            "completion_tokens_details": {"reasoning_tokens": 20},
        },
    )
    result = extract_usage(ChatResponse(content=[], usage=usage))
    assert result.details == {"cache_read_tokens": 80, "reasoning_tokens": 20}
    assert (result.input_tokens, result.output_tokens) == (120, 30)


def test_media_inside_tool_result_remains_unknown():
    config_cls, capture, _ = implementation()
    result = capture(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "op",
                        "content": [
                            {
                                "type": "image",
                                "source": {"data": "private-base64"},
                            }
                        ],
                    }
                ],
            }
        ],
        [],
        "scope",
        config_cls(enabled=True, fingerprint_key="key"),
    )
    assert result["capture_state"] == "partial"
    assert result["items"][0]["estimated_tokens"] is None
    assert result["items"][0]["producer_operation_id"] == "op"
    assert "private-base64" not in str(result)
