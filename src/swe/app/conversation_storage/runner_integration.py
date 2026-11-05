# -*- coding: utf-8 -*-
"""Request-owned SQL admission and terminal publication for the Runner."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from anyio import fail_after

from .bundle import new_operation_key
from .errors import (
    ConflictError,
    IntegrityError,
    StorageError,
    StorageUnavailable,
    UnknownCommit,
)
from .execution import ExecutionAuthority
from .publications import _request

logger = logging.getLogger(__name__)


class IndexedQuery:
    """Keep execution authority outside SQL transactions and final frames."""

    def __init__(self, storage, owner, request, execution):
        self.storage, self.owner = storage, owner
        self.request, self.execution = request, execution
        self.session = storage.session_for(request.channel or "console")
        self.authority = None
        self.observation = None
        self.pending_operation = None
        self.pending_request = None
        self.terminal_operation = None
        self.terminal_status = None
        self.renew_error = None
        self.stop_requested = False
        self.runtime = None
        self.semantic_publisher = None
        self.question_key = None
        self.chat = None
        self.request_ended = False
        self._admission_args = None

    @asynccontextmanager
    async def admit(self, messages):
        try:
            async with self.storage.executions.renewal_capacity():
                async with self._admit(messages):
                    yield self
        finally:
            # The caller closes its source first; _admit drains its renewer and
            # every PublicationStore NAS operation drains cancellation workers.
            self.request_ended = True

    def _register(self):
        self.execution.indexed_query = self
        self.execution.defer_close = True
        identity = (
            self.owner._answer_turn_identity(self.request)
            if hasattr(self.owner, "_answer_turn_identity")
            else None
        )
        if identity is not None:
            self.owner._answer_turn_runtimes[identity] = (None, self.execution)

    @asynccontextmanager
    async def _admit(self, messages):
        if not messages or getattr(messages[-1], "role", None) != "user":
            raise ValueError(
                "SQL admission requires the submitted user message",
            )
        question = messages[-1].to_dict()
        self.question_key = question["id"]
        runtime_key = (self.request.channel_meta or {}).get("turn_id")
        runtime_key = runtime_key or "turn-" + uuid4().hex
        self.chat = await self.owner._get_or_create_chat(
            session_id=self.request.session_id,
            user_id=self.request.user_id,
            channel=self.request.channel or "console",
            name="",
            request=self.request,
            turn_id=runtime_key,
        )
        self._admission_args = {
            "operation_key": new_operation_key(),
            "runtime_turn_key": runtime_key,
            "expected_state_revision": self.execution.revision,
        }
        try:
            admission = await self.storage.executions.reserve_admission(
                self.execution.identity,
                self.chat.id,
                self.question_key,
                **self._admission_args,
            )
        except UnknownCommit as error:
            self._register()
            self._unknown(error)
            raise
        if not admission.created:
            self.observation = admission
            yield self
            return
        self.authority = admission.authority
        self._register()
        parent = asyncio.current_task()
        renewer = asyncio.create_task(self._renew(parent))
        try:
            state = deepcopy(self.execution.state)
            state.setdefault("turn_states", {})[self.question_key] = {
                "status": "admitted",
                "message": question,
                "chat_id": self.chat.id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            state["revision"] = self.execution.revision + 1
            try:
                await self.storage.executions.publish_admission(
                    admission,
                    question,
                    state,
                )
                self.execution.accept_committed_state(state)
                await self.storage.executions.start(self.authority)
            except (Exception, asyncio.CancelledError) as error:
                self._unknown(
                    (
                        error
                        if isinstance(error, UnknownCommit)
                        else UnknownCommit(
                            self._admission_args["operation_key"],
                        )
                    ),
                )
                raise
            async with self.session.bind_authority(self.authority):
                yield self
        except asyncio.CancelledError:
            if self.renew_error is not None:
                raise self.renew_error
            raise
        finally:
            renewer.cancel()
            with suppress(asyncio.CancelledError):
                await renewer

    def _unknown(self, error):
        if self.pending_operation != error.operation_key:
            self.pending_request = None
        self.pending_operation = error.operation_key
        self.execution.pending_operation = error.operation_key
        self.pending_request = (
            error.publication_request or self.pending_request
        )
        if self.pending_request is not None:
            self.execution.pending_request = self.pending_request

    async def _renew(self, parent):
        loop = asyncio.get_running_loop()
        interval = self.storage.executions.lease_seconds / 3
        next_attempt = loop.time() + interval
        unavailable = False
        while True:
            await asyncio.sleep(max(0, next_attempt - loop.time()))
            started = loop.time()
            try:
                await self.storage.executions.renew(self.authority)
            except Exception as error:
                if isinstance(error, UnknownCommit) or not isinstance(
                    error,
                    StorageUnavailable,
                ):
                    if await self._own_terminal_committed():
                        return
                    self.renew_error = error
                    if parent is not None:
                        parent.cancel()
                    return
                if not unavailable:
                    logger.warning(
                        "Conversation heartbeat unavailable; execution continues under generation checks",
                        exc_info=True,
                    )
                unavailable = True
                # An outage never causes catch-up attempts or a tight retry loop.
                next_attempt = loop.time() + interval
                continue
            if unavailable:
                logger.info("Conversation heartbeat recovered")
                unavailable = False
            # Do not accumulate SQL duration, or burst after a delayed attempt.
            next_attempt = started + interval
            if next_attempt <= loop.time():
                next_attempt = loop.time() + interval

    async def _own_terminal_committed(self):
        # Terminal SQL releases ownership before asynchronous reader cleanup.
        # Only this request's exact committed terminal explains renewal loss.
        if self.terminal_operation is None:
            return False
        try:
            with (
                fail_after(self.storage.executions.lease_seconds / 3),
                self.storage.db.control_priority(),
            ):
                row = await self.storage.publications.get(
                    self.terminal_operation,
                )
            if row is None or (
                row["state"] != "committed" or row["kind"] != "terminal"
            ):
                return False
            self._validate_pending_request(_request(row))
            return True
        except Exception:
            return False

    async def bind_runtime(self, runtime):
        """Snapshot the current Agent plus every unknown recovery module."""
        from .semantic_runtime import SemanticRuntimePublisher

        if self.semantic_publisher is not None:
            await self.semantic_publisher.close()
            self.semantic_publisher.check()
        self.runtime = runtime

        def root_provider():
            state = deepcopy(self.execution.state)
            state["agent"] = runtime.agent.state_dict()
            return state

        self.semantic_publisher = SemanticRuntimePublisher(
            runtime.agent,
            self.execution,
            root_provider,
            batch=getattr(self, "semantic_batch", None),
        ).install()
        self.storage.archive.bind_memory(
            runtime.agent.memory,
            runtime.chat.id,
            self.execution,
            self.semantic_publisher._root,
            self.authority,
        )

    async def finish(self, status="completed"):
        if self.terminal_status is not None:
            if self.terminal_status != status:
                raise ConflictError(
                    "Turn terminal state cannot be overwritten",
                )
            return
        pending = self.pending_operation or self.execution.pending_operation
        if pending is not None:
            await self._confirm_pending_terminal(pending)
            return
        publisher = getattr(self, "semantic_publisher", None)
        state = (
            await publisher.snapshot()
            if publisher is not None
            else deepcopy(self.execution.state)
        )
        if self.runtime is not None and publisher is None:
            state["agent"] = self.runtime.agent.state_dict()
        state.setdefault("turn_states", {}).setdefault(
            self.question_key,
            {},
        )["status"] = status
        state["revision"] = self.execution.revision + 1
        self.terminal_operation = (
            self.terminal_operation or new_operation_key()
        )
        try:
            await self.storage.executions.finish(
                self.authority,
                status,
                state,
                operation_key=self.terminal_operation,
            )
        except UnknownCommit as error:
            self._unknown(error)
            raise
        self.execution.accept_committed_state(state)
        self.terminal_status = status

    async def _confirm_pending_terminal(self, operation_key):
        """Observe completed reconciliation without touching a closed snapshot."""
        row = await self.storage.publications.get(operation_key)
        if self.request_ended:
            row = await self._settle_pending(operation_key, row)
            if self.terminal_status == "rejected":
                return
        if row is None or row["state"] not in ("committed", "aborted"):
            raise UnknownCommit(operation_key)
        authority = self.authority
        expected = (
            *authority.identity.scope.sql_values,
            authority.session_id,
            authority.chat_id,
            authority.turn_id,
        )
        actual = tuple(
            row[key]
            for key in (
                "service_unit_key",
                "tenant_key",
                "source_key",
                "agent_key",
                "session_id",
                "chat_id",
                "turn_id",
            )
        )
        if actual != expected:
            raise IntegrityError(
                "pending operation execution identity mismatch",
            )
        turn = await self.storage.db.fetch_one(
            "SELECT t.session_id,t.chat_id,t.generation,t.state,t.history_status,"
            "p.state AS publication_state,p.kind AS publication_kind,"
            "p.session_id AS publication_session_id,p.chat_id AS publication_chat_id,"
            "p.turn_id AS publication_turn_id FROM swe_conversation_turns t "
            "JOIN swe_conversation_publications p ON p.id=t.terminal_publication_id "
            "WHERE t.id=%s AND p.service_unit_key=%s AND p.tenant_key=%s AND p.source_key=%s AND p.agent_key=%s",
            (authority.turn_id, *authority.identity.scope.sql_values),
        )
        if turn is None or turn["publication_state"] != "committed":
            raise UnknownCommit(operation_key)
        associations = (
            turn["session_id"],
            turn["chat_id"],
            turn["generation"],
            turn["publication_session_id"],
            turn["publication_chat_id"],
            turn["publication_turn_id"],
            turn["publication_kind"],
        )
        if associations != (
            authority.session_id,
            authority.chat_id,
            authority.generation,
            authority.session_id,
            authority.chat_id,
            authority.turn_id,
            "terminal",
        ):
            raise IntegrityError(
                "reconciled terminal execution identity mismatch",
            )
        statuses = {
            "succeeded": "completed",
            "failed": "failed",
            "stopped": "stopped",
            "interrupted": "interrupted",
        }
        if (
            turn["state"] not in statuses
            or statuses[turn["state"]] != turn["history_status"]
        ):
            raise IntegrityError("reconciled terminal history status mismatch")
        self.terminal_status = turn["history_status"]
        self.pending_operation = self.execution.pending_operation = None
        self.pending_request = self.execution.pending_request = None

    async def _settle_pending(self, operation_key, row):
        """Use ended local request evidence; lease expiry is never death proof."""
        if self.authority is None:
            if row is None:
                if self._admission_args is None:
                    raise UnknownCommit(operation_key)
                result = await self.storage.executions.reserve_admission(
                    self.execution.identity,
                    self.chat.id,
                    self.question_key,
                    **self._admission_args,
                )
                row = result.publication
                if (
                    row is None
                    or row["operation_key"].decode("ascii") != operation_key
                ):
                    raise UnknownCommit(operation_key)
            self.authority = ExecutionAuthority(
                self.execution.identity,
                row["session_id"],
                row["chat_id"],
                row["turn_id"],
                row["generation"],
                row["owner_key"].decode("utf-8"),
            )
        if row is None:
            request = self.pending_request or getattr(
                self.execution,
                "pending_request",
                None,
            )
            if request is None or request.operation_key != operation_key:
                raise UnknownCommit(operation_key)
            self._validate_pending_request(request)
            await self.storage.publications.abort(request, owner_stopped=True)
            row = await self.storage.publications.get(operation_key)
            if row is None:
                raise UnknownCommit(operation_key)
        request = _request(row)
        self._validate_pending_request(request)
        authority = self.authority
        if row["state"] == "prepared":
            row = await self._settle_prepared(operation_key, row, request)
            if self.terminal_status == "rejected":
                return row
        if row["kind"] == "terminal" and row["state"] == "committed":
            return row
        if row["kind"] == "admission" and row["state"] == "aborted":
            await self.storage.executions.reject_ended_admission(
                authority,
                request,
                owner_stopped=True,
            )
            self.terminal_status = "rejected"
            self.pending_operation = self.execution.pending_operation = None
            return row
        if self.terminal_operation == operation_key:
            self.terminal_operation = None
        # Only confirmed durable history is used. The complete root, including
        # modules unknown to this version, is loaded through the normal reader.
        state = await self.session.get_session_state_dict(
            authority.identity.session_id,
            authority.identity.user_id,
        )
        self.terminal_operation = (
            self.terminal_operation or new_operation_key()
        )
        try:
            terminal = await self.storage.executions.settle_ended_terminal(
                authority,
                state,
                operation_key=self.terminal_operation,
                owner_stopped=True,
            )
        except UnknownCommit as error:
            self._unknown(error)
            raise
        self.pending_operation = self.execution.pending_operation = (
            self.terminal_operation
        )
        return terminal

    async def _settle_prepared(self, operation_key, row, request):
        authority = self.authority
        if not row.get("durable_at"):
            try:
                await self.storage.publications.confirm_bundle(
                    operation_key,
                )
            except FileNotFoundError:
                if row["kind"] == "admission":
                    await self.storage.executions.reject_ended_admission(
                        authority,
                        request,
                        owner_stopped=True,
                    )
                    self.terminal_status = "rejected"
                    self.pending_operation = (
                        self.execution.pending_operation
                    ) = None
                    return await self.storage.publications.get(
                        operation_key,
                    )
                await self.storage.publications.abort(
                    request,
                    owner_stopped=True,
                )
                row = await self.storage.publications.get(operation_key)
        if row["state"] == "prepared":
            session = await self.session._row(authority.identity)
            try:
                row = await self.storage.publications.reconcile(
                    operation_key,
                    owner_stopped=True,
                    expected_generation=session["generation"],
                )
            except ConflictError:
                # A Stop or changed CAS baseline can reject the immutable
                # result. Retain its body and fence its original key.
                row = await self.storage.publications.abort(
                    request,
                    owner_stopped=True,
                )
        return row

    def _validate_pending_request(self, request):
        authority = self.authority
        if request.scope != authority.identity.scope or (
            request.session_id,
            request.chat_id,
            request.turn_id,
            request.generation,
            request.owner_key,
        ) != (
            authority.session_id,
            authority.chat_id,
            authority.turn_id,
            authority.generation,
            authority.owner_key,
        ):
            raise IntegrityError(
                "pending operation execution identity mismatch",
            )

    async def stream(self, source):
        try:
            async for frame in self._stream(source):
                yield frame
        finally:
            if self.semantic_publisher is not None:
                await self.semantic_publisher.close()

    async def _stream(self, source):
        pending = None
        try:
            async for message, last in source:
                if self.semantic_publisher is not None:
                    self.semantic_publisher.check()
                if pending is not None:
                    yield pending[0], False
                    pending = None
                if last:
                    pending = (message, last)
                else:
                    yield message, last
            if self.semantic_publisher is not None:
                await self.semantic_publisher.close()
                self.semantic_publisher.check()
            if self.terminal_status is None:
                await self.finish(
                    "stopped" if self.stop_requested else "completed",
                )
        except UnknownCommit as error:
            self._unknown(error)
            raise
        except StorageError:
            raise
        except asyncio.CancelledError:
            if self.semantic_publisher is not None:
                await self.semantic_publisher.close()
                self.semantic_publisher.check()
            if self.renew_error is not None:
                raise self.renew_error
            if self.terminal_status is None:
                await self.finish(
                    "stopped" if self.stop_requested else "interrupted",
                )
            raise
        except Exception:
            if self.terminal_status is None:
                await self.finish("failed")
            raise
        settle = getattr(self.owner, "_settle_stopped_turn", None)
        if self.terminal_status == "stopped" and settle is not None:
            await settle(
                runtime=self.runtime,
                request=self.request,
                session_execution=self.execution,
            )
        if pending is not None:
            yield pending


async def _observe(query):
    from agentscope.message import Msg

    turn = query.observation.turn
    if turn["state"] == "rejected":
        raise ConflictError(
            "original question admission was rejected; submit a new question",
        )
    if turn["state"] not in (
        "succeeded",
        "failed",
        "stopped",
        "interrupted",
    ):
        raise ConflictError(
            "original Turn is active or settling; reconnect is required",
        )
    history = await query.storage.history.answer_turn(
        query.question_key,
        chat_id=query.chat.id,
        user_id=query.request.user_id,
    )
    for index, message in enumerate(history.messages):
        if message.role == "user":
            continue
        yield Msg.from_dict(message.model_dump(mode="json")), (
            index == len(history.messages) - 1
        )


async def _preflight(owner, request, query, execution):
    return await owner._prepare_query_preflight(
        session_id=request.session_id,
        user_id=request.user_id,
        query=query,
        request=request,
        session_execution=execution,
    )


async def _stream_admitted(
    owner,
    messages,
    request,
    query,
    execution,
    indexed,
):
    preflight = await _preflight(owner, request, query, execution)
    if preflight.response is not None:
        if preflight.cleanup_denied_memory:
            await owner._cleanup_denied_session_memory(
                session_id=request.session_id,
                user_id=request.user_id,
                denial_response=preflight.response,
                session_execution=execution,
            )
        await indexed.finish("failed")
        yield preflight.response, True
        return
    async for frame in owner._stream_query_after_preflight(
        messages,
        request=request,
        query=query,
        session_id=request.session_id,
        preflight=preflight,
        session_execution=execution,
    ):
        yield frame


async def _stream_command(owner, storage, messages, request, query, execution):
    from ..runner.command_dispatch import run_command_path

    preflight = await _preflight(owner, request, query, execution)
    if preflight.response is not None:
        yield preflight.response, True
        return
    pending = None
    async for message, last in run_command_path(
        request,
        messages,
        owner,
        session_execution=execution,
    ):
        if pending is not None:
            yield pending
        pending = (message, last) if last else None
        if not last:
            yield message, last
    if pending is not None:
        yield pending


async def stream_indexed_query(owner, storage, messages, *, request, query):
    """Reuse the Runner flow under SQL admission and request-owned state."""
    from ..runner.command_dispatch import _is_command

    adapter = storage.session_for(request.channel or "console")
    async with adapter.execution(
        request.session_id,
        user_id=request.user_id,
    ) as execution:
        if query and _is_command(query):
            async for frame in _stream_command(
                owner,
                storage,
                messages,
                request,
                query,
                execution,
            ):
                yield frame
            return
        indexed = IndexedQuery(storage, owner, request, execution)
        async with indexed.admit(messages):
            if indexed.observation is not None:
                async for frame in _observe(indexed):
                    yield frame
                return
            source = _stream_admitted(
                owner,
                messages,
                request,
                query,
                execution,
                indexed,
            )
            try:
                async for frame in indexed.stream(source):
                    yield frame
            finally:
                await source.aclose()
