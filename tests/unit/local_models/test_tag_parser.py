# -*- coding: utf-8 -*-
from swe.local_models.tag_parser import (
    extract_thinking_from_text,
    normalize_thinking_prefix,
    strip_think_tags,
)


def test_extract_thinking_from_complete_block() -> None:
    result = extract_thinking_from_text("<think>abc</think>tail")

    assert result.thinking == "abc"
    assert result.remaining_text == "tail"
    assert result.has_open_tag is False


def test_extract_thinking_from_closing_only_block() -> None:
    result = extract_thinking_from_text("abc</think>")

    assert result.thinking == "abc"
    assert result.remaining_text == ""
    assert result.has_open_tag is False


def test_strip_think_tags_removes_literal_markers() -> None:
    assert strip_think_tags("<think>abc</think>tail") == "abctail"
    assert strip_think_tags("abc</think>") == "abc"


def test_normalize_thinking_prefix_removes_leading_tag_wrappers() -> None:
    assert normalize_thinking_prefix("<think>abc</think>") == "abc"
    assert normalize_thinking_prefix("abc</think>") == "abc"
    assert normalize_thinking_prefix("plain text") == "plain text"
