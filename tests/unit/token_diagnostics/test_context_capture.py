# -*- coding: utf-8 -*-
"""Context evidence is ordered, bounded, scoped, and free of raw content."""

import copy
import json
from dataclasses import replace

import pytest

from swe.token_diagnostics.config import DiagnosticConfig

CONFIG = DiagnosticConfig(
    enabled=True,
    context_enabled=True,
    fingerprint_key="test",
)


def implementation():
    try:
        from swe.token_diagnostics import context_capture
    except ImportError:
        pytest.fail("Bounded context capture implementation is missing")
    return context_capture


@pytest.fixture(autouse=True)
def controlled_clock(monkeypatch):
    try:
        from swe.token_diagnostics import context_capture
    except ImportError:
        return
    monkeypatch.setattr(context_capture, "monotonic", lambda: 0.0)


def capture(messages, tools=None, scope=("source", "tenant"), config=CONFIG):
    return implementation().capture_context(
        messages,
        tools or [],
        scope,
        config,
        context_id="ctx",
        context_role="prepared",
    )


def digests(result):
    return [block["chained_digest"] for block in result["blocks"]]


def test_tools_precede_messages_and_order_is_preserved():
    tools = [{"name": "first"}, {"name": "second"}]
    messages = [{"role": "user", "content": "PRIVATE"}]
    before = copy.deepcopy((messages, tools))
    result = capture(messages, tools)
    assert result["capture_state"] == "complete"
    assert [o["category"] for o in result["occurrences"]] == [
        "tools",
        "tools",
        "user",
    ]
    assert digests(result) != digests(capture(messages, tools[::-1]))
    assert (messages, tools) == before
    assert "PRIVATE" not in json.dumps(result)


def test_schema_key_order_normalized_but_array_order_matters():
    a = {"name": "t", "schema": {"enum": ["a", "b"], "type": "str"}}
    b = {"schema": {"type": "str", "enum": ["a", "b"]}, "name": "t"}
    assert digests(capture([], [a])) == digests(capture([], [b]))
    b["schema"]["enum"].reverse()
    assert digests(capture([], [a])) != digests(capture([], [b]))


