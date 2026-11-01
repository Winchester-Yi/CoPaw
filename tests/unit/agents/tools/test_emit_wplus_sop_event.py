# -*- coding: utf-8 -*-
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.agents.tools.emit_wplus_sop_event import emit_wplus_sop_event
from swe.app.agent_context import set_current_agent_id
from swe.app.wplus_sop.models import (
    CommandReceipt,
    OwnershipTuple,
    RunAttempt,
    RunStatus,
    SessionProjection,
    SessionState,
)
from swe.app.wplus_sop.runtime_context import (
    WPlusRuntimeContext,
    bind_wplus_runtime,
)
from swe.app.wplus_sop.service import store_path_for_workspace
from swe.app.wplus_sop.store import WPlusSopStore
from swe.config.context import (
    reset_current_task_progress_chat_id,
    set_current_task_progress_chat_id,
    tenant_context,
)


def _tool_payload(response):
    return json.loads(response.content[0]["text"])


@pytest.mark.asyncio
async def test_event_tool_fails_closed_without_agent_turn_context():
    response = await emit_wplus_sop_event(
        kind="stage_proposal",
        payload={"stages": []},
        event_key="stage-proposal-v1",
    )

    assert _tool_payload(response)["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_storage", [False, True])
async def test_event_tool_appends_to_the_owned_precreated_session(
    tmp_path: Path,
    monkeypatch,
    sql_storage,
):
    ownership = OwnershipTuple(
        tenant_id="tenant-1",
        source_id="console",
        user_id="user-1",
        agent_id="agent-1",
        chat_id="chat-1",
        logical_chat_session_id="logical-1",
    )
    store = WPlusSopStore(store_path_for_workspace(tmp_path))
    store.create_session(
        SessionProjection(
            sop_session_id="sop-1",
            ownership=ownership,
            skill_snapshot_id="sha256:miner",
            state=SessionState.GENERATING_STAGE_PROPOSAL,
            state_version=1,
            title="SOP",
            current_run_id="run-1",
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-entry",
            command="confirm_entry",
            sop_session_id="sop-1",
            resulting_state_version=1,
        ),
        run_attempt=RunAttempt(
            run_id="run-1",
            attempt_id="attempt-1",
            command_request_id="cmd-entry",
            command="confirm_entry",
            status=RunStatus.CLAIMED,
        ),
    )
    if sql_storage:
        from swe.app.runner.models import ChatSpec

        chat = ChatSpec(id="chat-1", session_id="logical-1", user_id="user-1")
        repository = SimpleNamespace(
            get_chat=AsyncMock(return_value=chat),
            update_chat_if_exists=AsyncMock(side_effect=lambda value: value),
        )
        monkeypatch.setattr(
            "swe.app.conversation_storage.runtime.workspace_storage",
            lambda workspace: SimpleNamespace(chats=repository),
        )
        # The indexed path must work even when the old registry is absent.
        assert not (tmp_path / "chats.json").exists()
    set_current_agent_id("agent-1")
    chat_token = set_current_task_progress_chat_id("chat-1")
    try:
        with tenant_context(
            tenant_id="tenant-1",
            source_id="console",
            user_id="user-1",
            workspace_dir=tmp_path,
        ):
            with bind_wplus_runtime(
                WPlusRuntimeContext(
                    sop_session_id="sop-1",
                    run_id="run-1",
                    attempt_id="attempt-1",
                    command="confirm_entry",
                ),
            ):
                response = await emit_wplus_sop_event(
                    kind="stage_proposal",
                    payload={
                        "stages": [
                            {"stage_id": "stage-1", "name": "确认范围"},
                            {"stage_id": "stage-2", "name": "生成结果"},
                        ],
                    },
                    event_key="stage-proposal-v1",
                )
    finally:
        reset_current_task_progress_chat_id(chat_token)

    body = _tool_payload(response)
    assert body["ok"] is True
    assert body["session"]["state"] == "AwaitingQueueConfirmation"
    if sql_storage:
        assert body["chat_projection_events"] > 0
        repository.update_chat_if_exists.assert_awaited()
        assert not (tmp_path / "chats.json").exists()
    assert (
        WPlusSopStore(
            store_path_for_workspace(tmp_path),
        )
        .get_session("sop-1")
        .projection.state_version
        == 2
    )
