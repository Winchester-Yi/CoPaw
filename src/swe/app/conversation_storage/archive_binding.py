# -*- coding: utf-8 -*-
"""Request-owned memory wrappers for atomic indexed archive publications."""

import inspect
from copy import deepcopy
from dataclasses import dataclass

from ...agents.memory.chat_checkpoint import render_checkpoint_projection
from .errors import ConflictError, UnknownCommit


@dataclass
class ArchiveBinding:
    store: object
    memory: object
    chat_id: str
    execution: object
    root_provider: object
    authority: object

    async def _root(self, content, summary):
        previous = self.memory.content
        old_summary = getattr(self.memory, "_compressed_summary", "")
        self.memory.content = content
        self.memory._compressed_summary = summary
        try:
            result = self.root_provider()
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict):
                raise ValueError(
                    "archive root provider must return complete state",
                )
            return deepcopy(result)
        finally:
            self.memory.content = previous
            self.memory._compressed_summary = old_summary

    def _retained(self, messages):
        entries = list(self.memory.content)
        selected = set()
        for message in messages:
            matches = [
                i
                for i, (value, _) in enumerate(entries)
                if i not in selected and value is message
            ]
            if not matches:
                matches = [
                    i
                    for i, (value, _) in enumerate(entries)
                    if i not in selected
                    and value.to_dict() == message.to_dict()
                ]
            if len(matches) != 1:
                raise ConflictError(
                    "compact source occurrence is ambiguous or absent",
                )
            selected.add(matches[0])
        return [entry for i, entry in enumerate(entries) if i not in selected]

    async def compact(self, messages, *, summary=None, candidate_id=None):
        retained = self._retained(messages)
        if candidate_id is not None:
            candidate = await self.store.read_candidate(
                self.chat_id,
                candidate_id,
            )
            summary = render_checkpoint_projection(candidate.record, ())
        summary = (
            getattr(self.memory, "_compressed_summary", "")
            if summary is None
            else summary
        )
        root = await self._root(retained, summary)
        result = await self.store.compact(
            self.chat_id,
            messages,
            recovery_state=root,
            authority=self.authority,
            candidate_id=candidate_id,
        )
        self.memory.content = retained
        self.memory._compressed_summary = summary
        self.execution.accept_committed_state(result.recovery_state)
        return result

    async def reset(self, *, reason):
        root = await self._root([], "")
        result = await self.store.reset(
            self.chat_id,
            recovery_state=root,
            authority=self.authority,
            reason=reason,
        )
        self.memory.content = []
        self.memory._compressed_summary = ""
        self.memory._chat_checkpoint_epoch = result.state.current_epoch
        self.execution.accept_committed_state(result.recovery_state)
        return result.state

    async def append(self, message):
        root = await self._root(
            self.memory.content,
            getattr(self.memory, "_compressed_summary", ""),
        )
        result = await self.store.append_message(
            self.chat_id,
            message,
            recovery_state=root,
            authority=self.authority,
        )
        self.execution.accept_committed_state(result.recovery_state)


def attach_bound_memory(memory, store, chat_id):
    """Attach explicit indexed methods; authority is supplied per request later."""
    if getattr(memory, "_indexed_archive_store", None) is store:
        return memory
    memory._indexed_archive_store = store
    memory._chat_checkpoint_chat_id = chat_id
    memory.chat_checkpoint_store = store
    memory.conversation_archive_store = store
    original_add = getattr(memory, "add", None)

    def binding():
        result = getattr(memory, "_conversation_archive_binding", None)
        if result is None:
            raise ConflictError(
                "indexed memory requires request execution binding",
            )
        return result

    def serialized(function):
        async def call(*args, **kwargs):
            execution = binding().execution
            async with execution.mutation_lock:
                if execution.pending_operation is not None:
                    raise UnknownCommit(execution.pending_operation)
                try:
                    return await function(*args, **kwargs)
                except UnknownCommit as error:
                    execution.pending_operation = error.operation_key
                    execution.pending_request = error.publication_request
                    raise

        return call

    async def archive(messages, *, summary=None):
        return (await binding().compact(messages, summary=summary)).boundary

    async def checkpoint(messages, candidate_id):
        return await binding().compact(messages, candidate_id=candidate_id)

    async def ready(messages):
        candidate_id = getattr(memory, "_checkpoint_candidate_id", None)
        if candidate_id is None:
            return False
        await binding().compact(messages, candidate_id=candidate_id)
        del memory._checkpoint_candidate_id
        return True

    async def projection(record=None):
        if record is not None:
            root = await binding()._root(
                memory.content,
                render_checkpoint_projection(record, ()),
            )
            result = await store.write_active_checkpoint(
                chat_id,
                record,
                recovery_state=root,
                authority=binding().authority,
            )
            binding().execution.accept_committed_state(result.recovery_state)
        state = await store.read_checkpoint_projection(chat_id)
        memory._compressed_summary = render_checkpoint_projection(
            state.record,
            state.events[-5:],
        )
        return state.record

    async def add(*args, **kwargs):
        result = original_add(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        message = args[0] if args else kwargs.get("message")
        await binding().append(message)
        return result

    async def reset(*, reason):
        return await binding().reset(reason=reason)

    async def degraded(messages):
        from dataclasses import replace
        from ...agents.memory.chat_checkpoint import EvidenceItem

        active = await store.read_checkpoint_record(chat_id)
        refs = tuple(f"message:{message.id}" for message in messages)
        record = replace(
            active,
            revision=active.revision + 1,
            source_revision=active.revision,
            confidence="degraded",
            critical_context=(
                *active.critical_context,
                EvidenceItem(
                    "Emergency compaction retained evidence references",
                    refs,
                ),
            ),
        )
        return await projection(record)

    async def recover(**kwargs):
        return await store.recover_evidence(chat_id=chat_id, **kwargs)

    memory.archive_compacted_messages = serialized(archive)
    memory.archive_checkpoint_messages = serialized(checkpoint)
    memory.commit_ready_precompaction = serialized(ready)
    memory.install_checkpoint_projection = serialized(projection)
    memory.install_degraded_checkpoint = serialized(degraded)
    memory.reset_context_epoch = serialized(reset)
    memory.recover_evidence = recover
    if original_add is not None:
        memory.add = serialized(add)
    return memory
