# -*- coding: utf-8 -*-
"""Abstract base class for SWE memory managers."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from contextvars import Context
from dataclasses import dataclass
from functools import partial
import itertools
import time
import weakref
from typing import TYPE_CHECKING, Any, Optional


from agentscope.formatter import FormatterBase
from agentscope.message import Msg
from agentscope.model import ChatModelBase
from agentscope.tool import ToolResponse

if TYPE_CHECKING:
    from reme.memory.file_based.reme_in_memory_memory import ReMeInMemoryMemory


from .summary_task_context import (
    create_summary_task,
    capture_summary_trace_context,
)
from swe.tracing.sanitizer import sanitize_trace_value

logger = logging.getLogger(__name__)
SUMMARY_RESULT_TTL_SECONDS = 300
SUMMARY_RESULT_SCOPE_LIMIT = 10
SUMMARY_RESULT_LIMIT = 256
SUMMARY_RESULT_MAX_BYTES = 512 * 1024
SUMMARY_CLOSE_GRACE_SECONDS = 5
SUMMARY_CLOSE_CANCEL_SECONDS = 5
_summary_managers = weakref.WeakSet()


@dataclass(frozen=True)
class _SummaryResult:
    task_id: int
    scope: str
    status: str
    finished_at: float
    expires_at: float
    message: str
    payload_bytes: int

    def text(self) -> str:
        if self.status == "cancelled":
            return "Summary task was cancelled.\n"
        return f"Summary task {self.status}: {self.message}\n"


async def prune_summary_task_results() -> None:
    """Prune bounded result histories without retaining managers across yields."""
    for manager_ref in [weakref.ref(m) for m in _summary_managers]:
        manager = manager_ref()
        if manager is not None:
            manager._prune_summary_results()
        del manager
        await asyncio.sleep(0)


def summary_task_stats() -> dict[str, int]:
    """Return aggregate counts without exposing scope identifiers."""
    managers = list(_summary_managers)
    return {
        "managers": len(managers),
        "pending": sum(
            sum(
                not t.done()
                for tasks in m._summary_tasks_by_scope.values()
                for t in tasks
            )
            for m in managers
        ),
        "completed": sum(len(m._summary_results) for m in managers),
        "payload_bytes": sum(m._summary_result_bytes for m in managers),
    }


class BaseMemoryManager(ABC):
    """Abstract base class defining the memory manager interface.

    All memory manager backends must implement this interface to be usable
    as a drop-in replacement within the SWE workspace.

    Concrete implementations are responsible for managing conversation memory,
    including compaction, summarization, semantic search, and lifecycle
    management.

    Attributes:
        working_dir: Working directory path for memory storage.
        agent_id: Unique agent identifier.
    """

    def __init__(
        self,
        working_dir: str,
        agent_id: str,
        tenant_id: str | None = None,
    ):
        """Initialize common memory manager attributes.

        Args:
            working_dir: Working directory path for memory storage.
            agent_id: Unique agent identifier.
            tenant_id: Optional tenant identifier for scoped config access.
        """
        self.working_dir: str = working_dir
        self.agent_id: str = agent_id
        self.tenant_id: str | None = tenant_id

        # 按会话隔离后台总结任务，避免不同对话互相等待或回收任务。
        self._summary_tasks_by_scope: dict[str, list[asyncio.Task]] = {}
        self._summary_results: OrderedDict[int, _SummaryResult] = OrderedDict()
        self._summary_result_bytes = 0
        self._summary_history_claims: dict[int, object] = {}
        self._summary_task_ids = itertools.count(1)
        self._summary_closing = False
        self._summary_close_lock = asyncio.Lock()
        self._summary_backend_closed = False
        _summary_managers.add(self)

    @abstractmethod
    async def start(self) -> None:
        """Start the memory manager lifecycle."""

    @abstractmethod
    async def close(self) -> bool:
        """Close the memory manager and perform cleanup."""

    @abstractmethod
    async def compact_tool_result(self, **kwargs) -> None:
        """Compact tool results by truncating large outputs.

        Args:
            **kwargs: Compaction parameters (messages, thresholds, etc.).
        """

    @abstractmethod
    async def check_context(self, **kwargs) -> tuple:
        """Check context size and determine if compaction is needed.

        Args:
            **kwargs: Context check parameters (messages, thresholds, etc.).

        Returns:
            Tuple of (messages_to_compact, remaining_messages, is_valid).
        """

    @abstractmethod
    async def compact_memory(
        self,
        messages: list[Msg],
        previous_summary: str = "",
        **kwargs,
    ) -> str:
        """Compact a list of messages into a condensed summary.

        Args:
            messages: List of messages to compact.
            previous_summary: Optional previous summary to incorporate.
            **kwargs: Additional keyword arguments.

        Returns:
            Condensed summary string, or empty string on failure.
        """

    @abstractmethod
    async def summary_memory(self, messages: list[Msg], **kwargs) -> str:
        """Generate a comprehensive summary of the given messages.

        Args:
            messages: List of messages to summarize.
            **kwargs: Additional keyword arguments.

        Returns:
            Comprehensive summary string.
        """

    @abstractmethod
    async def archive_checkpoint_messages(
        self,
        *,
        chat_id: str,
        messages: list[Msg],
        candidate_id: str,
        memory: Any,
    ) -> Any:
        """Archive source messages while activating a Chat checkpoint."""

    @abstractmethod
    async def schedule_precompaction(
        self,
        *,
        chat_id: str,
        watermark: int,
        messages: list[Msg],
        memory: Any,
        **kwargs,
    ) -> bool:
        """Schedule a non-blocking candidate for a stable Chat snapshot."""

    @abstractmethod
    async def install_ready_precompaction(
        self,
        *,
        chat_id: str,
        messages: list[Msg] | None = None,
        memory: Any,
    ) -> bool:
        """Install the newest valid candidate for a Chat, if available."""

    @abstractmethod
    async def recover_evidence(
        self,
        *,
        chat_id: str,
        epoch: int,
        **kwargs,
    ) -> Any:
        """Recover bounded evidence for the request-bound Chat and epoch."""

    @abstractmethod
    async def reset_context_epoch(
        self,
        *,
        chat_id: str,
        reason: str,
        memory: Any,
    ) -> Any:
        """Start a fresh Context Epoch without deleting Chat evidence."""

    @abstractmethod
    async def dream_memory(
        self,
        tenant_id: str | None = None,
        **kwargs,
    ) -> None:
        """Run one dream-based memory optimization task.

        This method performs dream-based memory optimization by executing
        an agent query with specific instructions for memory file optimization.

        Args:
            tenant_id: Optional tenant ID for tenant-scoped config lookup.
            **kwargs: Additional keyword arguments for the dream task.
        """

    def add_async_summary_task(
        self,
        messages: list[Msg],
        *,
        chat_model: Optional[ChatModelBase] = None,
        formatter: Optional[FormatterBase] = None,
        scope_id: str | None = None,
        **kwargs,
    ):
        """Start a summary with a detached, explicitly allowed context."""
        if self._summary_closing:
            raise RuntimeError("Memory manager is closing")
        scope = self._normalize_summary_scope(scope_id)
        task_id = next(self._summary_task_ids)
        kwargs["_summary_trace_context"] = capture_summary_trace_context()
        task = create_summary_task(
            self._run_summary(
                task_id,
                scope,
                messages,
                _bound_chat_model=chat_model,
                _bound_formatter=formatter,
                **kwargs,
            ),
        )
        task._swe_summary_task_id = task_id
        self._summary_tasks_by_scope.setdefault(scope, []).append(task)
        Context().run(
            task.add_done_callback,
            lambda done: self._settle_summary_task(done, scope, task_id),
        )

    async def _run_summary(self, task_id, scope, messages, **kwargs):
        task = asyncio.current_task()
        status, message = "completed", ""
        try:
            message = await self.summary_memory(messages=messages, **kwargs)
            return message
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status, message = "failed", exc
            raise
        finally:
            text = self._summary_status_message(message)
            now = time.monotonic()
            task._swe_summary_result = self._make_summary_result(
                task_id,
                scope,
                status,
                now,
                text,
            )

    @staticmethod
    def _make_summary_result(task_id, scope, status, now, text):
        expires_at = now + SUMMARY_RESULT_TTL_SECONDS
        fields = {
            "task_id": str(task_id),
            "scope": scope,
            "status": status,
            "finished_at": str(now),
            "expires_at": str(expires_at),
            "bounded_message": text,
        }
        payload_bytes = len(str(task_id).encode("utf-8")) + sum(
            len(key.encode("utf-8")) + len(value.encode("utf-8"))
            for key, value in fields.items()
        )
        return _SummaryResult(
            task_id,
            scope,
            status,
            now,
            expires_at,
            text,
            payload_bytes,
        )

    @staticmethod
    def _summary_status_message(message) -> str:
        try:
            safe = sanitize_trace_value(str(message or ""), max_bytes=1024)
            text = safe.value
            if safe.truncated:
                marker = " [truncated]"
                text = (
                    text.encode("utf-8")[: 1024 - len(marker)].decode(
                        "utf-8",
                        "ignore",
                    )
                    + marker
                )
            return text
        except Exception:
            return "<summary status unavailable>"

    def _settle_summary_task(self, task, scope, task_id):
        if getattr(task, "_swe_summary_settled", False):
            return task._swe_summary_result
        if not task.done():
            return None
        if not task.cancelled():
            exception = task.exception()
            if exception is None:
                task.result()
        record = getattr(task, "_swe_summary_result", None)
        if record is None:  # Cancellation before the coroutine's first step.
            now = time.monotonic()
            record = self._make_summary_result(
                task_id,
                scope,
                "cancelled",
                now,
                "",
            )
            task._swe_summary_result = record
        task._swe_summary_settled = True
        tasks = self._summary_tasks_by_scope.get(scope, [])
        tasks[:] = [candidate for candidate in tasks if candidate is not task]
        if not tasks:
            self._summary_tasks_by_scope.pop(scope, None)
        self._prune_summary_results()
        self._summary_results[task_id] = record
        self._summary_result_bytes += record.payload_bytes
        while (
            sum(r.scope == scope for r in self._summary_results.values())
            > SUMMARY_RESULT_SCOPE_LIMIT
        ):
            oldest = next(
                key
                for key, r in self._summary_results.items()
                if r.scope == scope
            )
            self._remove_summary_result(oldest)
        while self._summary_results and (
            len(self._summary_results) > SUMMARY_RESULT_LIMIT
            or self._summary_result_bytes > SUMMARY_RESULT_MAX_BYTES
        ):
            self._remove_summary_result(next(iter(self._summary_results)))
        return record

    def _remove_summary_result(self, task_id):
        record = self._summary_results.pop(task_id, None)
        if record is not None:
            self._summary_result_bytes -= record.payload_bytes
        self._summary_history_claims.pop(task_id, None)

    def _prune_summary_results(self):
        now = time.monotonic()
        for task_id, record in list(self._summary_results.items()):
            if record.expires_at <= now:
                self._remove_summary_result(task_id)

    def get_summary_task_count(self, scope_id: str | None = None) -> int:
        """Return only running summary tasks in this scope."""
        scope = self._normalize_summary_scope(scope_id)
        return sum(
            not task.done()
            for task in self._summary_tasks_by_scope.get(scope, ())
        )

    def has_summary_task_results(self, scope_id: str | None = None) -> bool:
        self._prune_summary_results()
        scope = self._normalize_summary_scope(scope_id)
        for task in list(self._summary_tasks_by_scope.get(scope, ())):
            if task.done():
                self._settle_summary_task(
                    task,
                    scope,
                    task._swe_summary_task_id,
                )
        return any(r.scope == scope for r in self._summary_results.values())

    async def await_summary_tasks(self, scope_id: str | None = None) -> str:
        """Await a fixed snapshot; cancellation leaves background work intact."""
        scope = self._normalize_summary_scope(scope_id)
        self._prune_summary_results()
        tasks = list(self._summary_tasks_by_scope.get(scope, ()))
        task_ids = {getattr(t, "_swe_summary_task_id", None) for t in tasks}
        claim = object()
        history = [
            r
            for r in self._summary_results.values()
            if r.scope == scope
            and r.task_id not in task_ids
            and r.task_id not in self._summary_history_claims
        ]
        for record in history:
            self._summary_history_claims[record.task_id] = claim
        results = list(history)
        try:
            for task in tasks:
                waiter = Context().run(
                    partial(
                        asyncio.gather,
                        task,
                        return_exceptions=True,
                    ),
                )
                await Context().run(asyncio.shield, waiter)
                results.append(
                    self._settle_summary_task(
                        task,
                        scope,
                        task._swe_summary_task_id,
                    ),
                )
        except BaseException:
            for record in history:
                if self._summary_history_claims.get(record.task_id) is claim:
                    self._summary_history_claims.pop(record.task_id, None)
            raise
        for record in results:
            self._remove_summary_result(record.task_id)
        return "".join(record.text() for record in results)

    async def _close_summary_tasks(self) -> bool:
        """Stop producers, then drain and cancel without abandoning live tasks."""
        self._summary_closing = True
        tasks = [
            t for group in self._summary_tasks_by_scope.values() for t in group
        ]
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=SUMMARY_CLOSE_GRACE_SECONDS,
            )
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(
                    pending,
                    timeout=SUMMARY_CLOSE_CANCEL_SECONDS,
                )
            if pending:
                logger.error(
                    "Summary shutdown timed out: %s task(s)",
                    len(pending),
                )
                return False
        for scope, group in list(self._summary_tasks_by_scope.items()):
            for task in list(group):
                self._settle_summary_task(
                    task,
                    scope,
                    task._swe_summary_task_id,
                )
        self._summary_results.clear()
        self._summary_history_claims.clear()
        self._summary_result_bytes = 0
        return True

    @property
    def summary_tasks(self) -> list[asyncio.Task]:
        """Compatibility view without creating empty scopes."""
        return self._summary_tasks_by_scope.get(
            self._normalize_summary_scope(None),
            [],
        )

    @staticmethod
    def _normalize_summary_scope(scope_id: str | None) -> str:
        """规范化 summary 任务作用域键。"""
        normalized = str(scope_id or "").strip()
        return normalized or "__default__"

    @abstractmethod
    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ) -> ToolResponse:
        """Search stored memories for relevant content.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return.
            min_score: Minimum relevance score threshold.

        Returns:
            ToolResponse containing search results.
        """

    @abstractmethod
    def get_in_memory_memory(self, **kwargs) -> "ReMeInMemoryMemory | None":
        """Retrieve the in-memory memory object for the agent.

        Args:
            **kwargs: Additional keyword arguments.

        Returns:
            In-memory memory instance.
        """
