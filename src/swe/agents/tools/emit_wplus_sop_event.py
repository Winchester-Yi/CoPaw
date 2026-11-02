# -*- coding: utf-8 -*-
"""Commit one typed W+ SOP event from trusted Agent-turn context."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...app.agent_context import get_current_agent_id
from ...app.wplus_sop.models import OwnershipTuple
from ...app.wplus_sop.runtime_context import get_current_wplus_runtime
from ...app.wplus_sop.service import (
    WPlusSopService,
    serialize_session,
    store_path_for_workspace,
)
from ...app.wplus_sop.store import WPlusSopStore
from ...config.context import (
    get_current_source_id,
    get_current_task_progress_chat_id,
    get_current_task_progress_tracker,
    get_current_tenant_id,
    get_current_user_id,
    get_current_workspace_dir,
)


def _response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            ),
        ],
    )


async def emit_wplus_sop_event(
    kind: str,
    payload: dict[str, Any],
    event_key: str,
) -> ToolResponse:
    """提交当前 W+ SOP 会话的结构化业务事件。

    只能在 CoPaw 已创建并绑定 W+ SOP Session 的 Agent 回合中调用。`kind`
    必须是协议事件类型，`payload` 必须满足对应 schema，`event_key` 必须在
    同一业务事件的重发中保持稳定。租户、来源、用户、Agent、Chat 和 Session
    归属全部由受信任运行时上下文解析，不能作为参数覆盖。
    """
    workspace_dir = get_current_workspace_dir()
    tenant_id = get_current_tenant_id()
    source_id = get_current_source_id()
    user_id = get_current_user_id()
    chat_id = get_current_task_progress_chat_id()
    agent_id = get_current_agent_id(tenant_id)
    trusted_runtime = get_current_wplus_runtime()
    if not all(
        (
            workspace_dir,
            tenant_id,
            source_id,
            user_id,
            chat_id,
            agent_id,
            event_key.strip(),
            trusted_runtime,
        ),
    ):
        return _response(
            {
                "ok": False,
                "reason": "trusted W+ SOP runtime context unavailable",
            },
        )

    assert tenant_id is not None
    assert source_id is not None
    assert agent_id is not None
    store = WPlusSopStore(store_path_for_workspace(workspace_dir))
    assert trusted_runtime is not None
    record = store.get_session(trusted_runtime.sop_session_id)
    if (
        record is None
        or not record.projection.holds_chat_slot
        or (
            record.projection.ownership.tenant_id,
            record.projection.ownership.source_id,
            record.projection.ownership.user_id,
            record.projection.ownership.agent_id,
            record.projection.ownership.chat_id,
        )
        != (tenant_id, source_id, user_id, agent_id, chat_id)
    ):
        return _response(
            {
                "ok": False,
                "reason": "the trusted W+ SOP run is not active for this Chat",
            },
        )

    ownership = OwnershipTuple.model_validate(
        record.projection.ownership,
    )
    from ...app.conversation_storage.runtime import workspace_storage
    from ...app.runner.manager import ChatManager

    storage = workspace_storage(
        SimpleNamespace(
            workspace_dir=workspace_dir,
            agent_id=agent_id,
            tenant_id=tenant_id,
            source_id=source_id,
        ),
    )
    chats_path = Path(workspace_dir) / "chats.json"
    chat_manager = None
    if storage is not None:
        chat_manager = ChatManager(repo=storage.chats)
    elif chats_path.is_file():
        from ...app.runner.repo.json_repo import JsonChatRepository

        chat_manager = ChatManager(
            repo=JsonChatRepository(str(chats_path)),
        )
    service = WPlusSopService(
        workspace=SimpleNamespace(
            workspace_dir=workspace_dir,
            chat_manager=chat_manager,
        ),
        ownership=ownership,
        store=store,
    )
    try:
        mutation = service.append_agent_event(
            kind=kind,
            payload=payload,
            event_key=event_key,
            trusted_sop_session_id=trusted_runtime.sop_session_id,
            trusted_run_id=trusted_runtime.run_id,
            trusted_attempt_id=trusted_runtime.attempt_id,
        )
    except Exception as exc:  # normalized into a tool-visible structured error
        return _response(
            {
                "ok": False,
                "reason": str(exc),
            },
        )
    projected = await service.flush_chat_projection_outbox()
    if mutation.record.projection.state.value in {"Paused", "Terminated"}:
        tracker = get_current_task_progress_tracker()
        if tracker is not None:
            loop = asyncio.get_running_loop()
            loop.call_soon(
                asyncio.create_task,
                tracker.request_stop(chat_id),
            )
    return _response(
        {
            "ok": True,
            "duplicate": mutation.duplicate,
            "chat_projection_events": projected,
            "session": serialize_session(mutation.record),
        },
    )
