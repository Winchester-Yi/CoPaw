# -*- coding: utf-8 -*-
# flake8: noqa: E704
"""Session state and skill-snapshot lifecycle collaboration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ...agents.skills_manager import resolve_effective_skill_dir
from ...constant import WORKING_DIR
from .query_contracts import _QueryRuntime
from .session import (
    SESSION_SKILL_SNAPSHOT_STATE_KEY,
    _normalize_state_for_load,
)

logger = logging.getLogger(__name__)

TURN_STATES_KEY = "turn_states"


def _user_turn_anchor(
    msgs: list[Any],
    request: Any,
) -> tuple[str, dict[str, Any]] | None:
    if not msgs:
        return None
    message = msgs[-1]
    if getattr(message, "role", None) != "user" or not hasattr(
        message,
        "to_dict",
    ):
        return None
    channel_meta = getattr(request, "channel_meta", None) or {}
    turn_id = str(
        getattr(message, "id", None) or channel_meta.get("msgid") or "",
    ).strip()
    if not turn_id:
        return None
    return turn_id, message.to_dict()


def discard_admitted_user_anchor(agent: Any, turn_id: str | None) -> bool:
    """Remove the prewritten anchor from the live Agent memory snapshot."""
    if not turn_id:
        return False
    memory = getattr(agent, "memory", None)
    content = getattr(memory, "content", None)
    if not isinstance(content, list):
        return False
    original_len = len(content)
    memory.content = [
        entry
        for entry in content
        if not (
            isinstance(entry, tuple)
            and entry
            and getattr(entry[0], "id", None) == turn_id
        )
    ]
    return len(memory.content) != original_len


def mark_terminal_turn_state(
    state: dict[str, Any],
    turn_id: str,
    status: str,
) -> None:
    """Record one public terminal answer-turn status in the durable state."""
    turn_states = state.setdefault(TURN_STATES_KEY, {})
    if not isinstance(turn_states, dict):
        turn_states = {}
        state[TURN_STATES_KEY] = turn_states
    turn_state = turn_states.setdefault(turn_id, {})
    if not isinstance(turn_state, dict):
        turn_state = {}
        turn_states[turn_id] = turn_state
    turn_state["status"] = status


def _find_persisted_turn_anchor(
    content: list[Any],
    turn_id: str,
) -> int | None:
    """Return the user-anchor index for a persisted turn."""
    return next(
        (
            index
            for index, entry in enumerate(content)
            if isinstance(entry, list)
            and entry
            and isinstance(entry[0], dict)
            and entry[0].get("id") == turn_id
            and entry[0].get("role") == "user"
        ),
        None,
    )


def _mark_persisted_assistant_stopped(
    content: list[Any],
    anchor_index: int,
) -> None:
    """Annotate the latest persisted assistant message after the anchor."""
    for entry in reversed(content[anchor_index + 1 :]):
        if (
            not isinstance(entry, list)
            or not entry
            or not isinstance(entry[0], dict)
        ):
            continue
        message = entry[0]
        if message.get("role") != "assistant":
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            message["metadata"] = metadata
        metadata["turn_status"] = "stopped"
        return


def mark_stopped_turn_state(state: dict[str, Any], turn_id: str) -> None:
    """Record terminal Stop status and annotate the last assistant output."""
    mark_terminal_turn_state(state, turn_id, "stopped")
    memory_state = (state.get("agent") or {}).get("memory") or {}
    content = (
        memory_state.get("content") if isinstance(memory_state, dict) else None
    )
    if not isinstance(content, list):
        return
    anchor_index = _find_persisted_turn_anchor(content, turn_id)
    if anchor_index is None:
        return
    _mark_persisted_assistant_stopped(content, anchor_index)


def mark_stopped_agent_memory(agent: Any, turn_id: str | None) -> bool:
    """Annotate the latest assistant message in a live Agent memory."""
    content = getattr(getattr(agent, "memory", None), "content", None)
    if not turn_id or not isinstance(content, list):
        return False
    anchor_index = next(
        (
            index
            for index, entry in enumerate(content)
            if isinstance(entry, tuple)
            and entry
            and getattr(entry[0], "id", None) == turn_id
            and getattr(entry[0], "role", None) == "user"
        ),
        None,
    )
    if anchor_index is None:
        return False
    for entry in reversed(content[anchor_index + 1 :]):
        message = entry[0] if isinstance(entry, tuple) and entry else None
        if getattr(message, "role", None) != "assistant":
            continue
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            message.metadata = metadata
        metadata["turn_status"] = "stopped"
        return True
    return False


async def admit_user_turn(
    execution: Any,
    *,
    msgs: list[Any],
    request: Any,
) -> str | None:
    """Durably record the submitted user anchor before preflight/Agent work."""
    anchor = _user_turn_anchor(msgs, request)
    if anchor is None:
        return None
    turn_id, message = anchor
    state = execution.state
    turn_states = state.get(TURN_STATES_KEY)
    if not isinstance(turn_states, dict):
        turn_states = {}
        state[TURN_STATES_KEY] = turn_states
    existing = turn_states.get(turn_id)
    if not isinstance(existing, dict) or existing.get("message") != message:
        admitted_state = {
            "status": "admitted",
            "message": message,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        chat_id = (getattr(request, "channel_meta", None) or {}).get(
            "chat_id",
        )
        if isinstance(chat_id, str) and chat_id:
            admitted_state["chat_id"] = chat_id
        turn_states[turn_id] = admitted_state
    if not isinstance(existing, dict) or existing.get("message") != message:
        execution.mark_state_dirty()
        await execution.commit_state(state)
    return turn_id


if TYPE_CHECKING:
    from ...agents.react_agent import SWEAgent


class SessionLifecycleOwner(Protocol):
    """Small runner surface required for session lifecycle operations."""

    session: Any
    workspace_dir: Path | None

    async def _save_cron_session_state(
        self,
        agent: SWEAgent,
        session_id: Any,
        user_id: str | None,
        hook_overlay: Any = None,
        session_execution: Any = None,
    ) -> bool: ...

    async def _save_regular_session_state(
        self,
        agent: SWEAgent,
        session_id: Any,
        user_id: str | None,
        hook_overlay: Any = None,
        session_execution: Any = None,
    ) -> None: ...

    def _normalize_session_skill_snapshot(
        self,
        value: Any,
    ) -> dict[str, dict[str, Any]]: ...

    def _supports_session_skill_freshness_refresh(
        self,
        *,
        runtime: _QueryRuntime,
    ) -> bool: ...

    def _refresh_session_skill_snapshot_entries(
        self,
        snapshot: dict[str, dict[str, Any]],
        *,
        stored_snapshot: dict[str, dict[str, Any]],
        effective_skill_dirs: dict[str, Path],
    ) -> list[Any]: ...

    def _skill_freshness_notice_text(self, changes: list[Any]) -> str: ...

    def _can_restore_confirmed_session_skill_context(
        self,
        *,
        runtime: _QueryRuntime,
        detector: Any,
    ) -> bool: ...

    def _select_restorable_session_skill(
        self,
        snapshot: dict[str, dict[str, Any]],
        *,
        enabled_skills: list[str],
    ) -> str | None: ...


async def get_state_loaded(
    owner: SessionLifecycleOwner,
    agent: SWEAgent,
    session_id: str | None,
    session_state_loaded: bool,
    skip_history: bool | Any,
    user_id: str | None,
    *,
    coerce_session_id: Any,
    coerce_user_id: Any,
    session_execution: Any = None,
    retry_state_snapshot: dict[str, Any] | None = None,
) -> bool:
    """Restore state once, retaining cron and schema-mismatch behavior."""
    storage_session_id = coerce_session_id(session_id)
    storage_user_id = coerce_user_id(user_id)
    if retry_state_snapshot is not None:
        agent.load_state_dict(_normalize_state_for_load(retry_state_snapshot))
        return True
    if skip_history:
        logger.info(
            "Cron task: skipping session state load (session_id=%s)",
            session_id,
        )
        return True
    if session_execution is not None:
        state = await session_execution.read_state()
        agent_state = state.get("agent") if isinstance(state, dict) else None
        if isinstance(agent_state, dict):
            agent.load_state_dict(_normalize_state_for_load(agent_state))
        return True
    try:
        await owner.session.load_session_state(
            session_id=storage_session_id,
            user_id=storage_user_id,
            agent=agent,
        )
    except KeyError as exc:
        logger.warning(
            "load_session_state skipped (state schema mismatch): %s; "
            "will save fresh state on completion to recover file",
            exc,
        )
    return True


async def save_job_session_state(
    owner: SessionLifecycleOwner,
    agent: SWEAgent,
    session_id: Any,
    skip_history: bool | Any,
    user_id: str | None,
    hook_overlay: Any = None,
    session_execution: Any = None,
) -> bool:
    """Persist cron or regular session state through runner-owned writers."""
    if skip_history:
        if session_execution is None:
            return await owner._save_cron_session_state(
                agent,
                session_id,
                user_id,
                hook_overlay,
            )
        return await owner._save_cron_session_state(
            agent,
            session_id,
            user_id,
            hook_overlay,
            session_execution=session_execution,
        )
    if session_execution is None:
        await owner._save_regular_session_state(
            agent,
            session_id,
            user_id,
            hook_overlay,
        )
        return True
    await owner._save_regular_session_state(
        agent,
        session_id,
        user_id,
        hook_overlay,
        session_execution=session_execution,
    )
    return True


async def _runtime_skill_snapshot(
    owner: SessionLifecycleOwner,
    runtime: _QueryRuntime,
) -> dict[str, dict[str, Any]]:
    session_execution = runtime.session_execution
    if session_execution is not None:
        state = await session_execution.read_state()
        snapshot = (
            state.get(SESSION_SKILL_SNAPSHOT_STATE_KEY)
            if isinstance(state, dict)
            else None
        )
        return owner._normalize_session_skill_snapshot(snapshot)
    return owner._normalize_session_skill_snapshot(
        await owner.session.get_session_skill_snapshot(
            session_id=runtime.session_id,
            user_id=runtime.user_id,
            allow_not_exist=True,
        ),
    )


async def refresh_session_skill_freshness(
    owner: SessionLifecycleOwner,
    *,
    runtime: _QueryRuntime,
    refresh_result_type: type[Any],
) -> Any:
    """Refresh persisted skills and report context that must be injected."""
    if not owner._supports_session_skill_freshness_refresh(runtime=runtime):
        return refresh_result_type()
    stored_snapshot = await _runtime_skill_snapshot(owner, runtime)
    if not stored_snapshot:
        return refresh_result_type(stored_snapshot={}, refreshed_snapshot={})
    workspace_dir = Path(owner.workspace_dir or WORKING_DIR)
    effective_skill_dirs = {
        skill_name: resolved
        for skill_name in runtime.agent.get_effective_skills()
        if (resolved := resolve_effective_skill_dir(workspace_dir, skill_name))
        is not None
    }
    next_snapshot = owner._normalize_session_skill_snapshot(stored_snapshot)
    changes = owner._refresh_session_skill_snapshot_entries(
        next_snapshot,
        stored_snapshot=stored_snapshot,
        effective_skill_dirs=effective_skill_dirs,
    )
    return refresh_result_type(
        notice_text=(
            owner._skill_freshness_notice_text(changes) if changes else None
        ),
        stored_snapshot=stored_snapshot,
        refreshed_snapshot=next_snapshot,
    )


async def build_skill_snapshot_to_persist(
    owner: SessionLifecycleOwner,
    *,
    runtime: _QueryRuntime,
    refresh_result: Any,
) -> dict[str, dict[str, Any]] | None:
    """Merge confirmed skill entries into a changed persisted snapshot."""
    if (
        runtime.skip_history
        or owner.session is None
        or not runtime.session_id
        or refresh_result.stored_snapshot is None
        or refresh_result.refreshed_snapshot is None
    ):
        return None
    next_snapshot = owner._normalize_session_skill_snapshot(
        refresh_result.refreshed_snapshot,
    )
    if runtime.pending_confirmed_skill_snapshots:
        next_snapshot.update(
            owner._normalize_session_skill_snapshot(
                runtime.pending_confirmed_skill_snapshots,
            ),
        )
    return (
        next_snapshot
        if next_snapshot != refresh_result.stored_snapshot
        else None
    )


async def restore_confirmed_session_skill_context(
    owner: SessionLifecycleOwner,
    *,
    runtime: _QueryRuntime,
) -> None:
    """Restore a one-shot continuation from the persisted skill snapshot."""
    detector = getattr(runtime, "session_skill_detector", None)
    if runtime.skip_history or owner.session is None:
        return
    if not owner._can_restore_confirmed_session_skill_context(
        runtime=runtime,
        detector=detector,
    ):
        return
    stored_snapshot = await _runtime_skill_snapshot(owner, runtime)
    skills = (
        runtime.agent.get_runtime_skills()
        if hasattr(runtime.agent, "get_runtime_skills")
        else runtime.agent.get_effective_skills()
    )
    skill_name = owner._select_restorable_session_skill(
        stored_snapshot,
        enabled_skills=skills,
    )
    if skill_name:
        detector.restore_confirmed_skill(
            skill_name,
            allow_one_shot_continuation=True,
        )