def test_canonical_object_bytes_are_model_text_not_framing():
    tool = {"name": "t", "schema": {"enum": ["é", 2], "type": "string"}}
    canonical = json.dumps(
        tool,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    result = capture([], [tool])
    assert result["text_bytes"] == len(canonical)
    assert result["encoded_bytes"] > result["text_bytes"]
    block = {
        "type": "tool_result",
        "tool_use_id": "id",
        "content": {"result": 123},
    }
    result = capture([{"role": "user", "content": [block]}])
    payload = json.dumps(
        {"content": {"result": 123}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert result["text_bytes"] == len(payload)


def test_schema_escaped_json_counts_canonical_utf8_bytes():
    tool = {"name": "a\\b\nc"}
    result = capture([], [tool])
    canonical = json.dumps(
        tool,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    assert result["text_bytes"] == len(canonical)


@pytest.mark.parametrize("text", ["a b", "a  b", "a\nb", "中文é😀"])
def test_text_utf8_and_whitespace_are_preserved(text):
    result = capture([{"role": "user", "content": text}])
    assert result["text_bytes"] == len(text.encode("utf-8"))
    assert result["occurrences"][0]["text_end"] == result["text_bytes"]
    assert digests(result) != digests(
        capture([{"role": "user", "content": text + " "}]),
    )


def test_role_and_framed_boundaries_change_representation():
    assert digests(capture([{"role": "user", "content": "same"}])) != (
        digests(capture([{"role": "assistant", "content": "same"}]))
    )
    assert digests(capture([{"role": "user", "content": 'a"},b'}])) != (
        digests(
            capture(
                [
                    {"role": "user", "content": "a"},
                    {"role": "user", "content": "b"},
                ],
            ),
        )
    )


@pytest.mark.parametrize("scope", [("other", "tenant"), ("source", "other")])
def test_scope_and_key_version_isolate_all_digests(scope):
    message = [{"role": "user", "content": "same"}]
    original = capture(message)
    assert digests(original) != digests(capture(message, scope=scope))
    newer = capture(message, config=replace(CONFIG, key_version="v2"))
    assert digests(original) != digests(newer)
    assert original["occurrences"][0]["content_digest"] != (
        newer["occurrences"][0]["content_digest"]
    )


def test_occurrences_independent_and_operation_ids_not_source_identity():
    messages = [
        {"role": "tool", "tool_call_id": "id-a", "content": "same"},
        {"role": "tool", "tool_call_id": "id-b", "content": "same"},
    ]
    result = capture(messages)
    first, second = result["occurrences"]
    assert first["occurrence_index"] == 0
    assert second["occurrence_index"] == 1
    assert first["content_digest"] == second["content_digest"]
    assert first["source_object_id"] is None
    assert first["attribution"] == "role"
    messages[0]["tool_call_id"] = "changed"
    assert digests(result) == digests(capture(messages))


def test_identity_first_registration_is_scoped_and_temporary():
    implementation()
    from swe.token_diagnostics.provenance import register_source

    first = {"role": "user", "content": "same"}
    second = copy.deepcopy(first)
    plain = capture([first, second])
    with (
        register_source(
            first,
            ("source", "tenant"),
            CONFIG,
            source_object_id="source-1",
            source_version="1",
            category="memory",
        ),
        register_source(
            second,
            ("source", "tenant"),
            CONFIG,
            source_object_id="source-2",
            source_version="1",
            category="memory",
        ),
    ):
        bound = capture([first, second])
        assert [o["source_object_id"] for o in bound["occurrences"]] == [
            "source-1",
            "source-2",
        ]
        assert bound["occurrences"][0]["attribution"] == "explicit"
        assert digests(bound) == digests(plain)
        assert (
            capture([copy.deepcopy(first)])["occurrences"][0][
                "source_object_id"
            ]
            is None
        )
        assert (
            capture([first], scope=("source", "other"))["occurrences"][0][
                "source_object_id"
            ]
            is None
        )
        first["content"] = "changed"
        assert capture([first])["occurrences"][0]["source_object_id"] is None
    assert capture([second])["occurrences"][0]["source_object_id"] is None


@pytest.mark.parametrize(
    "config,reason",
    [
        (replace(CONFIG, fingerprint_key=""), "missing_key"),
        (replace(CONFIG, enabled=False), "disabled"),
        (replace(CONFIG, context_enabled=False), "disabled"),
    ],
)
def test_disabled_or_missing_key_unavailable(config, reason):
    result = capture([{"role": "user", "content": "PRIVATE"}], config=config)
    assert result["capture_state"] == "unavailable"
    assert result["capture_reason"] == reason
    assert result["blocks"] == []
    assert result["text_bytes"] is None


@pytest.mark.parametrize(
    "block,reason",
    [
        ({"type": "image_url", "image_url": {"url": "PRIVATE"}}, "media"),
        (
            {"type": "future_modality", "data": "PRIVATE"},
            "unsupported_content",
        ),
        ({"type": "tool_result", "content": [{"type": "audio"}]}, "media"),
    ],
)
def test_unknown_or_media_never_claim_complete(block, reason):
    result = capture([{"role": "user", "content": [block]}])
    assert result["capture_state"] == "partial"
    assert result["capture_reason"] == reason
    assert result["occurrences"][0]["content_digest"] is None
    assert "PRIVATE" not in repr(result)


def test_blocks_have_exact_contiguous_intervals_and_short_tail():
    result = capture([{"role": "user", "content": "a" * 700}])
    blocks = result["blocks"]
    assert len(blocks) == 3
    assert blocks[-1]["encoded_end"] - blocks[-1]["encoded_start"] < 256
    assert blocks[0]["text_start"] == 0
    for previous, current in zip(blocks, blocks[1:]):
        assert previous["encoded_end"] == current["encoded_start"]
        assert previous["text_end"] == current["text_start"]
    assert blocks[-1]["encoded_end"] == result["encoded_bytes"]
    assert blocks[-1]["text_end"] == 700


def test_byte_limit_streams_huge_text_without_whole_encoding(monkeypatch):
    module = implementation()
    dumps = module.json.dumps
    largest = []

    def bounded_dumps(value, *args, **kwargs):
        if isinstance(value, str):
            largest.append(len(value))
            assert len(value) <= 64
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(module.json, "dumps", bounded_dumps)
    result = capture([{"role": "user", "content": "PRIVATE" * 1000000}])
    assert result["capture_reason"] == "byte_limit"
    assert result["encoded_bytes"] <= 256 * 1024
    assert len(result["blocks"]) <= 1024
    assert result["expected_item_count"] is None
    assert result["occurrences"][0]["content_digest"] is None
    assert max(largest) <= 64


@pytest.mark.parametrize("limit", [-3, 0, 1, 2, 100000, "bad", True])
def test_item_limit_never_expands_fixed_budget(limit):
    config = replace(CONFIG, max_items=limit)
    result = capture([{"role": "user", "content": ""}] * 3000, config=config)
    effective = max(1, min(limit, 2000)) if type(limit) is int else 2000
    assert result["capture_reason"] == "item_limit"
    assert len(result["occurrences"]) == effective
    assert result["expected_item_count"] is None


def test_cooperative_clock_checked_before_items_and_each_block(monkeypatch):
    calls = []

    def clock():
        calls.append(None)
        return 0.0 if len(calls) < 8 else 0.006

    monkeypatch.setattr(implementation(), "monotonic", clock)
    result = capture([{"role": "user", "content": "x" * 100000}])
    assert result["capture_reason"] == "time_budget"
    assert result["encoded_bytes"] <= 6 * 256
    assert result["expected_item_count"] is None
    assert result["occurrences"][0]["content_digest"] is None


def test_empty_context_is_complete_without_synthetic_blocks():
    result = capture([])
    assert result["capture_state"] == "complete"
    assert result["expected_item_count"] == 0
    assert result["text_bytes"] == 0
    assert result["blocks"] == []


def test_nested_unknown_modality_is_partial():
    block = {
        "type": "tool_result",
        "content": [
            {"type": "future_modality", "payload": "PRIVATE"},
        ],
    }
    result = capture([{"role": "user", "content": [block]}])
    assert result["capture_reason"] == "unsupported_content"
    assert result["occurrences"][0]["content_digest"] is None


@pytest.mark.parametrize(
    "kind,id_key",
    [
        ("tool_use", "id"),
        ("tool_result", "tool_use_id"),
    ],
)
def test_tool_operation_ids_do_not_change_content_or_prefix(kind, id_key):
    block = {"type": kind, id_key: "op-a", "content": "text"}
    message = {"role": "assistant", "content": [block]}
    before = capture([message])
    block[id_key] = "op-b"
    after = capture([message])
    assert digests(before) == digests(after)
    assert before["occurrences"][0]["content_digest"] == (
        after["occurrences"][0]["content_digest"]
    )


def test_empty_message_blocks_still_preserve_role():
    result = capture([{"role": "user", "content": []}])
    assert len(result["occurrences"]) == 1
    assert digests(result) != digests(
        capture([{"role": "assistant", "content": []}]),
    )


def test_openai_call_ids_are_operation_metadata():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "op-a",
                "type": "function",
                "function": {
                    "name": "calculate",
                    "arguments": "PRIVATE",
                },
            },
        ],
    }
    before = capture([message])
    message["tool_calls"][0]["id"] = "op-b"
    assert digests(before) == digests(capture([message]))
    assert "PRIVATE" not in repr(before)


def test_source_identity_matches_single_text_block_in_message():
    from swe.token_diagnostics.provenance import register_source

    message = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    with register_source(
        message,
        ("source", "tenant"),
        CONFIG,
        source_object_id="id",
        source_version="v1",
        category="memory",
    ):
        result = capture([message])
    assert result["occurrences"][0]["source_object_id"] == "id"


def test_huge_dictionary_keys_stop_before_sorting(monkeypatch):
    import builtins

    def bounded_sort(keys):
        assert sum(len(key) for key in keys) <= 256 * 1024
        return builtins.sorted(keys)

    monkeypatch.setattr(
        implementation(),
        "sorted",
        bounded_sort,
        raising=False,
    )
    tool = {"x" * 300000: "PRIVATE"}
    result = capture([], [tool])
    assert result["capture_reason"] == "byte_limit"
    assert result["occurrences"][0]["content_digest"] is None


def test_source_registration_finally_releases_borrowed_objects():
    import sys
    from swe.token_diagnostics.provenance import register_source, _sources

    source = {"role": "user", "content": "same"}
    references = sys.getrefcount(source)
    with pytest.raises(RuntimeError):
        with register_source(
            source,
            ("source", "tenant"),
            CONFIG,
            source_object_id="id",
            source_version="v1",
            category="memory",
        ):
            assert _sources.get().borrow.identity is source
            raise RuntimeError("controlled")
    assert _sources.get() is None
    assert sys.getrefcount(source) == references


def test_item_budget_stops_without_reading_another_message():
    def messages():
        yield {"role": "user", "content": "one"}
        yield {"role": "user", "content": "two"}
        raise AssertionError("Capture read past its item budget")

    result = capture(messages(), config=replace(CONFIG, max_items=2))
    assert result["capture_reason"] == "item_limit"
    assert len(result["occurrences"]) == 2


@pytest.mark.parametrize("content", [42, {"type": "text", "text": {}}])
def test_unsupported_message_text_is_not_complete(content):
    result = capture([{"role": "user", "content": content}])
    assert result["capture_state"] == "partial"
    assert result["capture_reason"] == "unsupported_content"


def test_source_registration_keeps_tool_schema_payload_type_and_scope():
    from swe.token_diagnostics.provenance import register_source

    schema = {"type": "text", "description": "schema payload"}
    with register_source(
        schema,
        ("source", "tenant"),
        CONFIG,
        source_object_id="tool-schema",
        source_version="v1",
        category="tools",
    ):
        result = capture([], [schema])
    assert result["occurrences"][0]["source_object_id"] == "tool-schema"


@pytest.mark.parametrize("scope", [None, "source:tenant", ("", "t"), (1, "t")])
def test_malformed_scope_is_unavailable_without_private_errors(scope):
    result = capture([{"role": "user", "content": "PRIVATE"}], scope=scope)
    assert result["capture_state"] == "unavailable"
    assert result["capture_reason"] == "capture_error"
    assert "PRIVATE" not in repr(result)


def test_unexpected_capture_error_fails_open_without_logging(
    monkeypatch,
    caplog,
):
    def failed(*args, **kwargs):
        raise AttributeError("PRIVATE exception payload")

    monkeypatch.setattr(implementation(), "_canonical_parts", failed)
    result = capture([{"role": "user", "content": "PRIVATE"}])
    assert result["capture_reason"] == "capture_error"
    assert "PRIVATE" not in repr(result)
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("role", ["bad", [], {}])
def test_invalid_context_role_is_rejected_as_caller_contract(role):
    with pytest.raises(ValueError, match="Unsupported context role"):
        implementation().capture_context(
            [],
            [],
            ("source", "tenant"),
            CONFIG,
            context_id="ctx",
            context_role=role,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_object_id", "PRIVATE" * 100),
        ("source_version", None),
        ("category", "PRIVATE"),
    ],
)
def test_invalid_explicit_source_metadata_not_persisted(field, value):
    from swe.token_diagnostics.provenance import register_source

    fields = {
        "source_object_id": "id",
        "source_version": "v1",
        "category": "memory",
    }
    fields[field] = value
    message = {"role": "user", "content": "same"}
    with register_source(message, ("source", "tenant"), CONFIG, **fields):
        result = capture([message])
    assert result["occurrences"][0]["source_object_id"] is None
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize(
    "divisor",
    [0, -1, False, "PRIVATE", float("nan"), float("inf"), 1e-100],
)
def test_invalid_divisor_is_unavailable_without_unsafe_intervals(divisor):
    result = capture(
        [{"role": "user", "content": "same"}],
        config=replace(CONFIG, divisor=divisor),
    )
    assert result["capture_state"] == "unavailable"
    assert result["capture_reason"] == "capture_error"
    assert result["occurrences"] == []
    assert "PRIVATE" not in repr(result)


