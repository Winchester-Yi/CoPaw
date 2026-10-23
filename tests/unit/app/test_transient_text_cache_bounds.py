# -*- coding: utf-8 -*-
"""Real store regressions, loaded without optional HTTP/model dependencies."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_store(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def push(monkeypatch):
    module = load_store(
        "bounded_push_test",
        "src/swe/app/console_push_store.py",
    )
    monkeypatch.setattr(
        module,
        "_resolve_store_key",
        lambda key=None: key or "default",
    )
    return module


@pytest.fixture
def suggestions(monkeypatch):
    module = load_store(
        "bounded_suggestions_test",
        "src/swe/app/suggestions/store.py",
    )
    monkeypatch.setattr(
        module,
        "_resolve_scope_key",
        lambda key=None: key or "default",
    )
    return module


def clock(monkeypatch, module):
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_push_reads_do_not_create_keys(push):
    assert await push.take("missing") == []
    assert await push.take_all("absent") == []
    assert await push.get_recent(tenant_id="empty") == []
    assert push.get_stats()["tenant_count"] == 0


@pytest.mark.asyncio
async def test_push_monotonic_ttl_sticky_and_dedup(push, monkeypatch):
    now = clock(monkeypatch, push)
    assert await push.append("s", "old", sticky=True, delivery_key="d") is True
    now[0] += 30
    assert await push.append("s", "repeat", delivery_key="d") is True
    assert [m["text"] for m in await push.get_recent(120)] == ["old"]
    now[0] += 31
    assert await push.get_recent(120) == []
    assert push.get_stats()["entries"] == 0
    assert push.get_stats()["expired_total"] == 1
    assert await push.append("s", "new", delivery_key="d") is True
    assert [m["text"] for m in await push.take("s")] == ["new"]
    assert push.get_stats()["payload_bytes"] == 0


@pytest.mark.asyncio
async def test_push_utf8_and_key_budget_rejection(push, monkeypatch):
    monkeypatch.setattr(push, "_MAX_ENTRY_BYTES", 100)
    assert await push.append("s", "中" * 30) is False
    assert await push.append("s" * 101, "x") is False
    assert await push.append("s", "x", delivery_key="d" * 101) is False
    assert push.get_stats()["entries"] == 0
    assert push.get_stats()["rejected_total"] == 3


@pytest.mark.asyncio
async def test_push_scope_then_global_write_age_eviction(push, monkeypatch):
    monkeypatch.setattr(push, "_MAX_MESSAGES", 2)
    monkeypatch.setattr(push, "_MAX_TOTAL_MESSAGES", 3)
    for scope, text in [("a", "a1"), ("b", "b1"), ("a", "a2")]:
        await push.append("s", text, tenant_id=scope)
    await push.get_recent(tenant_id="a")
    await push.append("s", "a3", tenant_id="a")
    assert [m["text"] for m in await push.get_recent(tenant_id="a")] == [
        "a2",
        "a3",
    ]
    await push.append("s", "c1", tenant_id="c")
    assert await push.take_all("b") == []
    assert push.get_stats()["entries"] == 3
    assert push.get_stats()["evicted_total"] == 2
    await push.reset()
    assert (
        push.get_stats()["entries"] == push.get_stats()["payload_bytes"] == 0
    )


@pytest.mark.asyncio
async def test_suggestions_expire_on_peek_and_take(suggestions, monkeypatch):
    now = clock(monkeypatch, suggestions)
    await suggestions.store_suggestions("peek", ["a"])
    await suggestions.store_suggestions("take", ["b"])
    now[0] += 61
    assert await suggestions.peek_suggestions("peek") == []
    assert await suggestions.take_suggestions("take") == []
    assert suggestions.get_stats()["session_count"] == 0


@pytest.mark.asyncio
async def test_suggestions_own_copy_first_ten_and_utf8_budget(
    suggestions,
    monkeypatch,
):
    values = [str(i) for i in range(12)]
    await suggestions.store_suggestions("s", values)
    values.append("bypass")
    result = await suggestions.peek_suggestions("s")
    assert result[0]["suggestions"] == [str(i) for i in range(10)]
    result[0]["suggestions"].append("also bypass")
    assert (
        len((await suggestions.peek_suggestions("s"))[0]["suggestions"]) == 10
    )
    old_bytes = suggestions.get_stats()["payload_bytes"]
    monkeypatch.setattr(suggestions, "_MAX_ENTRY_BYTES", 100)
    await suggestions.store_suggestions("s", ["中" * 40])
    assert suggestions.get_stats()["payload_bytes"] == old_bytes
    assert suggestions.get_stats()["rejected_total"] == 1
    await suggestions.take_suggestions("s")
    assert suggestions.get_stats()["payload_bytes"] == 0


@pytest.mark.asyncio
async def test_suggestions_overwrite_eviction_and_counts(
    suggestions,
    monkeypatch,
):
    monkeypatch.setattr(suggestions, "_MAX_SCOPE_ENTRIES", 2)
    monkeypatch.setattr(suggestions, "_MAX_TOTAL_ENTRIES", 3)
    await suggestions.store_suggestions("s1", ["old"], "a")
    await suggestions.store_suggestions("s2", ["two"], "a")
    await suggestions.store_suggestions("s1", ["new"], "a")
    await suggestions.store_suggestions("s3", ["three"], "a")
    assert await suggestions.peek_suggestions("s2", "a") == []
    await suggestions.store_suggestions("s4", ["four"], "b")
    await suggestions.store_suggestions("s5", ["five"], "c")
    assert await suggestions.peek_suggestions("s1", "a") == []
    assert suggestions.get_stats()["entries"] == 3
    await suggestions.reset()
    assert suggestions.get_stats()["payload_bytes"] == 0


@pytest.mark.asyncio
async def test_qa_write_ttl_cannot_be_extended_by_reads_or_later_writes(
    suggestions,
    monkeypatch,
):
    now = clock(monkeypatch, suggestions)
    await suggestions.store_qa_content(
        "chat",
        "short",
        "answer",
        max_age_seconds=5,
    )
    now[0] += 6
    await suggestions.store_qa_content(
        "chat",
        "long",
        "answer",
        max_age_seconds=300,
    )
    assert (
        await suggestions.get_qa_content("chat", "short", max_age_seconds=999)
        is None
    )
    now[0] += 200
    assert (
        await suggestions.get_qa_content("chat", "long", max_age_seconds=999)
        is not None
    )
    assert (
        await suggestions.get_qa_content("chat", "long", max_age_seconds=100)
        is None
    )
    assert suggestions.get_stats()["qa"]["entries"] == 0


@pytest.mark.asyncio
async def test_qa_limits_overwrite_and_utf8(suggestions, monkeypatch):
    monkeypatch.setattr(suggestions, "_QA_MAX_CHAT_ENTRIES", 2)
    monkeypatch.setattr(suggestions, "_QA_MAX_SCOPE_ENTRIES", 3)
    monkeypatch.setattr(suggestions, "_QA_MAX_TOTAL_ENTRIES", 4)
    for question in ["q1", "q2", "q1", "q3"]:
        await suggestions.store_qa_content("c", question, "中", "a")
    assert await suggestions.get_qa_content("c", "q2", "a") is None
    await suggestions.store_qa_content("d", "q4", "answer", "a")
    await suggestions.store_qa_content("e", "q5", "answer", "a")
    assert await suggestions.get_qa_content("c", "q1", "a") is None
    await suggestions.store_qa_content("c", "q6", "answer", "b")
    await suggestions.store_qa_content("c", "q7", "answer", "c")
    assert suggestions.get_stats()["qa"]["entries"] == 4
    monkeypatch.setattr(suggestions, "_QA_MAX_ENTRY_BYTES", 100)
    await suggestions.store_qa_content("c", "oversize", "中" * 40, "a")
    assert suggestions.get_stats()["qa"]["rejected_total"] == 1
    assert await suggestions.get_qa_content("c", "oversize", "a") is None


@pytest.mark.asyncio
async def test_active_prune_reclaims_unpolled_keys_and_yields(
    push,
    suggestions,
    monkeypatch,
):
    now = clock(monkeypatch, push)
    for i in range(300):
        await push.append(str(i), "text", tenant_id=str(i))
        await suggestions.store_suggestions(str(i), ["text"], str(i))
        await suggestions.store_qa_content(str(i), "q", "a", str(i))
    now[0] += 121
    turns = []

    async def observe():
        turns.append("ran")

    observer = asyncio.create_task(observe())
    await push.prune_expired()
    await suggestions.prune_expired()
    await observer
    assert turns == ["ran"]
    assert push.get_stats()["entries"] == push.get_stats()["scopes"] == 0
    assert (
        suggestions.get_stats()["entries"]
        == suggestions.get_stats()["scopes"]
        == 0
    )
    assert suggestions.get_stats()["qa"]["entries"] == 0
    assert not push._write_order
    assert not suggestions._suggestion_order
    assert not suggestions._qa_order


@pytest.mark.asyncio
async def test_byte_limits_and_accounting_across_eviction(
    push,
    suggestions,
    monkeypatch,
):
    monkeypatch.setattr(push, "_MAX_SCOPE_BYTES", 220)
    monkeypatch.setattr(push, "_MAX_TOTAL_BYTES", 350)
    monkeypatch.setattr(suggestions, "_MAX_SCOPE_BYTES", 220)
    monkeypatch.setattr(suggestions, "_MAX_TOTAL_BYTES", 350)
    monkeypatch.setattr(suggestions, "_QA_MAX_SCOPE_BYTES", 220)
    monkeypatch.setattr(suggestions, "_QA_MAX_TOTAL_BYTES", 350)
    for scope in ["a", "a", "b", "c"]:
        await push.append("s", "x" * 80, tenant_id=scope)
        await suggestions.store_suggestions("s", ["x" * 80], scope)
        await suggestions.store_qa_content("c", "q", "x" * 80, scope)
    assert push.get_stats()["payload_bytes"] <= 350
    assert all(value <= 220 for value in push._scope_bytes.values())
    assert suggestions.get_stats()["payload_bytes"] <= 350
    assert all(
        value <= 220 for value in suggestions._suggestion_scope_bytes.values()
    )
    assert suggestions.get_stats()["qa"]["payload_bytes"] <= 350
    assert all(value <= 220 for value in suggestions._qa_scope_bytes.values())
    await push.reset()
    await suggestions.reset()
    assert not push._scope_bytes
    assert not suggestions._suggestion_scopes
    assert not suggestions._qa_scopes
    assert suggestions.get_stats()["qa"]["payload_bytes"] == 0


@pytest.mark.asyncio
async def test_giant_input_rejected_before_encoding(
    push,
    suggestions,
    monkeypatch,
):
    class MustNotEncode(str):
        def encode(self, *args, **kwargs):
            raise AssertionError(
                "giant strings must be checked before encoding",
            )

    giant = MustNotEncode("x" * 1000)
    monkeypatch.setattr(push, "_MAX_ENTRY_BYTES", 100)
    monkeypatch.setattr(suggestions, "_MAX_ENTRY_BYTES", 100)
    monkeypatch.setattr(suggestions, "_QA_MAX_ENTRY_BYTES", 100)
    assert await push.append("s", giant) is False
    await suggestions.store_suggestions("s", [giant])
    await suggestions.store_qa_content("c", "q", giant)
    assert suggestions.get_stats()["entries"] == 0
    assert suggestions.get_stats()["qa"]["entries"] == 0


@pytest.mark.asyncio
async def test_cleanup_preserves_concurrent_replacements(
    suggestions,
    monkeypatch,
):
    now = clock(monkeypatch, suggestions)
    for i in range(257):
        await suggestions.store_suggestions(str(i), ["old"], str(i))
        await suggestions.store_qa_content(str(i), "q", "old", str(i))
    now[0] += 121
    original_sleep = asyncio.sleep
    replaced = []

    async def replace_after_batch(delay):
        if not replaced:
            replaced.append(True)
            await suggestions.store_suggestions("256", ["new"], "256")
            await suggestions.store_qa_content("256", "q", "new", "256")
        await original_sleep(delay)

    monkeypatch.setattr(suggestions.asyncio, "sleep", replace_after_batch)
    await suggestions.prune_expired()
    assert (await suggestions.peek_suggestions("256", "256"))[0][
        "suggestions"
    ] == ["new"]
    assert (await suggestions.get_qa_content("256", "q", "256"))[
        "assistant_response"
    ] == "new"
    assert suggestions.get_stats()["entries"] == 1
    assert suggestions.get_stats()["qa"]["entries"] == 1


@pytest.mark.asyncio
async def test_fixed_workload_keeps_metadata_bounded(
    push,
    suggestions,
    monkeypatch,
):
    monkeypatch.setattr(push, "_MAX_TOTAL_MESSAGES", 20)
    monkeypatch.setattr(suggestions, "_MAX_TOTAL_ENTRIES", 20)
    monkeypatch.setattr(suggestions, "_QA_MAX_TOTAL_ENTRIES", 20)
    for i in range(1000):
        scope = str(i)
        await push.append("s", "text", tenant_id=scope)
        await suggestions.store_suggestions("s", ["text"], scope)
        await suggestions.store_qa_content("c", "q", "a", scope)
    assert (
        len(push._tenant_messages)
        == len(push._scope_bytes)
        == len(push._write_order)
        == 20
    )
    assert (
        len(suggestions._suggestion_scopes)
        == len(suggestions._suggestion_order)
        == 20
    )
    assert len(suggestions._qa_scopes) == len(suggestions._qa_order) == 20


@pytest.mark.asyncio
async def test_giant_keys_are_rejected_before_scope_resolution(
    push,
    suggestions,
    monkeypatch,
):
    def unexpected_resolution(*args):
        raise AssertionError(
            "oversized raw keys must not be decoded or encoded",
        )

    monkeypatch.setattr(push, "_MAX_ENTRY_BYTES", 100)
    monkeypatch.setattr(suggestions, "_MAX_ENTRY_BYTES", 100)
    monkeypatch.setattr(suggestions, "_QA_MAX_ENTRY_BYTES", 100)
    monkeypatch.setattr(push, "_resolve_store_key", unexpected_resolution)
    monkeypatch.setattr(
        suggestions,
        "_resolve_scope_key",
        unexpected_resolution,
    )
    giant = "x" * 1000
    assert await push.append("s", "x", tenant_id=giant) is False
    await suggestions.store_suggestions("s", ["x"], giant)
    await suggestions.store_qa_content("c", "q", "a", giant)
