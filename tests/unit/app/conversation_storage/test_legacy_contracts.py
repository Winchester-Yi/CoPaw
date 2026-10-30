# -*- coding: utf-8 -*-
from __future__ import annotations


def test_contract_fixture_preserves_full_identity_and_empty_user(
    conversation_contracts,
):
    cases = conversation_contracts["identity_cases"]

    assert len(set(cases)) == 5
    assert cases[0][3] == ""
    assert cases[1][3] == "alice"
    assert len({case[1] for case in cases}) > 1
    assert len({case[2] for case in cases}) > 1
    assert len({case[4] for case in cases}) > 1


def test_contract_fixture_keeps_multiple_chats_and_registry_ties(
    conversation_contracts,
):
    chats = conversation_contracts["chats"]

    assert [chat.id for chat in chats] == ["chat-first", "chat-second"]
    assert {chat.session_id for chat in chats} == {"logical-session"}
    assert chats[0].updated_at == chats[1].updated_at
    assert chats[0].meta == {"custom": {"enabled": True}}


def test_contract_fixture_keeps_duplicate_source_messages_and_unknown_modules(
    conversation_contracts,
):
    state = conversation_contracts["legacy_session_state"]

    assert [message["id"] for message in state["messages"]] == [
        "duplicate-source-id",
        "duplicate-source-id",
    ]
    assert state["custom_recovery_module"] == {"must_survive": True}
    assert (
        "turn_states"
        not in conversation_contracts["legacy_session_without_turn_states"]
    )


async def test_old_session_reader_preserves_duplicate_ids_and_unknown_modules(
    legacy_reader_contract,
):
    _, state = legacy_reader_contract

    assert [message["id"] for message in state["messages"]] == [
        "duplicate-source-id",
        "duplicate-source-id",
    ]
    assert state["custom_recovery_module"] == {"must_survive": True}


async def test_old_archive_reader_keeps_chat_boundary_and_message_order(
    legacy_archive_contract,
):
    boundary, page = legacy_archive_contract

    assert [message.id for message in page.messages] == [
        "archive-0",
        "archive-1",
    ]
    assert page.boundaries == [boundary]