def test_source_metadata_returned_to_callers_does_not_mutate_sidecar():
    from swe.token_diagnostics.provenance import register_source, find_source

    message = {"role": "user", "content": "same"}
    with register_source(
        message,
        ("source", "tenant"),
        CONFIG,
        source_object_id="id",
        source_version="v1",
        category="memory",
    ):
        digest = capture([message])["occurrences"][0]["content_digest"]
        found = find_source(message, digest, ("source", "tenant"), "v1")
        found["category"] = "PRIVATE"
        result = capture([message])
    assert result["occurrences"][0]["category"] == "memory"
    assert "PRIVATE" not in repr(result)


@pytest.mark.asyncio
async def test_inherited_source_expires_when_parent_registration_exits():
    import asyncio
    from swe.token_diagnostics.provenance import register_source

    message = {"role": "user", "content": "same"}
    started, release = asyncio.Event(), asyncio.Event()

    async def child():
        started.set()
        await release.wait()
        return capture([message])

    with register_source(
        message,
        ("source", "tenant"),
        CONFIG,
        source_object_id="id",
        source_version="v1",
        category="memory",
    ):
        task = asyncio.create_task(child())
        await started.wait()
        assert capture([message])["occurrences"][0]["source_object_id"] == "id"
    release.set()
    result = await task
    assert result["occurrences"][0]["source_object_id"] is None


