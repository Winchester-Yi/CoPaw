# -*- coding: utf-8 -*-
"""Admission ordering for one query execution."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from swe.tracing.models import TraceStatus
from swe.tracing.agent_trace_sdk import start_child_span
from ...crons.recheck_context import RecheckStartDenied, active_recheck

from ..command_dispatch import _is_command, run_command_path
from ..query_attempt import _query_session_execution
from ..session_lifecycle import admit_user_turn


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
    workspace = getattr(owner, "_workspace", None)
    storage = getattr(workspace, "conversation_storage", None)
    if storage is not None:
        from ...conversation_storage.runner_integration import (
            stream_indexed_query,
        )

        async for frame in stream_indexed_query(
            owner,
            storage,
            msgs,
            request=request,
            query=query,
        ):
            yield frame
        return
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
            if session_execution is not None and not is_command:
                await admit_user_turn(
                    session_execution,
                    msgs=msgs,
                    request=request,
                )
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
