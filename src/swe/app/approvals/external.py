# -*- coding: utf-8 -*-
"""External approval submission helpers.

These helpers let another channel approve or deny an existing console
approval by submitting the same ``/approve`` or ``/deny`` command that the
console UI would send.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from agentscope_runtime.engine.schemas.agent_schemas import (
    ContentType,
    TextContent,
)

from .service import PendingApproval, get_approval_service
from ..answer_turn.models import TurnIdentity

logger = logging.getLogger(__name__)

CONSOLE_CHANNEL = "console"
ZHAOHU_CHANNEL = "zhaohu"
TOOL_GUARD_APPROVAL_KIND = "tool_guard"
APPROVAL_SOURCE_CHANNEL_META_KEY = "approval_source_channel"
APPROVAL_REQUEST_ID_META_KEY = "approval_request_id"
APPROVAL_DECISION_META_KEY = "approval_decision"
_RUN_IDLE_WAIT_SECONDS = 5.0
_RUN_IDLE_POLL_SECONDS = 0.05
_EXTERNAL_APPROVAL_MESSAGE_META_KEY = "external_approval_message"


class ExternalApprovalDecision(str, Enum):
    """Decision submitted by an external channel."""

    APPROVE = "approve"
    DENY = "deny"


@dataclass(frozen=True)
class ExternalApprovalSubmission:
    """Result of submitting an external approval decision."""

    request_id: str
    decision: ExternalApprovalDecision
    status: str
    session_id: str
    chat_id: str | None = None
    submitted: bool = False
    reconnect: bool = False
    is_new_run: bool = False


def _command_for_decision(
    decision: ExternalApprovalDecision,
    request_id: str,
) -> str:
    if decision == ExternalApprovalDecision.APPROVE:
        return f"/approve {request_id}"
    return f"/deny {request_id}"


def _status_for_decision(decision: ExternalApprovalDecision) -> str:
    if decision == ExternalApprovalDecision.APPROVE:
        return "approved"
    return "denied"


def _message_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _external_approval_memory_entry(
    *,
    pending: PendingApproval,
    command: str,
    decision: ExternalApprovalDecision,
    source_channel: str,
    source_user_id: str | None = None,
    source_message_id: str | None = None,
) -> list[Any]:
    return [
        {
            "id": str(uuid4()),
            "name": pending.user_id or "external-approval",
            "role": "user",
            "content": command,
            "metadata": {
                _EXTERNAL_APPROVAL_MESSAGE_META_KEY: True,
                APPROVAL_REQUEST_ID_META_KEY: pending.request_id,
                APPROVAL_DECISION_META_KEY: decision.value,
                APPROVAL_SOURCE_CHANNEL_META_KEY: source_channel,
                "approval_source_user_id": source_user_id,
                "approval_source_message_id": source_message_id,
            },
            "timestamp": _message_timestamp(),
        },
        [],
    ]


def _has_external_approval_memory_entry(
    content: list[Any],
    *,
    request_id: str,
    decision: ExternalApprovalDecision,
) -> bool:
    for entry in content:
        if not isinstance(entry, list) or not entry:
            continue
        message = entry[0]
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if not metadata.get(_EXTERNAL_APPROVAL_MESSAGE_META_KEY):
            continue
        if metadata.get(APPROVAL_REQUEST_ID_META_KEY) != request_id:
            continue
        if metadata.get(APPROVAL_DECISION_META_KEY) == decision.value:
            return True
    return False


async def _append_external_approval_message(
    *,
    workspace: Any,
    pending: PendingApproval,
    decision: ExternalApprovalDecision,
    source_channel: str,
    source_user_id: str | None = None,
    source_message_id: str | None = None,
) -> None:
    runner = getattr(workspace, "runner", None)
    session = getattr(runner, "session", None)
    mutate_session_state = getattr(session, "mutate_session_state", None)
    if not callable(mutate_session_state):
        logger.debug(
            "external approval message append skipped: session unavailable "
            "request_id=%s",
            pending.request_id,
        )
        return

    command = _command_for_decision(decision, pending.request_id)

    def _mutate(state: dict[str, Any]) -> dict[str, Any]:
        agent_state = state.setdefault("agent", {})
        if not isinstance(agent_state, dict):
            agent_state = {}
            state["agent"] = agent_state
        memory_state = agent_state.setdefault("memory", {})
        if not isinstance(memory_state, dict):
            memory_state = {}
            agent_state["memory"] = memory_state
        memory_state.setdefault("_compressed_summary", "")
        content = memory_state.setdefault("content", [])
        if not isinstance(content, list):
            content = []
            memory_state["content"] = content
        if _has_external_approval_memory_entry(
            content,
            request_id=pending.request_id,
            decision=decision,
        ):
            return state
        content.append(
            _external_approval_memory_entry(
                pending=pending,
                command=command,
                decision=decision,
                source_channel=source_channel,
                source_user_id=source_user_id,
                source_message_id=source_message_id,
            ),
        )
        return state

    try:
        await mutate_session_state(
            session_id=pending.session_id,
            user_id=pending.user_id or "external-approval",
            mutator=_mutate,
            create_if_not_exist=True,
        )
    except Exception:
        logger.warning(
            "external approval message append failed: request_id=%s",
            pending.request_id,
            exc_info=True,
        )


async def _record_approval_event(
    pending: PendingApproval,
    event_type: str,
    *,
    status: str | None = None,
    actor_channel: str | None = None,
    actor_user_id: str | None = None,
    source_message_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        await get_approval_service().record_event(
            pending,
            event_type,
            status=status,
            actor_channel=actor_channel,
            actor_user_id=actor_user_id,
            source_message_id=source_message_id,
            details=details,
        )
    except Exception:
        logger.warning(
            "approval audit event failed: request_id=%s event=%s",
            pending.request_id,
            event_type,
            exc_info=True,
        )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _get_channel(channel_manager: Any, name: str) -> Any | None:
    if channel_manager is None:
        return None
    get_channel = getattr(channel_manager, "get_channel", None)
    if get_channel is None:
        return None
    return await _maybe_await(get_channel(name))


def _is_tool_guard_approval(pending: PendingApproval) -> bool:
    extra = pending.extra if isinstance(pending.extra, dict) else {}
    return (
        extra.get("approval_kind") or TOOL_GUARD_APPROVAL_KIND
    ) == TOOL_GUARD_APPROVAL_KIND


def _approval_source_id(pending: PendingApproval) -> str | None:
    extra = pending.extra if isinstance(pending.extra, dict) else {}
    source_id = extra.get("source_id")
    return source_id if isinstance(source_id, str) and source_id else None


def _source_system_config_service_from_workspace(workspace: Any | None) -> Any:
    if workspace is None:
        return None
    return getattr(
        workspace,
        "_source_system_config_service",
        None,
    ) or getattr(workspace, "source_system_config_service", None)


async def _resolve_source_config_for_notification(
    pending: PendingApproval,
    *,
    workspace: Any | None = None,
) -> Any | None:
    from ..source_system_config.runtime import get_current_source_system_config

    current_config = get_current_source_system_config()
    if current_config is not None:
        return current_config

    source_id = _approval_source_id(pending)
    if not source_id:
        return None

    service = _source_system_config_service_from_workspace(workspace)
    resolve_config = getattr(service, "resolve_config", None)
    if not callable(resolve_config):
        return None

    try:
        return await _maybe_await(resolve_config(source_id))
    except Exception:
        logger.warning(
            "zhaohu approval notification source config resolve failed: "
            "request_id=%s source_id=%s",
            pending.request_id,
            source_id,
            exc_info=True,
        )
        return None


async def _should_notify_zhaohu_for_approval(
    pending: PendingApproval,
    *,
    workspace: Any | None = None,
) -> bool:
    if not _is_tool_guard_approval(pending):
        return True
    from ..source_system_config.runtime import (
        is_zhaohu_tool_guard_notification_enabled,
    )

    source_config = await _resolve_source_config_for_notification(
        pending,
        workspace=workspace,
    )
    return is_zhaohu_tool_guard_notification_enabled(source_config)


async def _wait_until_run_idle(task_tracker: Any, run_key: str) -> None:
    status_fn = getattr(task_tracker, "status", None)
    if status_fn is None:
        return

    deadline = asyncio.get_running_loop().time() + _RUN_IDLE_WAIT_SECONDS
    while True:
        status = await _maybe_await(status_fn(run_key))
        if status is None or getattr(status, "value", status) == "idle":
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("approval chat is still streaming")
        await asyncio.sleep(_RUN_IDLE_POLL_SECONDS)


def build_external_approval_payload(
    *,
    pending: PendingApproval,
    decision: ExternalApprovalDecision,
    source_channel: str,
    source_user_id: str | None = None,
    source_message_id: str | None = None,
    source_id: str | None = None,
    user_name: str | None = None,
    bbk_id: str | None = None,
) -> dict[str, Any]:
    """Build the console-native payload for an external decision."""
    command = _command_for_decision(decision, pending.request_id)
    meta: dict[str, Any] = {
        "session_id": pending.session_id,
        "user_id": pending.user_id,
        APPROVAL_REQUEST_ID_META_KEY: pending.request_id,
        APPROVAL_DECISION_META_KEY: decision.value,
        APPROVAL_SOURCE_CHANNEL_META_KEY: source_channel,
    }
    if source_user_id:
        meta["approval_source_user_id"] = source_user_id
    if source_message_id:
        meta["approval_source_message_id"] = source_message_id
    if source_id:
        meta["source_id"] = source_id
    if user_name:
        meta["user_name"] = user_name
    if bbk_id:
        meta["bbk_id"] = bbk_id

    return {
        "channel_id": CONSOLE_CHANNEL,
        "sender_id": pending.user_id or "external-approval",
        "content_parts": [
            TextContent(type=ContentType.TEXT, text=command),
        ],
        "meta": meta,
    }


async def notify_cron_approval_pending(
    pending: PendingApproval,
    *,
    channel_manager: Any | None,
) -> None:
    """Notify zhaohu that a cron approval is pending.

    Zhaohu rendering is currently a no-op hook. This function keeps the
    workflow boundary in place so the real rich card implementation can fill
    it later.
    """
    if not await _should_notify_zhaohu_for_approval(pending):
        return
    zhaohu = await _get_channel(channel_manager, ZHAOHU_CHANNEL)
    if zhaohu is None:
        return
    sender = getattr(zhaohu, "send_cron_approval_card", None)
    if sender is None:
        return

    approval_meta = pending.extra if isinstance(pending.extra, dict) else {}
    tool_call = pending.extra.get("tool_call") if pending.extra else None
    tool_input = tool_call.get("input") if isinstance(tool_call, dict) else {}
    try:
        result = await sender(
            request_id=pending.request_id,
            session_id=pending.session_id,
            user_id=pending.user_id,
            agent_id=approval_meta.get("agent_id") or "",
            tenant_id=approval_meta.get("tenant_id") or "",
            source_id=approval_meta.get("source_id") or "",
            tool_name=pending.tool_name,
            result_summary=pending.result_summary,
            findings_count=pending.findings_count,
            tool_input=tool_input,
            approve_command=f"/approve {pending.request_id}",
            deny_command=f"/deny {pending.request_id}",
        )
        await _record_approval_event(
            pending,
            "pending_notified",
            actor_channel=ZHAOHU_CHANNEL,
            details={"result": result},
        )
    except Exception:
        logger.exception(
            "zhaohu cron approval pending notification failed: request_id=%s",
            pending.request_id,
        )
        await _record_approval_event(
            pending,
            "notify_failed",
            actor_channel=ZHAOHU_CHANNEL,
            details={"phase": "pending"},
        )


async def notify_cron_approval_result(
    workspace: Any,
    pending: PendingApproval,
    *,
    decision: ExternalApprovalDecision,
    source_channel: str,
) -> None:
    """Notify zhaohu about the submitted approval result."""
    if not await _should_notify_zhaohu_for_approval(
        pending,
        workspace=workspace,
    ):
        return
    channel_manager = getattr(workspace, "channel_manager", None)
    zhaohu = await _get_channel(channel_manager, ZHAOHU_CHANNEL)
    if zhaohu is None:
        return
    sender = getattr(zhaohu, "send_cron_approval_result", None)
    if sender is None:
        return

    try:
        result = await sender(
            request_id=pending.request_id,
            session_id=pending.session_id,
            user_id=pending.user_id,
            tool_name=pending.tool_name,
            decision=_status_for_decision(decision),
            source_channel=source_channel,
        )
        await _record_approval_event(
            pending,
            "result_notified",
            status=_status_for_decision(decision),
            actor_channel=ZHAOHU_CHANNEL,
            details={
                "source_channel": source_channel,
                "result": result,
            },
        )
    except Exception:
        logger.exception(
            "zhaohu cron approval result notification failed: request_id=%s",
            pending.request_id,
        )
        await _record_approval_event(
            pending,
            "notify_failed",
            actor_channel=ZHAOHU_CHANNEL,
            details={
                "phase": "result",
                "source_channel": source_channel,
            },
        )


async def _resolve_external_approval_chat(workspace, pending, decision):
    manager = workspace.chat_manager
    user_id = pending.user_id or "external-approval"
    extra = getattr(pending, "extra", None) or {}
    chat_id = extra.get("chat_id")
    channel = getattr(pending, "channel", None) or CONSOLE_CHANNEL
    if chat_id:
        chat = await manager.get_chat(chat_id)
        if (
            chat is None
            or chat.user_id != user_id
            or chat.session_id != pending.session_id
        ):
            raise RuntimeError("Original approval Chat is unavailable")
        return chat
    if channel != CONSOLE_CHANNEL:
        matches = await manager.list_chats_by_session(
            pending.session_id,
            user_id,
            channel,
        )
        if len(matches) != 1:
            raise RuntimeError("Original approval Chat is unavailable")
        return matches[0]
    return await manager.get_or_create_chat(
        pending.session_id,
        user_id,
        CONSOLE_CHANNEL,
        name=_command_for_decision(decision, pending.request_id),
        meta=(
            {"agent_id": workspace.agent_id}
            if getattr(workspace, "agent_id", None)
            else None
        ),
    )


async def submit_external_approval_decision(
    *,
    workspace: Any,
    pending: PendingApproval,
    decision: ExternalApprovalDecision,
    source_channel: str,
    source_user_id: str | None = None,
    source_message_id: str | None = None,
    source_id: str | None = None,
    user_name: str | None = None,
    bbk_id: str | None = None,
    caller_user_id: str | None = None,
) -> ExternalApprovalSubmission:
    """Submit an external approval decision through the console runner.

    ``caller_user_id`` 是服务端解析出的调用方身份；与审批归属不一致时
    直接拒绝，避免同 scope 内跨用户批准。
    """
    owner_user_id = getattr(pending, "user_id", None)
    if (
        caller_user_id is not None
        and owner_user_id
        and owner_user_id != caller_user_id
    ):
        raise PermissionError("approval belongs to another user")
    if pending.status != "pending":
        return ExternalApprovalSubmission(
            request_id=pending.request_id,
            decision=decision,
            status=pending.status,
            session_id=pending.session_id,
            submitted=False,
        )

    channel_manager = getattr(workspace, "channel_manager", None)
    console_channel = await _get_channel(channel_manager, CONSOLE_CHANNEL)
    if console_channel is None:
        raise RuntimeError("console channel not found")

    chat = await _resolve_external_approval_chat(workspace, pending, decision)

    await _wait_until_run_idle(workspace.task_tracker, chat.id)

    current = await get_approval_service().get_request(pending.request_id)
    if current is None or current.status != "pending":
        return ExternalApprovalSubmission(
            request_id=pending.request_id,
            decision=decision,
            status=current.status if current is not None else "superseded",
            session_id=pending.session_id,
            submitted=False,
        )
    pending = current

    payload = build_external_approval_payload(
        pending=pending,
        decision=decision,
        source_channel=source_channel,
        source_user_id=source_user_id,
        source_message_id=source_message_id,
        source_id=source_id,
        user_name=user_name,
        bbk_id=bbk_id,
    )
    payload["channel_id"] = getattr(chat, "channel", CONSOLE_CHANNEL)
    payload["meta"]["chat_id"] = chat.id
    await _append_external_approval_message(
        workspace=workspace,
        pending=pending,
        decision=decision,
        source_channel=source_channel,
        source_user_id=source_user_id,
        source_message_id=source_message_id,
    )
    coordinator = workspace.answer_turn_coordinator
    if coordinator is None:
        raise RuntimeError("answer-turn coordinator is not configured")

    submission_ready = asyncio.Event()
    submission_accepted = False

    async def producer(identity: TurnIdentity, bound_payload: Any):
        await submission_ready.wait()
        if not submission_accepted:
            await coordinator.cancel_unstarted(identity)
            return
        if not await coordinator.mark_producer_started(identity):
            await coordinator.cancel_unstarted(identity)
            return
        next_payload = {
            **bound_payload,
            "meta": {
                **(bound_payload.get("meta") or {}),
                "answer_turn_identity": identity,
                "msgid": identity.msgid,
            },
        }
        async for event in console_channel.stream_one(next_payload):
            yield event

    try:
        lease = await coordinator.start_or_attach(
            chat.id,
            payload,
            producer,
            msgid=f"approval-{pending.request_id}",
            defer_producer_start=True,
        )
        is_new_run = lease.is_new_run

        await get_approval_service().record_external_submission(
            pending,
            decision=decision.value,
            source_channel=source_channel,
            source_user_id=source_user_id,
            source_message_id=source_message_id,
        )
        submission_accepted = True
    finally:
        submission_ready.set()

    await notify_cron_approval_result(
        workspace,
        pending,
        decision=decision,
        source_channel=source_channel,
    )

    return ExternalApprovalSubmission(
        request_id=pending.request_id,
        decision=decision,
        status="submitted",
        session_id=pending.session_id,
        chat_id=chat.id,
        submitted=True,
        reconnect=True,
        is_new_run=is_new_run,
    )
