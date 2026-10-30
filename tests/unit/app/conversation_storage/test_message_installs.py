# -*- coding: utf-8 -*-
import pytest

from swe.app.conversation_storage.message_installs import (
    MessageInstall,
    TurnInstall,
    install_messages,
    install_turn,
)
from swe.app.conversation_storage.bundle import BlockExtent, BundleIndex
from swe.app.conversation_storage.errors import ConflictError, IntegrityError
from swe.app.conversation_storage.publications import CommitPlan
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_message_install_roundtrip_preserves_occurrence_and_empty_source_id():
    message = MessageInstall(
        "m:0",
        "memory",
        "assistant",
        source_msg_key="",
        source_occurrence_no=2,
        in_context=True,
    )
    assert MessageInstall.from_dict(message.to_dict()) == message
    assert message.source_msg_key == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"layer": "unknown"},
        {"role": "x" * 33},
        {"source_msg_key": "x" * 1025},
        {"source_occurrence_no": -1},
        {"in_context": 1},
        {"layer": "archive"},
        {"layer": "memory", "archive_chat_id": 1},
        {"block_name": "x" * 257},
    ],
)
def test_message_install_rejects_invalid_typed_metadata(changes):
    values = {"block_name": "m", "layer": "memory", "role": "user", **changes}
    with pytest.raises((ValueError, TypeError)):
        MessageInstall(**values)


def test_turn_install_requires_compatible_phase_and_status():
    assert TurnInstall.from_dict(
        TurnInstall("admission").to_dict(),
    ) == TurnInstall("admission")
    assert (
        TurnInstall("terminal", history_status="completed").history_status
        == "completed"
    )
    with pytest.raises(ValueError):
        TurnInstall("admission", history_status="completed")
    with pytest.raises(ValueError):
        TurnInstall("terminal", history_status="made-up")
    with pytest.raises(ValueError, match="identity"):
        TurnInstall("admission", execution_identity_text='{"prompt":"body"}')


async def test_message_occurrences_allocate_stable_orders_without_deduplicating_source_id():
    db = SimpleNamespace(
        insert=AsyncMock(return_value=1),
        execute=AsyncMock(return_value=1),
    )
    request = SimpleNamespace(session_id=1, chat_id=2, turn_id=3)
    index = BundleIndex(
        "bundle",
        100,
        {},
        {
            "m:0": BlockExtent("message", 50, 10),
            "m:1": BlockExtent("message", 60, 10),
        },
    )
    messages = (
        MessageInstall("m:0", "memory", "user", source_msg_key="same"),
        MessageInstall(
            "m:1",
            "memory",
            "assistant",
            source_msg_key="same",
            source_occurrence_no=1,
        ),
    )
    await install_messages(
        db,
        request,
        messages,
        {"id": 4},
        index,
        ({"history_high": 8}, {"history_high": 2}, None),
    )
    assert db.insert.await_count == 2
    assert db.insert.await_args_list[0].args[1][4] == 9
    assert db.insert.await_args_list[1].args[1][4] == 10
    assert db.execute.await_args_list[0].args[1] == (10, 1)


async def test_message_unknown_extent_prevents_any_reference_write():
    db = SimpleNamespace(insert=AsyncMock(), execute=AsyncMock())
    request = SimpleNamespace(session_id=1, chat_id=2, turn_id=None)
    with pytest.raises(IntegrityError, match="block"):
        await install_messages(
            db,
            request,
            (MessageInstall("missing", "memory", "user"),),
            {"id": 4},
            BundleIndex("bundle", 100, {}, {}),
            ({"history_high": 0}, {"history_high": 0}, None),
        )
    db.insert.assert_not_awaited()


async def test_terminal_stop_prevents_natural_completion_without_clearing_slot():
    db = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(session_id=1, chat_id=2, turn_id=3)
    with pytest.raises(ConflictError, match="Stop"):
        await install_turn(
            db,
            request,
            TurnInstall("terminal", history_status="completed"),
            {"id": 4},
            ({}, {}, {"state": "stopping"}),
        )
    db.execute.assert_not_awaited()


async def test_online_message_cannot_install_into_another_context_epoch():
    db = SimpleNamespace(insert=AsyncMock(), execute=AsyncMock())
    request = SimpleNamespace(session_id=1, chat_id=2, turn_id=None)
    message = MessageInstall("m", "memory", "assistant", context_epoch=1)
    index = BundleIndex(
        "bundle",
        100,
        {},
        {"m": BlockExtent("message", 50, 10)},
    )
    with pytest.raises(ConflictError, match="epoch"):
        await install_messages(
            db,
            request,
            (message,),
            {"id": 4},
            index,
            (
                {"history_high": 0, "context_epoch": 2},
                {"history_high": 0},
                None,
            ),
        )
    db.insert.assert_not_awaited()


def test_admission_plan_requires_one_durable_user_anchor():
    with pytest.raises(ValueError, match="anchor"):
        CommitPlan(
            recovery_block="root",
            turn_install=TurnInstall("admission"),
        ).serialize()
    with pytest.raises(ValueError, match="anchor"):
        CommitPlan(
            recovery_block="root",
            turn_install=TurnInstall("admission"),
            messages=(MessageInstall("m", "memory", "assistant"),),
        ).serialize()


async def test_archive_preserves_a_historical_turn_from_shared_session():
    db = SimpleNamespace(insert=AsyncMock(return_value=1), execute=AsyncMock())
    message = MessageInstall(
        "m",
        "archive",
        "user",
        archive_chat_id=2,
        turn_id=7,
    )
    await install_messages(
        db,
        SimpleNamespace(session_id=1, chat_id=2, turn_id=None),
        (message,),
        {"id": 4},
        BundleIndex("bundle", 100, {}, {"m": BlockExtent("archive", 50, 10)}),
        ({"history_high": 1}, {"history_high": 0}, None),
    )
    assert db.insert.await_args.args[1][6] == 7


async def test_online_occurrence_cannot_claim_another_turn():
    db = SimpleNamespace(insert=AsyncMock(), execute=AsyncMock())
    with pytest.raises(IntegrityError, match="Turn"):
        await install_messages(
            db,
            SimpleNamespace(session_id=1, chat_id=2, turn_id=3),
            (MessageInstall("m", "memory", "user", turn_id=7),),
            {"id": 4},
            BundleIndex(
                "bundle",
                100,
                {},
                {"m": BlockExtent("message", 50, 10)},
            ),
            ({"history_high": 0}, {"history_high": 0}, None),
        )
    db.insert.assert_not_awaited()


async def test_archived_occurrence_preserves_original_chat_of_historical_turn():
    db = SimpleNamespace(insert=AsyncMock(return_value=1), execute=AsyncMock())
    message = MessageInstall(
        "m",
        "archive",
        "user",
        archive_chat_id=2,
        origin_chat_id=5,
        turn_id=7,
    )
    await install_messages(
        db,
        SimpleNamespace(session_id=1, chat_id=2, turn_id=None),
        (message,),
        {"id": 4},
        BundleIndex("bundle", 100, {}, {"m": BlockExtent("archive", 50, 10)}),
        ({"history_high": 1}, {"history_high": 0}, None),
    )
    assert db.insert.await_args.args[1][1] == 5
