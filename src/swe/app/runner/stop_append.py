# -*- coding: utf-8 -*-
"""Durable post-approval assistant messages, serialized against explicit Stop."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncGenerator
from uuid import uuid4

from agentscope.message import Msg

from ...agents.hook_runtime.models import HookEventName
from ...agents.hook_runtime.append import log_append_diagnostic
from ...agents.hook_runtime.conversation_snapshot import (
    capture_conversation_snapshot,
)
from ...constant import QUERY_CLEANUP_TIMEOUT, WORKING_DIR
from ..answer_turn.models import TurnStatus


class StopAppendPersistenceError(RuntimeError):
    """A post-approval storage failure must never restart query execution."""


STOP_APPEND_PERSISTENCE_FAILED = "Stop append persistence failed"


def _append_budget(agent_config: Any) -> float:
    settings = getattr(
        getattr(agent_config, "running", None),
        "hook_runtime",
        None,
    )
    value = getattr(settings, "max_stop_append_seconds", 30.0)
    if (
        not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return 30.0
    return float(value)


def _contains_committed_message(state: Any, message_id: str) -> bool:
    if isinstance(state, dict):
        marker = state.get("stop_append")
        if isinstance(marker, dict) and marker.get("message_id") == message_id:
            return True
        return any(
            _contains_committed_message(value, message_id)
            for value in state.values()
        )
    if isinstance(state, (list, tuple)):
        return any(
            _contains_committed_message(value, message_id) for value in state
        )
    return False


def _remove_local_message(memory: Any, message_id: str) -> None:
    content = getattr(memory, "content", None)
    if not isinstance(content, list):
        return
    content[:] = [
        entry
        for entry in content
        if getattr(
            entry[0] if isinstance(entry, (list, tuple)) else entry,
            "id",
            None,
        )
        != message_id
    ]


async def _persist_message(owner: Any, runtime: Any, msg: Msg) -> None:
    try:
        await runtime.agent.memory.add(msg)
        saved = await asyncio.wait_for(
            owner.save_job_session_state(
                runtime.agent,
                runtime.session_id,
                runtime.skip_history,
                runtime.user_id,
                runtime.hook_overlay,
                session_execution=runtime.session_execution,
            ),
            timeout=QUERY_CLEANUP_TIMEOUT,
        )
        if not saved:
            raise StopAppendPersistenceError(STOP_APPEND_PERSISTENCE_FAILED)
    except BaseException as exc:
        if not _contains_committed_message(
            runtime.session_execution.state,
            msg.id,
        ):
            _remove_local_message(runtime.agent.memory, msg.id)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not isinstance(exc, Exception):
            raise
        log_append_diagnostic(
            "Stop append persistence failed category=%s message_id=%s",
            type(exc).__name__,
            msg.id,
        )
        raise StopAppendPersistenceError(
            STOP_APPEND_PERSISTENCE_FAILED,
        ) from None


def _append_eligible(request: Any, outcome: Any) -> bool:
    return bool(
        getattr(request, "channel", None) == "console"
        and getattr(outcome, "stop_append_approved", False)
        and not getattr(outcome, "stop_append_started", False)
        and outcome.assistant_response.strip()
        and not outcome.completion_blocked
        and not outcome.plan_interaction_turn_boundary
        and not outcome.pre_tool_terminal_stop
        and not outcome.goal_finalization_fallback,
    )


async def stream_stop_appended_messages(
    owner: Any,
    *,
    request: Any,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    """Collect once, then persist each contribution before publishing it."""
    if not _append_eligible(request, outcome):
        return
    identity = owner._answer_turn_identity(request)
    coordinator = getattr(owner, "_answer_turn_coordinator", None)
    if (
        identity is None
        or coordinator is None
        or runtime.session_execution is None
    ):
        log_append_diagnostic(
            "Stop append skipped: durable turn context unavailable",
        )
        return

    async def stop_requested() -> bool:
        return await coordinator.status(identity) != TurnStatus.RUNNING

    if await stop_requested():
        return
    outcome.stop_append_started = True
    # Import the runner's established builders lazily to avoid a module cycle.
    from .runner import _build_runner_hook_context, _build_stop_hook_runtime

    context = _build_runner_hook_context(
        HookEventName.STOP,
        request=request,
        runner=owner,
        prompt=plan.original_user_message,
        assistant_response=outcome.assistant_response,
    )

    async def snapshot_provider():
        return await capture_conversation_snapshot(runtime.agent.memory)

    contributions = await _build_stop_hook_runtime(
        tenant_hooks=runtime.tenant_hooks,
        agent_config=runtime.agent_config,
        overlay=runtime.hook_overlay,
    ).emit_stop_append(
        context,
        workspace_dir=Path(owner.workspace_dir or WORKING_DIR),
        max_append_seconds=_append_budget(runtime.agent_config),
        conversation_snapshot_provider=snapshot_provider,
        stop_requested=stop_requested,
    )
    if await stop_requested():
        return
    for contribution in contributions:
        if await stop_requested():
            return
        msg = Msg(
            name="Friday",
            role="assistant",
            content=[{"type": "text", "text": contribution.text}],
        )
        msg.id = str(uuid4())
        msg.metadata = {
            "stop_append": {
                "message_id": msg.id,
                **asdict(identity),
                "source": contribution.source,
                "group_id": contribution.group_id,
                "handler_id": contribution.handler_id,
                "order": contribution.order,
            },
        }

        async def persist(message: Msg = msg) -> None:
            await _persist_message(owner, runtime, message)

        commit_started = time.monotonic()
        if not await coordinator.commit_message(identity, persist):
            return
        log_append_diagnostic(
            "Stop append committed turn_id=%s trace_id=%s source=%s "
            "group_id=%s handler_id=%s message_id=%s order=%s "
            "bytes=%s sha256=%s commit_seconds=%.6f",
            identity.turn_id,
            getattr(request, "trace_id", None),
            contribution.source,
            contribution.group_id,
            contribution.handler_id,
            msg.id,
            contribution.order,
            len(contribution.text.encode("utf-8")),
            hashlib.sha256(contribution.text.encode("utf-8")).hexdigest(),
            time.monotonic() - commit_started,
        )
        yield msg, True