@pytest.mark.asyncio
async def test_inherited_context_does_not_pin_borrowed_raw_objects():
    import asyncio
    import weakref
    from swe.token_diagnostics.provenance import register_source

    class Payload:
        pass

    payload = Payload()
    reference = weakref.ref(payload)
    message = {"role": "user", "content": "same", "opaque": payload}
    del payload
    started, release = asyncio.Event(), asyncio.Event()

    async def child():
        started.set()
        await release.wait()

    with register_source(
        message,
        ("source", "tenant"),
        CONFIG,
        source_object_id="id",
        source_version="v1",
        category="memory",
    ):
        task = asyncio.create_task(child())
        await started.wait()
    del message
    try:
        assert reference() is None
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_expired_nested_registration_reveals_active_parent_in_child():
    import asyncio
    from swe.token_diagnostics.provenance import register_source

    message = {"role": "user", "content": "same"}
    started, release = asyncio.Event(), asyncio.Event()

    async def child():
        started.set()
        await release.wait()
        return capture([message])

    with register_source(
        message,
        ("source", "tenant"),
        CONFIG,
        source_object_id="outer",
        source_version="v1",
        category="memory",
    ):
        with pytest.raises(RuntimeError):
            with register_source(
                message,
                ("source", "tenant"),
                CONFIG,
                source_object_id="inner",
                source_version="v2",
                category="skill",
            ):
                task = asyncio.create_task(child())
                await started.wait()
                raise RuntimeError("controlled")
        release.set()
        result = await task
        assert result["occurrences"][0]["source_object_id"] == "outer"
