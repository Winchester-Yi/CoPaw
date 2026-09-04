# -*- coding: utf-8 -*-
"""Chat management API."""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from agentscope.memory import InMemoryMemory

from .session import (
    SafeJSONSession,
    _normalize_state_for_load,
)
from .manager import ChatManager
from .models import (
    ChatArchiveMetadata,
    ChatArchivePage,
    ChatCompactionBoundary,
    ChatPage,
    ChatSpec,
    ChatHistory,
    ChatMessage,
)
from ...agents.memory.conversation_archive import ConversationArchiveStore
from .model_call_error_detail import MODEL_CALL_FAILED_MESSAGES_STATE_KEY
from .hidden_context_injection import HIDDEN_CONTEXT_METADATA_KEY
from .utils import agentscope_msg_to_message
from ..approvals import get_approval_service

router = APIRouter(prefix="/chats", tags=["chats"])
TASK_MESSAGES_STATE_KEY = "task_messages"
TASK_RUNS_STATE_KEY = "task_runs"
TASK_RUN_SECTION_STEP = "step"
TASK_RUN_SECTION_FINAL = "final"
_SERVER_OWNED_CHAT_META_KEYS = frozenset(
    {
        "scenario_preset_snapshot",
        "scenario_preset_snapshot_source",
    },
)


def _reject_server_owned_chat_meta(meta: dict[str, Any] | None) -> None:
    for key in _SERVER_OWNED_CHAT_META_KEYS:
        if key in (meta or {}):
            raise HTTPException(
                status_code=400,
                detail=f"{key} is server-owned",
            )


