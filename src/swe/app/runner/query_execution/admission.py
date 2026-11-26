# -*- coding: utf-8 -*-
"""Admission ordering for one query execution."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from swe.tracing.models import TraceStatus
from swe.tracing.agent_trace_sdk import start_child_span
from ...crons.recheck_context import RecheckStartDenied, active_recheck

from ..command_dispatch import _is_command, run_command_path
from ..query_attempt import _query_session_execution
from ..session_lifecycle import admit_user_turn_with_creation
from swe.observability.runner_lifecycle import (
    accept_query_run,
    bind_query_context,
    defer_query_run_settlement,
    finish_query_run,
    prepare_query_run,
    query_waiting_for_approval,
    restore_approval_context,
    start_query_attempt,
    terminal_turn_status,
    update_deferred_query_run_approval,
)


async def stream_admission(
    owner: Any,
    msgs: list[Any],
    *,
    request: AgentRequest,
    query: str | None,
    session_id: str,
    user_id: str,
) -> AsyncIterator[tuple[Msg, bool]]:
    """Resolve admission before command or normal query execution."""
    async with start_child_span(
        "agent.admission",
        operation_name="swe.admission",
    ):
        async with _query_session_execution(
            owner,
            session_id,
            user_id,
            request=request,
        ) as session_execution:
            recheck = await _admit_recheck(
                active_recheck.get(),
                session_execution,
                session_id,
                user_id,
            )
            is_command = recheck is None and query and _is_command(query)
            approval_context, approval_decision = await _approval_continuation(
                owner,
                request=request,
                session_id=session_id,
                user_id=user_id,
                query=query,
            )
            turn_id = None
            is_new_turn = False
            if approval_context is not None:
                run_context = approval_context
            elif (
                session_execution is not None
                and not is_command
                and approval_decision != "unresolved"
            ):
                candidate_context = prepare_query_run(owner, request, msgs)
                turn_id, is_new_turn, run_context = (
                    await admit_user_turn_with_creation(
                        session_execution,
                        msgs=msgs,
                        request=request,
                        observability_context=candidate_context,
                    )
                )
                if is_new_turn:
                    accept_query_run(run_context)
                if hasattr(session_execution, "observed_turn_id"):
                    session_execution.observed_turn_id = turn_id
            else:
                run_context = None
            attempt_context = (
                start_query_attempt(run_context)
                if approval_decision in {None, "approved"}
                else None
            )
            attempt_context_ref = {"value": attempt_context}
            identity_getter = getattr(owner, "_answer_turn_identity", None)
            identity = (
                identity_getter(request) if callable(identity_getter) else None
            )
            deferred_settlement = (
                identity is not None
                and getattr(owner, "_answer_turn_coordinator", None)
                is not None
                and approval_decision
                not in {"denied", "timed_out", "unresolved"}
            )
            if deferred_settlement:
                defer_query_run_settlement(
                    owner,
                    identity,
                    run_context,
                    attempt_context,
                    waiting_for_approval=False,
                    attempt_ref=attempt_context_ref,
                )
            stream = _stream_admitted_query(
                owner,
                msgs,
                request=request,
                query=query,
                session_id=session_id,
                user_id=user_id,
                session_execution=session_execution,
                is_command=bool(is_command),
                observability_context=run_context,
                observability_attempt_ref=attempt_context_ref,
            )
            with bind_query_context(attempt_context or run_context):
                try:
                    async for item in stream:
                        yield item
                finally:
                    waiting_for_approval = await query_waiting_for_approval(
                        owner,
                        request,
                        session_execution,
                    )
                    if deferred_settlement:
                        update_deferred_query_run_approval(
                            owner,
                            identity,
                            waiting_for_approval=waiting_for_approval,
                            attempt=attempt_context_ref["value"],
                            clear_attempt=attempt_context_ref["value"] is None,
                        )
                    else:
                        finish_query_run(
                            run_context,
                            attempt_context_ref["value"],
                            terminal_turn_status(session_execution, turn_id),
                            waiting_for_approval=waiting_for_approval,
                        )


async def _approval_continuation(
    owner: Any,
    *,
    request: Any,
    session_id: str,
    user_id: str,
    query: str | None,
) -> tuple[Any, str | None]:
    if not query:
        return None, None
    from swe.app.approvals import get_approval_service
    from ..runner import (
        _approval_request_id,
        _denial_request_id,
        _is_approval,
        _is_denial,
        _request_matches_external_approval_submission,
        _select_pending_approval,
    )

    explicit_approval = _is_approval(query)
    decision = "approved" if explicit_approval else "denied"
    if not explicit_approval and not _is_denial(query):
        return None, None
    request_id = _approval_request_id(query) or _denial_request_id(query)
    service = get_approval_service()
    pending = await _select_pending_approval(
        service,
        session_id=session_id,
        request_id=request_id,
        user_id=user_id,
    )
    if pending is None and request_id:
        completed = await service.get_request(request_id)
        if (
            completed is not None
            and completed.session_id == session_id
            and completed.user_id == user_id
            and completed.status in {"approved", "denied", "timeout"}
        ):
            return None, "unresolved"
    if (
        pending is None
        or pending.status != "pending"
        or pending.user_id != user_id
    ):
        return None, None
    timeout_seconds = getattr(owner, "_APPROVAL_TIMEOUT_SECONDS", None)
    if (
        isinstance(timeout_seconds, (int, float))
        and time.time() - pending.created_at > timeout_seconds
    ):
        decision = "timed_out"
    extra = pending.extra if isinstance(pending.extra, dict) else {}
    submission = extra.get("external_submission")
    if isinstance(submission, dict):
        if not _request_matches_external_approval_submission(
            request,
            submission,
        ):
            return None, None
    context = restore_approval_context(owner, request, pending)
    return (context, decision) if context is not None else (None, "unresolved")


async def _stream_admitted_query(
    owner,
    msgs,
    *,
    request,
    query,
    session_id,
    user_id,
    session_execution,
    is_command,
    observability_context=None,
    observability_attempt_ref=None,
):
    preflight_args = {
        "session_id": session_id,
        "user_id": user_id,
        "query": query,
        "request": request,
    }
    if session_execution is not None:
        preflight_args["session_execution"] = session_execution
    preflight = await owner._prepare_query_preflight(**preflight_args)
    denial = await _prepare_preflight_denial(
        owner,
        preflight,
        session_execution,
        session_id,
        user_id,
    )
    if denial is not None:
        yield denial, True
        return

    if not preflight.approval_consumed and is_command:
        trace_id = await owner._start_query_trace(request, msgs)
        try:
            command_args = (request, msgs, owner)
            if session_execution is None:
                command_stream = run_command_path(*command_args)
            else:
                command_stream = run_command_path(
                    *command_args,
                    session_execution=session_execution,
                )
            async for message, last in command_stream:
                yield message, last
        except asyncio.CancelledError:
            await owner._end_trace_if_needed(
                trace_id,
                TraceStatus.CANCELLED,
            )
            raise
        except Exception as exc:
            await owner._end_trace_if_needed(
                trace_id,
                TraceStatus.ERROR,
                str(exc),
            )
            raise
        await owner._end_trace_if_needed(
            trace_id,
            TraceStatus.COMPLETED,
        )
        return

    query_args = {
        "request": request,
        "query": query,
        "session_id": session_id,
        "preflight": preflight,
    }
    if session_execution is not None:
        query_args["session_execution"] = session_execution
    if observability_context is not None:
        query_args["observability_context"] = observability_context
        query_args["observability_attempt_ref"] = observability_attempt_ref
    async for message, last in owner._stream_query_after_preflight(
        msgs,
        **query_args,
    ):
        yield message, last


async def _admit_recheck(recheck, session_execution, session_id, user_id):
    if recheck is None:
        return None
    if (
        session_execution is None
        or recheck.session_id != session_id
        or recheck.user_id != user_id
        or not await recheck.start()
    ):
        raise RecheckStartDenied()
    return recheck


async def _prepare_preflight_denial(
    owner,
    preflight,
    session_execution,
    session_id,
    user_id,
):
    response = preflight.response
    if response is None or not preflight.cleanup_denied_memory:
        return response
    cleanup_args = {
        "session_id": session_id,
        "user_id": user_id,
        "denial_response": response,
    }
    if session_execution is not None:
        cleanup_args["session_execution"] = session_execution
    await owner._cleanup_denied_session_memory(**cleanup_args)
    return response
