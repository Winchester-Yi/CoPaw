# -*- coding: utf-8 -*-
"""Bounded recovery envelopes preserve exact occurrence order and unknown state."""

import json

import pytest

from swe.app.conversation_storage.session_projection import (
    project_state,
    hydrate_state,
)


def message(key, content):
    return {
        "id": key,
        "role": "user",
        "content": content,
        "metadata": {"unknown": [1]},
    }


def test_projection_reuses_exact_occurrences_and_keeps_duplicate_source_ids():
    first, second = message("q", "one"), message("q", "two")
    state = {
        "revision": 7,
        "agent": {
            "memory": {
                "content": [[first, ["x"]], [second, []]],
                "_compressed_summary": "summary",
            },
        },
        "future": {"unknown": True},
    }
    refs = iter(
        [
            {
                "publication_id": 1,
                "message_id": 11,
                "offset": 10,
                "length": 20,
            },
            {
                "publication_id": 2,
                "message_id": 12,
                "offset": 30,
                "length": 40,
            },
        ],
    )
    projection = project_state(
        state,
        operation_key="a" * 64,
        resolve_message=lambda layer, body: next(refs),
    )
    assert not projection.blocks
    assert projection.dependency_ids == (1, 2)
    assert projection.envelope["state"]["agent"]["memory"]["content"] == []
    payloads = {11: first, 12: second}
    restored = hydrate_state(
        projection.envelope,
        lambda ref: payloads[ref["message_id"]],
    )
    assert restored == state
    assert restored is not state


def test_new_and_changed_messages_write_only_delta_and_pruning_drops_refs():
    old, new = message("q", "old"), message("q", "changed")

    def resolve(layer, body):
        return (
            {"publication_id": 1, "message_id": 11, "offset": 10, "length": 20}
            if body == old
            else None
        )

    full = project_state(
        {"agent": {"memory": {"content": [[old, []], [new, []]]}}},
        operation_key="a" * 64,
        resolve_message=resolve,
    )
    assert len(full.blocks) == 1
    assert json.loads(full.blocks[0].content) == new
    pruned = project_state(
        {"agent": {"memory": {"content": [[new, []]]}}},
        operation_key="b" * 64,
        resolve_message=resolve,
    )
    assert pruned.dependency_ids == ()
    assert len(pruned.envelope["lists"][0]["entries"]) == 1


def test_projection_preserves_unknown_modules_without_memory_and_task_error_shapes():
    state = {
        "revision": 2,
        "unknown": {"body": "keep"},
        "task_messages": [message("t", "task")],
        "model_call_failed_messages": [
            {"id": "e", "message": "error", "code": "model_call_failed"},
        ],
    }
    projection = project_state(state, operation_key="a" * 64)
    blocks = {
        block.name: json.loads(block.content) for block in projection.blocks
    }
    assert (
        hydrate_state(
            projection.envelope,
            lambda ref: blocks[ref["block_name"]],
        )
        == state
    )
    assert "agent" not in projection.envelope["state"]


def test_identical_raw_occurrences_still_have_distinct_new_blocks():
    raw = message("q", "same")
    result = project_state(
        {"agent": {"memory": {"content": [[raw, []], [raw, []]]}}},
        operation_key="a" * 64,
    )
    assert len(result.blocks) == 2
    assert (
        len(
            {
                item["ref"]["block_name"]
                for item in result.envelope["lists"][0]["entries"]
            },
        )
        == 2
    )


def test_active_and_modified_terminal_turns_remain_inline():
    state = {
        "turn_states": {
            "active": {"status": "running", "message": message("q", "one")},
            "changed": {
                "status": "completed",
                "message": message("q", "changed"),
            },
        },
    }
    result = project_state(
        state,
        operation_key="a" * 64,
        resolve_turn=lambda key, body: None,
    )
    assert result.envelope["state"]["turn_states"] == state["turn_states"]
    assert not result.envelope["turns"]


def test_frozen_terminal_turn_references_are_direct_and_hydrate_exactly():
    turn = {
        "status": "completed",
        "message": message("q", "one"),
        "unknown": {"keep": True},
    }
    state = {"turn_states": {"q": turn}}
    ref = {
        "publication_id": 3,
        "operation_key": "b" * 64,
        "block_name": "turn:q",
    }
    result = project_state(
        state,
        operation_key="a" * 64,
        resolve_turn=lambda key, body: ref,
    )
    assert result.dependency_ids == (3,)
    assert result.envelope["state"]["turn_states"] == {}
    assert hydrate_state(result.envelope, lambda value: turn) == state


def test_unknown_envelope_version_fails_closed():
    from swe.app.conversation_storage.errors import IntegrityError

    with pytest.raises(IntegrityError):
        hydrate_state(
            {"format": "swe-recovery-projection", "version": 99},
            lambda ref: None,
        )


def test_projection_rejects_unknown_paths_and_duplicate_inline_turn_state():
    from swe.app.conversation_storage.errors import IntegrityError

    value = {
        "format": "swe-recovery-projection",
        "version": 1,
        "state": {"unknown": []},
        "lists": [{"path": ["unknown"], "entries": []}],
        "turns": [],
    }
    with pytest.raises(IntegrityError, match="path"):
        hydrate_state(value, lambda ref: {})
    value = {
        "format": "swe-recovery-projection",
        "version": 1,
        "state": {"turn_states": {"q": {}}},
        "lists": [],
        "turns": [{"key": "q", "ref": {"publication_id": 1}}],
    }
    with pytest.raises(IntegrityError, match="duplicates"):
        hydrate_state(value, lambda ref: {})


def test_request_projection_cache_is_revision_scoped_and_detached():
    from swe.app.conversation_storage.projection_cache import (
        cache_scope,
        cached,
        remember,
    )

    identity = ("t", "s", "a", 1, 4)
    value = {("memory", "body"): [{"message_id": 11}]}
    with cache_scope():
        remember(identity, value)
        value[("memory", "body")][0]["message_id"] = 99
        assert cached(identity)[("memory", "body")][0]["message_id"] == 11
        assert cached(("t", "s", "a", 1, 5)) is None
    assert cached(identity) is None