class ChatUpdateRequest(BaseModel):
    """聊天更新请求，允许调用方只提交本次需要修改的字段。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    channel: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    meta: dict[str, Any] | None = None
    status: str | None = None


def _merge_chat_update(
    existing: ChatSpec,
    patch: ChatUpdateRequest,
) -> ChatSpec:
    """将部分更新合并到已有会话，保留调用方没有提交的字段。"""
    updates = patch.model_dump(exclude_unset=True)
    merged = existing.model_dump()

    for field_name in (
        "name",
        "session_id",
        "user_id",
        "channel",
        "created_at",
        "updated_at",
        "status",
    ):
        if field_name in updates and updates[field_name] is not None:
            merged[field_name] = updates[field_name]

    if "meta" in updates:
        # Plan Mode 只会提交一个 meta 开关，合并可避免覆盖其他会话元数据。
        incoming_meta = updates["meta"] or {}
        _reject_server_owned_chat_meta(incoming_meta)
        merged["meta"] = {
            **(existing.meta or {}),
            **incoming_meta,
        }

    return ChatSpec.model_validate(merged)


async def _annotate_approval_action_statuses(
    messages: list[ChatMessage],
) -> list[ChatMessage]:
    """Attach current approval status to messages carrying approval metadata."""
    approval_service = get_approval_service()
    request_ids: list[str] = []
    actions: list[dict[str, Any]] = []
    for message in messages:
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, dict):
            continue

        nested = metadata.get("metadata")
        if not isinstance(nested, dict):
            continue

        approval_action = nested.get("approval_action")
        if not isinstance(approval_action, dict):
            continue

        request_id = approval_action.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            continue
        request_ids.append(request_id)
        actions.append(approval_action)

    batch_get = getattr(approval_service, "get_requests", None)
    if callable(batch_get):
        requests = await batch_get(request_ids)
    else:
        requests = {
            request_id: request
            for request_id in request_ids
            if (request := await approval_service.get_request(request_id))
            is not None
        }
    for request_id, approval_action in zip(request_ids, actions):
        request = requests.get(request_id)
        if request is None:
            continue
        approval_action["status"] = request.status

    return messages


def _redact_hidden_context_messages(
    messages: list[ChatMessage],
) -> list[ChatMessage]:
    """Build display-safe chat-history messages without mutating memory."""
    redacted: list[ChatMessage] = []
    for message in messages:
        if message.role != "user" or not isinstance(message.metadata, dict):
            redacted.append(message)
            continue

        nested_metadata = message.metadata.get("metadata")
        marker = message.metadata.get(HIDDEN_CONTEXT_METADATA_KEY)
        if marker is None and isinstance(nested_metadata, dict):
            marker = nested_metadata.get(HIDDEN_CONTEXT_METADATA_KEY)
        if not isinstance(marker, dict):
            redacted.append(message)
            continue

        visible_text = marker.get("visible_text")
        suffix = marker.get("suffix")
        if not isinstance(visible_text, str) or not isinstance(suffix, str):
            redacted.append(message)
            continue

        payload = message.model_dump(mode="json")
        metadata = {
            key: value
            for key, value in message.metadata.items()
            if key != HIDDEN_CONTEXT_METADATA_KEY
        }
        if isinstance(nested_metadata, dict):
            metadata["metadata"] = {
                key: value
                for key, value in nested_metadata.items()
                if key != HIDDEN_CONTEXT_METADATA_KEY
            }
        payload["metadata"] = metadata
        payload["content"] = [{"type": "text", "text": visible_text}]
        redacted.append(ChatMessage.model_validate(payload))
    return redacted


def _task_session_messages_from_state(state: dict) -> list[ChatMessage]:
    raw_messages = state.get(TASK_MESSAGES_STATE_KEY, [])
    if not isinstance(raw_messages, list):
        return []

    messages: list[ChatMessage] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        content = raw.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        messages.append(
            ChatMessage.model_validate(
                {
                    "id": raw.get("id") or str(uuid4()),
                    "type": raw.get("type") or "message",
                    "role": raw.get("role") or "assistant",
                    "content": content,
                    "metadata": raw.get("metadata") or {},
                    "timestamp": raw.get("timestamp"),
                },
            ),
        )
    return messages


def _model_call_failed_messages_from_state(state: dict) -> list[ChatMessage]:
    raw_messages = state.get(MODEL_CALL_FAILED_MESSAGES_STATE_KEY, [])
    if not isinstance(raw_messages, list):
        return []

    messages: list[ChatMessage] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        if raw.get("code") != "model_call_failed":
            continue
        message = raw.get("message")
        if not isinstance(message, str) or not message:
            continue
        messages.append(
            ChatMessage.model_validate(
                {
                    "id": raw.get("id") or str(uuid4()),
                    "type": "error",
                    "role": "assistant",
                    "status": "failed",
                    "content": [],
                    "code": "model_call_failed",
                    "message": message,
                    "metadata": raw.get("metadata") or {},
                    "timestamp": raw.get("timestamp"),
                },
            ),
        )
    return messages


async def _messages_from_memory_state(
    memory_state: dict,
    *,
    session_id: str = "",
    position_offset: int = 0,
) -> list[ChatMessage]:
    if not memory_state:
        return []

    memory = InMemoryMemory()
    normalized_state = _normalize_state_for_load(memory_state)
    memory.load_state_dict(normalized_state, strict=False)
    memories = await memory.get_memory(prepend_summary=False)
    return agentscope_msg_to_message(
        memories,
        session_id=session_id,
        position_offset=position_offset,
    )


def _turn_state_to_message(
    turn_id: object,
    turn_state: object,
    *,
    chat_id: str | None,
) -> ChatMessage | None:
    """Convert one durable turn state into a public user message."""
    if not isinstance(turn_state, dict):
        return None
    stored_chat_id = turn_state.get("chat_id")
    if (
        chat_id is not None
        and isinstance(stored_chat_id, str)
        and stored_chat_id
        and stored_chat_id != chat_id
    ):
        return None
    raw = turn_state.get("message")
    if not isinstance(raw, dict) or raw.get("role") != "user":
        return None
    content = raw.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return None
    turn_key = str(turn_id)
    return ChatMessage.model_validate(
        {
            "id": raw.get("id") or turn_key,
            "type": raw.get("type") or "message",
            "role": "user",
            "content": content,
            "metadata": {
                **(raw.get("metadata") or {}),
                "original_id": raw.get("id") or turn_key,
            },
            "timestamp": raw.get("timestamp"),
        },
    )


def _turn_state_messages_from_state(
    state: dict,
    *,
    session_id: str,
    chat_id: str | None = None,
) -> list[ChatMessage]:
    """Expose pre-admitted user anchors when Agent memory has no output yet."""
    turn_states = state.get("turn_states")
    if not isinstance(turn_states, dict):
        return []
    messages: list[ChatMessage] = []
    for turn_id, turn_state in turn_states.items():
        message = _turn_state_to_message(
            turn_id,
            turn_state,
            chat_id=chat_id,
        )
        if message is not None:
            messages.append(message)
    return messages


def _turn_status_from_state(state: dict, msgid: str) -> str | None:
    turn_states = state.get("turn_states")
    if not isinstance(turn_states, dict):
        return None
    turn_state = turn_states.get(msgid)
    if not isinstance(turn_state, dict):
        return None
    status = turn_state.get("status")
    return status if isinstance(status, str) else None


def _turn_state_chat_id(state: dict, msgid: str) -> str | None:
    turn_states = state.get("turn_states")
    if not isinstance(turn_states, dict):
        return None
    turn_state = turn_states.get(msgid)
    if not isinstance(turn_state, dict):
        return None
    chat_id = turn_state.get("chat_id")
    return chat_id if isinstance(chat_id, str) and chat_id else None


def _message_turn_status(message: ChatMessage) -> str | None:
    metadata = getattr(message, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    for candidate in (metadata, metadata.get("metadata")):
        if isinstance(candidate, dict):
            status = candidate.get("turn_status")
            if isinstance(status, str):
                return status
    return None


def _slice_memory_state(
    memory_state: dict,
    start: int,
    end: int,
) -> dict | None:
    content = memory_state.get("content")
    if not isinstance(content, list):
        return None

    sliced_state = dict(memory_state)
    sliced_state["content"] = content[start:end]
    return sliced_state


def _message_has_text_content(message: ChatMessage) -> bool:
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return False

    for block in content:
        block_type = (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        )
        if block_type != "text":
            continue
        text = (
            block.get("text")
            if isinstance(block, dict)
            else getattr(block, "text", None)
        )
        if isinstance(text, str) and text.strip():
            return True
    return False


def _with_task_run_metadata(
    message: ChatMessage,
    *,
    run_id: str,
    run_index: int,
    section: str,
) -> ChatMessage:
    payload = message.model_dump(mode="json")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = {
        **metadata,
        "task_run_id": run_id,
        "task_run_index": run_index,
        "task_run_section": section,
    }
    payload["metadata"] = metadata
    return ChatMessage.model_validate(payload)


def _normalize_task_runs(
    raw_task_runs: list[dict],
    content_length: int,
) -> list[tuple[int, int, int, str]] | None:
    """校验并标准化 task_runs 索引数据。"""
    task_runs: list[tuple[int, int, int, str]] = []
    for run_index, raw_run in enumerate(raw_task_runs):
        if not isinstance(raw_run, dict):
            return None

        raw_start = raw_run.get("memory_start")
        raw_end = raw_run.get("memory_end")
        if raw_start is None or raw_end is None:
            return None

        try:
            start = int(raw_start)
            end = int(raw_end)
        except (TypeError, ValueError):
            return None

        if start < 0 or end < start or end > content_length:
            return None

        run_id = str(raw_run.get("run_id") or f"task-run-{run_index}")
        task_runs.append((start, end, run_index, run_id))

    task_runs.sort(key=lambda item: (item[0], item[2]))
    return task_runs


async def _messages_from_memory_range(
    memory_state: dict,
    start: int,
    end: int,
    *,
    session_id: str = "",
) -> list[ChatMessage] | None:
    """读取一段 memory slice 对应的消息。"""
    sliced_state = _slice_memory_state(memory_state, start, end)
    if sliced_state is None:
        return None
    return await _messages_from_memory_state(
        sliced_state,
        session_id=session_id,
        position_offset=start,
    )


def _find_final_text_message_index(
    run_messages: list[ChatMessage],
) -> int | None:
    """返回最后一个包含文本的 assistant 消息位置。"""
    for index in range(len(run_messages) - 1, -1, -1):
        candidate = run_messages[index]
        if candidate.role == "assistant" and _message_has_text_content(
            candidate,
        ):
            return index
    return None


def _annotate_run_messages(
    run_messages: list[ChatMessage],
    *,
    run_id: str,
    run_index: int,
) -> list[ChatMessage]:
    """为单次 task run 的消息补齐 step/final 元数据。"""
    final_index = _find_final_text_message_index(run_messages)
    if final_index is None:
        return run_messages

    return [
        _with_task_run_metadata(
            message,
            run_id=run_id,
            run_index=run_index,
            section=(
                TASK_RUN_SECTION_FINAL
                if index == final_index
                else TASK_RUN_SECTION_STEP
            ),
        )
        for index, message in enumerate(run_messages)
    ]


async def _annotate_task_run_messages(
    memory_state: dict,
    raw_task_runs: list[dict],
    *,
    session_id: str = "",
) -> list[ChatMessage]:
    content = memory_state.get("content")
    if not isinstance(content, list):
        return await _messages_from_memory_state(
            memory_state,
            session_id=session_id,
        )

    task_runs = _normalize_task_runs(raw_task_runs, len(content))
    if task_runs is None:
        return await _messages_from_memory_state(
            memory_state,
            session_id=session_id,
        )

    messages: list[ChatMessage] = []
    cursor = 0
    for start, end, run_index, run_id in task_runs:
        if start < cursor:
            return await _messages_from_memory_state(
                memory_state,
                session_id=session_id,
            )

        if cursor < start:
            gap_messages = await _messages_from_memory_range(
                memory_state,
                cursor,
                start,
                session_id=session_id,
            )
            if gap_messages is None:
                return await _messages_from_memory_state(
                    memory_state,
                    session_id=session_id,
                )
            messages.extend(gap_messages)

        run_messages = await _messages_from_memory_range(
            memory_state,
            start,
            end,
            session_id=session_id,
        )
        if run_messages is None:
            return await _messages_from_memory_state(
                memory_state,
                session_id=session_id,
            )
        messages.extend(
            _annotate_run_messages(
                run_messages,
                run_id=run_id,
                run_index=run_index,
            ),
        )

        cursor = end

    if cursor < len(content):
        tail_messages = await _messages_from_memory_range(
            memory_state,
            cursor,
            len(content),
            session_id=session_id,
        )
        if tail_messages is not None:
            messages.extend(tail_messages)

    return messages


def _message_sort_key(message: ChatMessage) -> tuple[int, datetime]:
    timestamp = getattr(message, "timestamp", None)
    if isinstance(timestamp, str) and timestamp:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(
                    tzinfo=datetime.now().astimezone().tzinfo,
                )
            return (
                0,
                parsed,
            )
        except ValueError:
            pass
    return (1, datetime.max.replace(tzinfo=timezone.utc))


async def get_workspace(request: Request):
    """Get the workspace for the active agent."""
    from ..agent_context import get_agent_for_request

    return await get_agent_for_request(request)


async def get_chat_manager(
    request: Request,
) -> ChatManager:
    """Get the chat manager for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        ChatManager instance for the specified agent

    Raises:
        HTTPException: If manager is not initialized
    """
    workspace = await get_workspace(request)
    return workspace.chat_manager


def _request_user_id(request: Request) -> str | None:
    request_state = getattr(request, "state", None)
    if request_state is None:
        return None
    user_id = getattr(request_state, "user_id", None)
    return user_id if isinstance(user_id, str) and user_id else None


def _request_source_id(request: Request) -> str | None:
    source_id = getattr(getattr(request, "state", None), "source_id", None)
    return source_id if isinstance(source_id, str) and source_id else None


def _request_agent_id(request: Request, workspace: Any) -> str | None:
    agent_id = getattr(getattr(request, "state", None), "agent_id", None)
    if not isinstance(agent_id, str) or not agent_id:
        agent_id = getattr(workspace, "agent_id", None)
    return agent_id if isinstance(agent_id, str) and agent_id else None


def _authorize_chat(
    request: Request,
    chat: ChatSpec,
    workspace: Any,
) -> None:
    """Ensure a Chat record belongs to the request identity and Agent."""
    request_user_id = _request_user_id(request)
    if request_user_id is not None and chat.user_id != request_user_id:
        raise HTTPException(status_code=404, detail="Chat not found")

    request_source_id = _request_source_id(request)
    chat_source_id = (chat.meta or {}).get("source_id")
    if (
        request_source_id is not None
        and isinstance(chat_source_id, str)
        and chat_source_id
        and chat_source_id != request_source_id
    ):
        raise HTTPException(status_code=404, detail="Chat not found")

    request_agent_id = _request_agent_id(request, workspace)
    chat_agent_id = (chat.meta or {}).get("agent_id")
    if (
        request_agent_id is not None
        and isinstance(chat_agent_id, str)
        and chat_agent_id
        and chat_agent_id != request_agent_id
    ):
        raise HTTPException(status_code=404, detail="Chat not found")


def _authorize_requested_user(request: Request, user_id: str) -> None:
    current_user_id = _request_user_id(request)
    if current_user_id is not None and user_id != current_user_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot access another user's chats",
        )


async def get_session(
    request: Request,
) -> SafeJSONSession:
    """Get the session for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        SafeJSONSession instance for the specified agent

    Raises:
        HTTPException: If session is not initialized
    """
    workspace = await get_workspace(request)
    return workspace.runner.session


async def _read_history_state(
    session: SafeJSONSession,
    session_id: str,
    user_id: str,
) -> dict:
    """Read the durable history snapshot without delaying on an active turn."""
    read_snapshot = getattr(session, "get_persisted_session_state_dict", None)
    if read_snapshot is not None:
        return await read_snapshot(session_id, user_id)
    return await session.get_session_state_dict(session_id, user_id)


async def _cached_history_state(
    cache: dict[tuple[str, str], dict],
    session: SafeJSONSession,
    session_id: str,
    user_id: str,
) -> dict:
    key = (session_id, user_id)
    state = cache.get(key)
    if state is None:
        state = await session.get_session_state_dict(session_id, user_id)
        cache[key] = state
    return state


async def _build_chat_history(
    chat_spec: ChatSpec,
    *,
    session: SafeJSONSession,
    workspace,
    status_override: str | None = None,
    non_blocking: bool = False,
    state: dict | None = None,
) -> ChatHistory:
    if state is None:
        state = (
            await _read_history_state(
                session,
                chat_spec.session_id,
                chat_spec.user_id,
            )
            if non_blocking
            else await session.get_session_state_dict(
                chat_spec.session_id,
                chat_spec.user_id,
            )
        )
    if status_override is not None:
        status = status_override
    else:
        coordinator = getattr(workspace, "answer_turn_coordinator", None)
        turn_status = (
            await coordinator.status(chat_spec.id)
            if coordinator is not None
            else None
        )
        status = turn_status.value if turn_status is not None else "idle"
    task_messages = _task_session_messages_from_state(state)
    model_call_failed_messages = _model_call_failed_messages_from_state(state)
    archive = await _archive_metadata(workspace, chat_spec.id)
    if not state:
        return ChatHistory(
            chat=chat_spec,
            messages=[*task_messages, *model_call_failed_messages],
            status=status,
            archive=archive,
        )
    memory_state = state.get("agent", {}).get("memory", {})
    messages: list[ChatMessage] = []
    if memory_state:
        if (
            (chat_spec.meta or {}).get("session_kind") == "task"
            and isinstance(state.get(TASK_RUNS_STATE_KEY), list)
            and state.get(TASK_RUNS_STATE_KEY)
        ):
            messages = await _annotate_task_run_messages(
                memory_state,
                state[TASK_RUNS_STATE_KEY],
                session_id=chat_spec.session_id,
            )
        else:
            messages = await _messages_from_memory_state(
                memory_state,
                session_id=chat_spec.session_id,
            )
    messages.extend(task_messages)
    messages.extend(model_call_failed_messages)
    known_ids = {
        _message_original_id(message)
        for message in messages
        if _message_original_id(message)
    }
    messages.extend(
        message
        for message in _turn_state_messages_from_state(
            state,
            session_id=chat_spec.session_id,
            chat_id=chat_spec.id,
        )
        if _message_original_id(message) not in known_ids
    )
    messages.sort(key=_message_sort_key)
    messages = await _annotate_approval_action_statuses(messages)
    messages = _redact_hidden_context_messages(messages)
    return ChatHistory(
        chat=chat_spec,
        messages=messages,
        status=status,
        archive=archive,
    )


def _archive_store(workspace) -> ConversationArchiveStore | None:
    workspace_dir = getattr(workspace, "workspace_dir", None)
    if workspace_dir is None:
        return None
    return ConversationArchiveStore(workspace_dir / "dialog")


def _boundary_model(boundary) -> ChatCompactionBoundary:
    return ChatCompactionBoundary(
        id=boundary.id,
        archived_message_count=boundary.archived_message_count,
        first_message_id=boundary.first_message_id,
        last_message_id=boundary.last_message_id,
        created_at=boundary.created_at,
        first_timestamp=boundary.first_timestamp,
        last_timestamp=boundary.last_timestamp,
    )


async def _archive_metadata(workspace, chat_id: str) -> ChatArchiveMetadata:
    store = _archive_store(workspace)
    if store is None:
        return ChatArchiveMetadata()
    page = await store.read_page(chat_id, limit=1)
    return ChatArchiveMetadata(
        has_more=bool(page.messages),
        boundaries=[_boundary_model(boundary) for boundary in page.boundaries],
    )


async def _archive_page(
    workspace,
    chat_id: str,
    before: str | None,
    limit: int,
) -> ChatArchivePage:
    store = _archive_store(workspace)
    if store is None:
        return ChatArchivePage()
    page = await store.read_page(chat_id, before=before, limit=limit)
    messages = agentscope_msg_to_message(
        page.messages,
        session_id=chat_id,
    )
    return ChatArchivePage(
        messages=_redact_hidden_context_messages(messages),
        boundaries=[_boundary_model(boundary) for boundary in page.boundaries],
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


def _message_original_id(message: ChatMessage) -> str | None:
    metadata = getattr(message, "metadata", None)
    if isinstance(metadata, dict):
        original_id = metadata.get("original_id")
        if isinstance(original_id, str) and original_id:
            return original_id
    message_id = getattr(message, "id", None)
    return message_id if isinstance(message_id, str) and message_id else None


def _slice_answer_turn(
    messages: list[ChatMessage],
    *,
    msgid: str,
) -> list[ChatMessage] | None:
    anchor_index: int | None = None
    for index, message in enumerate(messages):
        if _message_original_id(message) != msgid:
            continue
        if getattr(message, "role", None) != "user":
            return None
        anchor_index = index
        break

    if anchor_index is None:
        return None

    end_index = len(messages)
    for index in range(anchor_index + 1, len(messages)):
        if getattr(messages[index], "role", None) == "user":
            end_index = index
            break
    return messages[anchor_index:end_index]


@router.get("", response_model=list[ChatSpec] | ChatPage)
async def list_chats(
    request: Request,
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    page: Optional[int] = Query(None, ge=1, description="Page number"),
    page_size: Optional[int] = Query(
        None,
        ge=1,
        description="Chats per page",
    ),
    cursor: Optional[str] = Query(
        None,
        description="Opaque cursor for live best-effort chat pagination",
    ),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """List chats with optional filters and opt-in pagination.

    Omitting both pagination parameters preserves the legacy array response.
    Providing both returns a ``ChatPage`` ordered by latest update first.
    Cursor pages are live and best-effort: updates between requests can move
    records across the cursor boundary.

    Args:
        user_id: Optional user ID to filter chats
        channel: Optional channel name to filter chats
        page: Optional page number, paired with page_size
        page_size: Optional page size, paired with page
        mgr: Chat manager dependency
    """
    current_user_id = _request_user_id(request)
    if current_user_id is not None:
        if user_id is not None:
            _authorize_requested_user(request, user_id)
        user_id = current_user_id

    cursor_mode = cursor is not None
    if cursor_mode and page is not None:
        raise HTTPException(
            status_code=422,
            detail="page cannot be combined with cursor pagination",
        )
    if cursor_mode and page_size is None:
        raise HTTPException(
            status_code=422,
            detail="page_size is required with cursor pagination",
        )
    if not cursor_mode and (page is None) != (page_size is None):
        raise HTTPException(
            status_code=422,
            detail="page and page_size must be provided together",
        )

    coordinator = getattr(workspace, "answer_turn_coordinator", None)

    async def runtime_statuses(chat_ids: list[str]) -> dict[str, str]:
        if coordinator is None:
            return {chat_id: "idle" for chat_id in chat_ids}
        batch_status = getattr(coordinator, "statuses", None)
        if callable(batch_status):
            statuses = await batch_status(chat_ids)
        else:
            statuses = dict(
                zip(
                    chat_ids,
                    await asyncio.gather(
                        *(coordinator.status(chat_id) for chat_id in chat_ids),
                    ),
                ),
            )
        return {
            chat_id: (
                statuses.get(chat_id).value
                if statuses.get(chat_id) is not None
                else "idle"
            )
            for chat_id in chat_ids
        }

    if cursor_mode and page_size is not None:
        try:
            chat_page = await mgr.list_chats_cursor(
                user_id=user_id,
                channel=channel,
                page_size=page_size,
                cursor=cursor or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        statuses = await runtime_statuses(
            [spec.id for spec in chat_page.items],
        )
        items = [
            spec.model_copy(update={"status": statuses.get(spec.id, "idle")})
            for spec in chat_page.items
        ]
        return chat_page.model_copy(update={"items": items})

    if page is not None and page_size is not None:
        chat_page = await mgr.list_chats_page(
            user_id=user_id,
            channel=channel,
            page=page,
            page_size=page_size,
        )
        statuses = await runtime_statuses(
            [spec.id for spec in chat_page.items],
        )
        items = [
            spec.model_copy(update={"status": statuses.get(spec.id, "idle")})
            for spec in chat_page.items
        ]
        return chat_page.model_copy(update={"items": items})

    chats = await mgr.list_chats(user_id=user_id, channel=channel)
    statuses = await runtime_statuses([spec.id for spec in chats])
    return [
        spec.model_copy(update={"status": statuses.get(spec.id, "idle")})
        for spec in chats
    ]


@router.post("", response_model=ChatSpec)
async def create_chat(
    request_context: Request,
    request: ChatSpec,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Create a new chat.

    Server generates chat_id (UUID) automatically.

    Args:
        request: Chat creation request
        mgr: Chat manager dependency

    Returns:
        Created chat spec with UUID
    """
    _reject_server_owned_chat_meta(request.meta)
    current_user_id = _request_user_id(request_context)
    if current_user_id is not None:
        _authorize_requested_user(request_context, request.user_id)
    current_source_id = _request_source_id(request_context)
    current_agent_id = getattr(
        getattr(request_context, "state", None),
        "agent_id",
        None,
    )
    chat_id = str(uuid4())
    meta = dict(request.meta or {})
    if current_source_id is not None:
        meta["source_id"] = current_source_id
    if isinstance(current_agent_id, str) and current_agent_id:
        meta["agent_id"] = current_agent_id
    spec = ChatSpec(
        id=chat_id,
        name=request.name,
        session_id=request.session_id,
        user_id=request.user_id,
        channel=request.channel,
        meta=meta,
    )
    return await mgr.create_chat(spec)


