# -*- coding: utf-8 -*-
"""Actual serialized byte limits, including escaped text, before reservation."""

from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from swe.app.conversation_storage.bundle import new_operation_key
from swe.app.conversation_storage.commit_plan import CommitPlan, MAX_PLAN_BYTES
from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.message_installs import MessageInstall
from swe.app.conversation_storage.publications import (
    PublicationRequest,
    PublicationStore,
)


def encoded(plan):
    """The real format, without calling the writer whose boundary is tested."""
    return json.dumps(
        {
            "version": 1,
            "recovery_block": plan.recovery_block,
            "result_ids": plan.result_ids,
            "messages": [item.to_dict() for item in plan.messages],
        },
        separators=(",", ":"),
    )


def plan_with_bytes(size, character="a"):
    messages = tuple(
        MessageInstall(str(index), "memory", "user", source_msg_key="")
        for index in range(1024)
    )
    base = CommitPlan(messages=messages)
    remaining = size - len(encoded(base).encode("utf-8"))
    # Each source identity remains within its actual 1024 UTF-8 byte budget.
    unit = len(json.dumps(character)) - 2
    expanded = []
    for message in messages:
        count = min(1024, remaining)
        key = character * (count // unit) + "a" * (count % unit)
        expanded.append(replace(message, source_msg_key=key))
        remaining -= count
    assert remaining == 0
    plan = replace(base, messages=tuple(expanded))
    assert len(encoded(plan).encode("utf-8")) == size
    return plan


@pytest.mark.parametrize("character", ["a", "结", "😀", '"', "\\"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_actual_serialized_byte_boundary(character, offset):
    plan = plan_with_bytes(MAX_PLAN_BYTES + offset, character)
    if offset > 0:
        with pytest.raises(
            ValueError,
            match="commit plan.*byte|oversized commit plan",
        ):
            plan.serialize()
        with pytest.raises(IntegrityError):
            CommitPlan.parse(encoded(plan))
    else:
        text = plan.serialize()
        assert len(text.encode("utf-8")) == MAX_PLAN_BYTES + offset
        assert CommitPlan.parse(text) == plan


def test_legal_message_count_can_still_exceed_plan_bytes():
    plan = CommitPlan(
        messages=tuple(
            MessageInstall(
                f"source:{index}",
                "memory",
                "user",
                source_msg_key="q",
            )
            for index in range(4096)
        ),
    )
    assert len(encoded(plan).encode("utf-8")) > MAX_PLAN_BYTES
    with pytest.raises(
        ValueError,
        match="commit plan.*byte|oversized commit plan",
    ):
        plan.serialize()


async def test_oversized_plan_is_rejected_before_reserve_transaction_or_nas():
    database = Mock()
    database.transaction.side_effect = AssertionError(
        "reservation entered SQL",
    )
    bundles = Mock()
    bundles.write.side_effect = AssertionError("reservation entered NAS")
    scope = WorkspaceScope("t", "s", "a", service_unit_id="capacity-test")
    service = PublicationStore(
        database,
        bundles,
        service_unit_id=scope.service_unit_id,
    )
    request = PublicationRequest(
        new_operation_key(),
        scope,
        "state",
        "owner",
        1,
        session_id=1,
        chat_id=2,
        base_state_revision=0,
        epoch=1,
    )
    with pytest.raises(
        ValueError,
        match="commit plan.*byte|oversized commit plan",
    ):
        await service.reserve(request, plan_with_bytes(MAX_PLAN_BYTES + 1))
    database.transaction.assert_not_called()
    assert not database.mock_calls
    assert not bundles.mock_calls
