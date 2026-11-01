# -*- coding: utf-8 -*-
"""Await durable recovery publication inside the Agent's execution boundary."""

import asyncio
from contextlib import suppress
from copy import deepcopy
import inspect

from .errors import StorageError
from .session_adapter import SemanticBatch


class SemanticRuntimePublisher:
    """Publish complete roots and transient streaming occurrences under one lock."""

    def __init__(self, agent, execution, root_provider, *, batch=None):
        self.agent = agent
        self.execution = execution
        self.root_provider = root_provider
        self.batch = batch or SemanticBatch(64 * 1024, 64, 1.0)
        self.lock = execution.mutation_lock
        self.error = None
        self.timer = None
        self.timer_publishing = False
        self.producer = None
        self.producers = set()
        self.pending = []
        self.previous = None
        self.closed = False
        self.acting_override = agent.__dict__.get("_acting")
        self.acting = agent._acting
        self.failure_check_override = agent.__dict__.get(
            "_conversation_check_tool_failure",
        )

    def install(self):
        for kind, callback in (
            ("pre_print", self.pre_print),
            ("pre_reasoning", self.pre_step),
            ("post_reasoning", self.post_step),
            ("post_reply", self.post_step),
        ):
            self.agent.register_instance_hook(
                kind,
                "conversation_durable",
                callback,
            )
        # ToolGuardMixin's inherited _acting is not wrapped by AgentScope's
        # metaclass. Wrap the actual dispatch target so guard/approval paths
        # have the same durable boundary as ReActAgent's tool implementation.
        self.agent._acting = self._acting
        self.agent._conversation_check_tool_failure = self.check_tool_failure
        return self

    async def _acting(self, tool_call):
        task = asyncio.current_task()
        self.producers.add(task)
        await self.pre_step(self.agent, {})
        result = await self.acting(tool_call)
        await self.post_step(self.agent, {}, result)
        return result

    def check(self):
        if self.error is not None:
            raise self.error
        if self.execution.pending_operation is not None:
            from .errors import UnknownCommit

            raise UnknownCommit(self.execution.pending_operation)

    def check_tool_failure(self, error):
        """Fence configured external failure hooks after a storage failure."""
        if isinstance(error, StorageError) and self.error is None:
            self.error = error
        self.check()
        if self.closed:
            raise asyncio.CancelledError()

    async def _root(self):
        root = self.root_provider()
        if inspect.isawaitable(root):
            root = await root
        root = deepcopy(root)
        content = (
            root.setdefault("agent", {})
            .setdefault("memory", {})
            .setdefault("content", [])
        )
        retained = []
        for item in self.pending:
            # A matching occurrence can only be this source's append at/after
            # its original memory tail. Historical duplicate IDs stay intact.
            represented = any(
                row[0] == item["message"] for row in content[item["start"] :]
            )
            if not represented:
                retained.append(item)
        self.pending = retained
        content.extend([[item["message"], []] for item in retained])
        return root

    async def snapshot(self):
        """Keep committed streamed occurrences when terminal memory is partial."""
        async with self.lock:
            return await self._root()

    async def _publish(self):
        if self.closed:
            raise asyncio.CancelledError()
        self.check()
        try:
            root = await self._root()
            comparable = {
                key: value for key, value in root.items() if key != "revision"
            }
            if comparable != self.previous:
                await self.execution.commit_state(root)
                self.previous = deepcopy(comparable)
            self.batch.reset()
        except BaseException as error:
            self.error = error
            raise

    async def pre_print(self, agent, kwargs):
        async with self.lock:
            if self.closed:
                raise asyncio.CancelledError()
            self.check()
            self.producer = asyncio.current_task()
            self.producers.add(self.producer)
            message = kwargs["msg"].to_dict()
            source = (self.producer, message["id"])
            # Hooks on overriding methods and super() both run. Count one
            # printed event while retaining distinct occurrences of an ID.
            item = next(
                (item for item in self.pending if item["source"] == source),
                None,
            )
            last = kwargs.get("last", True)
            if (
                item is not None
                and item["message"] == message
                and item["last"] == last
            ):
                return
            text = kwargs["msg"].get_text_content() or ""
            prior = item["text"] if item is not None else ""
            if item is None:
                item = {
                    "source": source,
                    "start": len(getattr(agent.memory, "content", [])),
                }
                self.pending.append(item)
            item.update(message=message, last=last, text=text)
            delta = text[len(prior) :] if text.startswith(prior) else text
            if self.batch.add(delta, semantic=last):
                await self._publish()
            elif self.timer is None or self.timer.done():
                self.timer = asyncio.create_task(self._flush_after_delay())

    async def pre_step(self, agent, kwargs):
        async with self.lock:
            self.producer = asyncio.current_task()
            self.producers.add(self.producer)
            await self._publish()

    async def post_step(self, agent, kwargs, output):
        async with self.lock:
            await self._publish()

    async def _flush_after_delay(self):
        try:
            await asyncio.sleep(self.batch.max_seconds)
            async with self.lock:
                if not self.closed and self.batch.event_count:
                    self.timer_publishing = True
                    try:
                        await self._publish()
                    finally:
                        self.timer_publishing = False
        except asyncio.CancelledError:
            raise
        except Exception:
            # A stalled model must stop too. The sticky original exception is
            # raised by its next hook or by the request stream's check().
            for producer in self.producers:
                if producer is not None and not producer.done():
                    producer.cancel()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        producers = [
            task
            for task in self.producers
            if task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ]
        for task in producers:
            task.cancel()
        if producers:
            await asyncio.gather(*producers, return_exceptions=True)
        if self.timer is not None:
            if not self.timer_publishing:
                self.timer.cancel()
            with suppress(asyncio.CancelledError):
                await self.timer
        for kind in (
            "pre_print",
            "pre_reasoning",
            "post_reasoning",
            "post_reply",
        ):
            self.agent.remove_instance_hook(kind, "conversation_durable")
        if self.acting_override is None:
            del self.agent._acting
        else:
            self.agent._acting = self.acting_override
        if self.failure_check_override is None:
            del self.agent._conversation_check_tool_failure
        else:
            self.agent._conversation_check_tool_failure = (
                self.failure_check_override
            )