@router.post("/batch-delete", response_model=dict)
async def batch_delete_chats(
    request: Request,
    chat_ids: list[str],
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Delete chats by chat IDs.

    Args:
        chat_ids: List of chat IDs
        mgr: Chat manager dependency
    Returns:
        True if deleted, False if failed

    """
    authorized_ids: list[str] = []
    for chat_id in chat_ids:
        chat = await mgr.get_chat(chat_id)
        if chat is None:
            continue
        _authorize_chat(request, chat, None)
        authorized_ids.append(chat_id)
    deleted = await mgr.delete_chats(chat_ids=authorized_ids)
    return {"deleted": deleted}


@router.get("/answer-turn", response_model=ChatHistory)
async def get_answer_turn(
    request: Request,
    sessionid: str | None = Query(None, description="Logical session id"),
    msgid: str = Query(..., description="User question message id"),
    chat_id: str | None = Query(None, description="Chat id"),
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Get one answer turn by logical session id and user question msgid."""
    user_id = _request_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=400,
            detail="Request user identity is required",
        )
    chat_spec = None
    candidate_chats: list[ChatSpec] = []
    selected_history: ChatHistory | None = None
    selected_state: dict | None = None
    state_cache: dict[tuple[str, str], dict] = {}
    if chat_id:
        chat_spec = await mgr.get_chat(chat_id)
        if chat_spec:
            try:
                _authorize_chat(request, chat_spec, workspace)
            except HTTPException:
                chat_spec = None
    elif sessionid:
        candidate_chats = [
            chat
            for chat in await mgr.list_chats(
                user_id=user_id,
                channel="console",
            )
            if chat.session_id == sessionid
        ]
        for candidate in candidate_chats:
            try:
                _authorize_chat(request, candidate, workspace)
            except HTTPException:
                continue
            state = await _cached_history_state(
                state_cache,
                session,
                candidate.session_id,
                candidate.user_id,
            )
            stored_chat_id = _turn_state_chat_id(state, msgid)
            if stored_chat_id is not None and stored_chat_id != candidate.id:
                continue
            history = await _build_chat_history(
                candidate,
                session=session,
                workspace=workspace,
                non_blocking=False,
                state=state,
            )
            if _slice_answer_turn(history.messages, msgid=msgid) is not None:
                chat_spec = candidate
                selected_history = history
                selected_state = state
                break
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail="Answer turn not found",
        )

    if selected_history is None:
        selected_state = await _cached_history_state(
            state_cache,
            session,
            chat_spec.session_id,
            chat_spec.user_id,
        )
        selected_history = await _build_chat_history(
            chat_spec,
            session=session,
            workspace=workspace,
            non_blocking=False,
            state=selected_state,
        )
    history = selected_history
    messages = _slice_answer_turn(history.messages, msgid=msgid)
    if messages is None:
        raise HTTPException(
            status_code=404,
            detail="Answer turn not found",
        )
    turn_status = _turn_status_from_state(selected_state or {}, msgid)
    if turn_status is None:
        turn_status = next(
            (
                status
                for status in (
                    _message_turn_status(message) for message in messages
                )
                if status is not None
            ),
            None,
        )
    return ChatHistory(
        chat=chat_spec,
        messages=messages,
        status=history.status,
        turn_status=turn_status,
    )


@router.get("/{chat_id}/history", response_model=ChatArchivePage)
async def get_chat_history_page(
    request: Request,
    chat_id: str,
    before: str | None = Query(None),
    limit: int = Query(50, ge=1, le=50),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Load one display-safe page of compacted history for a chat record."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    _authorize_chat(request, chat_spec, workspace)
    try:
        return await _archive_page(workspace, chat_spec.id, before, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{chat_id}", response_model=ChatHistory)
async def get_chat(
    request: Request,
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Get detailed information about a specific chat by UUID.

    Args:
        request: FastAPI request (for agent context)
        chat_id: Chat UUID
        mgr: Chat manager dependency
        session: SafeJSONSession dependency

    Returns:
        ChatHistory with messages and status (idle/running/stopping)

    Raises:
        HTTPException: If chat not found (404)
    """
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    _authorize_chat(request, chat_spec, workspace)

    return await _build_chat_history(
        chat_spec,
        session=session,
        workspace=workspace,
        non_blocking=True,
    )


@router.put("/{chat_id}", response_model=ChatSpec)
async def update_chat(
    chat_id: str,
    request: Request,
    patch: ChatUpdateRequest,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Update an existing chat.

    Args:
        chat_id: Chat UUID
        patch: Updated chat fields
        mgr: Chat manager dependency

    Returns:
        Updated chat spec

    Raises:
        HTTPException: If chat_id mismatch (400) or not found (404)
    """
    if patch.id is not None and patch.id != chat_id:
        raise HTTPException(
            status_code=400,
            detail="chat_id mismatch",
        )

    existing = await mgr.get_chat(chat_id)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    _authorize_chat(request, existing, None)
    if patch.user_id is not None:
        _authorize_requested_user(request, patch.user_id)

    spec = _merge_chat_update(existing, patch)
    updated = await mgr.update_chat(spec)
    return updated


@router.delete("/{chat_id}", response_model=dict)
async def delete_chat(
    chat_id: str,
    request: Request,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Delete a chat by UUID.

    Note: This only deletes the chat spec (UUID mapping).
    JSONSession state is NOT deleted.

    Args:
        chat_id: Chat UUID
        mgr: Chat manager dependency

    Returns:
        True if deleted, False if failed

    Raises:
        HTTPException: If chat not found (404)
    """
    existing = await mgr.get_chat(chat_id)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    _authorize_chat(request, existing, None)
    deleted = await mgr.delete_chats(chat_ids=[chat_id])
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return {"deleted": True}
