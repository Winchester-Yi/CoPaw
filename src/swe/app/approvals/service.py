# -*- coding: utf-8 -*-
"""Approval service for sensitive tool execution.

The ``ApprovalService`` is the single central store for pending /
completed approval records.  Approval is granted exclusively via
the ``/daemon approve`` command in the chat interface.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...constant import TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS
from ...security.tool_guard.approval import ApprovalDecision

if TYPE_CHECKING:
    from ...security.tool_guard.models import ToolGuardResult

logger = logging.getLogger(__name__)

_GC_MAX_AGE_SECONDS = 3600.0
_GC_MAX_COMPLETED = 500
_GC_PENDING_MAX_AGE_SECONDS = max(
    1800.0,
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
)
_GC_MAX_PENDING = 200
_EXTERNAL_SUBMISSION_EXTRA_KEY = "external_submission"


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------


@dataclass
class PendingApproval:
    """In-memory record for one pending approval."""

    request_id: str
    session_id: str
    scope_id: str
    user_id: str
    channel: str
    tool_name: str
    created_at: float
    future: asyncio.Future[ApprovalDecision]
    status: str = "pending"
    resolved_at: float | None = None
    result_summary: str = ""
    findings_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    consumed: bool = False


# ------------------------------------------------------------------
# Service
# ------------------------------------------------------------------


class ApprovalService:
    """Central approval service.

    Tracks pending and completed approval records.  Approval is
    resolved via ``/daemon approve`` (see ``runner.py`` and
    ``daemon_commands.py``).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pending: dict[str, PendingApproval] = {}
        self._completed: dict[str, PendingApproval] = {}
        self._channel_manager: Any | None = None
        self._store: Any | None = None

    def set_channel_manager(self, channel_manager: Any) -> None:
        """Store a reference to the channel manager for push notifications."""
        self._channel_manager = channel_manager

    def set_store(self, store: Any | None) -> None:
        """设置审批审计存储。"""
        self._store = store

    def _get_current_scope_id(self) -> str | None:
        """Resolve the current runtime scope for approval isolation."""
        try:
            from ...config.context import (
                canonicalize_scope_id,
                get_current_effective_tenant_id,
                get_current_scope_id,
            )

            scope_id = get_current_scope_id()
            if scope_id is not None:
                return canonicalize_scope_id(scope_id)
            return get_current_effective_tenant_id()
        except Exception:
            return None

    def _matches_scope(self, pending: PendingApproval) -> bool:
        """Check whether a pending/completed record is visible in scope."""
        scope_id = self._get_current_scope_id()
        return scope_id is not None and pending.scope_id == scope_id

    @staticmethod
    def _debug_record_summary(
        pending: PendingApproval | None,
    ) -> dict[str, Any] | None:
        """Return a log-safe approval summary without tool input/output."""
        if pending is None:
            return None
        extra = pending.extra if isinstance(pending.extra, dict) else {}
        tool_call = extra.get("tool_call")
        if not isinstance(tool_call, dict):
            tool_call = {}
        return {
            "request_id": pending.request_id,
            "scope_id": pending.scope_id,
            "session_id": pending.session_id,
            "user_id": pending.user_id,
            "channel": pending.channel,
            "tool_name": pending.tool_name,
            "status": pending.status,
            "created_at": pending.created_at,
            "resolved_at": pending.resolved_at,
            "consumed": pending.consumed,
            "extra_tenant_id": extra.get("tenant_id"),
            "extra_source_id": extra.get("source_id"),
            "extra_agent_id": extra.get("agent_id"),
            "approval_kind": extra.get("approval_kind"),
            "tool_call_id": tool_call.get("id"),
            "extra_keys": sorted(extra.keys()),
        }

    def _request_id_inventory_locked(self) -> dict[str, Any]:
        """Return current in-memory approval request ids for diagnostics."""
        return {
            "pending_request_ids": list(self._pending.keys()),
            "completed_request_ids": list(self._completed.keys()),
            "pending_count": len(self._pending),
            "completed_count": len(self._completed),
        }

    @staticmethod
    def _external_submission_summary(
        pending: PendingApproval,
    ) -> dict[str, Any] | None:
        """Return log/API-safe metadata for an external decision submit."""
        extra = pending.extra if isinstance(pending.extra, dict) else {}
        submission = extra.get(_EXTERNAL_SUBMISSION_EXTRA_KEY)
        if not isinstance(submission, dict):
            return None
        return {
            "status": submission.get("status") or "submitted",
            "decision": submission.get("decision"),
            "source_channel": submission.get("source_channel"),
            "source_user_id": submission.get("source_user_id"),
            "source_message_id": submission.get("source_message_id"),
            "submitted_at": submission.get("submitted_at"),
        }

    async def debug_request_lookup(self, request_id: str) -> dict[str, Any]:
        """Return diagnostic data for an approval request lookup."""
        async with self._lock:
            pending = self._pending.get(request_id)
            completed = self._completed.get(request_id)
            recent_pending = sorted(
                self._pending.values(),
                key=lambda record: record.created_at,
                reverse=True,
            )[:5]
            return {
                "request_id": request_id,
                "current_scope_id": self._get_current_scope_id(),
                "pending_present": pending is not None,
                "completed_present": completed is not None,
                "pending_count": len(self._pending),
                "completed_count": len(self._completed),
                "audit_store_attached": self._store is not None,
                "pending": self._debug_record_summary(pending),
                "completed": self._debug_record_summary(completed),
                "recent_pending": [
                    self._debug_record_summary(record)
                    for record in recent_pending
                ],
            }

    # ------------------------------------------------------------------
    # Core approval lifecycle
    # ------------------------------------------------------------------

    async def create_pending(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        tool_name: str,
        result: "ToolGuardResult",
        extra: dict[str, Any] | None = None,
    ) -> PendingApproval:
        """Create a pending approval record and return it."""
        from ...security.tool_guard.approval import format_findings_summary

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        scope_id = self._get_current_scope_id() or "default"

        pending = PendingApproval(
            request_id=request_id,
            session_id=session_id,
            scope_id=scope_id,
            user_id=user_id,
            channel=channel,
            tool_name=tool_name,
            created_at=time.time(),
            future=loop.create_future(),
            result_summary=format_findings_summary(result),
            findings_count=result.findings_count,
            extra=dict(extra or {}),
        )

        async with self._lock:
            self._pending[request_id] = pending
            self._gc_pending_locked()
            self._gc_completed_locked()

        await self._persist_request(pending)
        await self.record_event(pending, "created", status=pending.status)
        return pending

    async def resolve_request(
        self,
        request_id: str,
        decision: ApprovalDecision,
    ) -> PendingApproval | None:
        """Resolve one pending approval request."""
        async with self._lock:
            scope_id = self._get_current_scope_id()
            pending = self._pending.get(request_id)
            inventory = self._request_id_inventory_locked()
            completed_present = request_id in self._completed
            logger.info(
                "Approval resolve state: request_id=%s decision=%s "
                "current_scope_id=%s pending_request_ids=%s "
                "completed_request_ids=%s pending_count=%d "
                "completed_count=%d pending_present=%s "
                "completed_present=%s",
                request_id,
                decision.value,
                scope_id,
                inventory["pending_request_ids"],
                inventory["completed_request_ids"],
                inventory["pending_count"],
                inventory["completed_count"],
                pending is not None,
                completed_present,
                extra={
                    **inventory,
                    "approval_request_id": request_id,
                    "approval_decision": decision.value,
                    "approval_current_scope_id": scope_id,
                    "approval_pending_present": pending is not None,
                    "approval_completed_present": completed_present,
                },
            )
            if pending is None or pending.scope_id != scope_id:
                completed = self._completed.get(request_id)
                if completed is None or completed.scope_id != scope_id:
                    logger.warning(
                        "Approval resolve miss: request_id=%s "
                        "decision=%s current_scope_id=%s "
                        "pending_present=%s completed_present=%s "
                        "pending_count=%d completed_count=%d pending=%s "
                        "completed=%s",
                        request_id,
                        decision.value,
                        scope_id,
                        pending is not None,
                        completed is not None,
                        len(self._pending),
                        len(self._completed),
                        self._debug_record_summary(pending),
                        self._debug_record_summary(completed),
                    )
                    return None
                return completed

            pending = self._pending.pop(request_id)
            if pending is None:
                return self._completed.get(request_id)

            pending.status = decision.value
            pending.resolved_at = time.time()
            self._completed[request_id] = pending
            self._gc_completed_locked()

        if not pending.future.done():
            pending.future.set_result(decision)

        goal_id = str(pending.extra.get("goal_id") or "").strip()
        if goal_id:
            try:
                from ..goals.registry import get_goal_service

                goal_service = get_goal_service()
                if goal_service is not None:
                    await goal_service.wake(
                        goal_id,
                        f"Tool approval {decision.value}",
                    )
            except Exception:
                logger.warning(
                    "Failed to wake Goal after approval resolution: %s",
                    goal_id,
                    exc_info=True,
                )

        await self._persist_request(pending)
        await self.record_event(pending, "resolved", status=pending.status)
        return pending

    async def get_request(self, request_id: str) -> PendingApproval | None:
        """按当前 scope 获取 pending 或已完成的审批请求。"""
        async with self._lock:
            scope_id = self._get_current_scope_id()
            pending = self._pending.get(request_id)
            completed = self._completed.get(request_id)
            logger.debug(
                "Approval lookup: request_id=%s current_scope_id=%s "
                "pending_present=%s completed_present=%s",
                request_id,
                scope_id,
                pending is not None,
                completed is not None,
            )
            record = pending or completed
            if record is None:
                logger.warning(
                    "Approval lookup miss: request_id=%s "
                    "current_scope_id=%s pending_count=%d "
                    "completed_count=%d",
                    request_id,
                    scope_id,
                    len(self._pending),
                    len(self._completed),
                )
                return None
            if record.scope_id != scope_id:
                logger.warning(
                    "Approval lookup scope mismatch: request_id=%s "
                    "current_scope_id=%s record=%s",
                    request_id,
                    scope_id,
                    self._debug_record_summary(record),
                )
                return None
            return record

    async def get_requests(
        self,
        request_ids: list[str],
    ) -> dict[str, PendingApproval]:
        """Batch-read approval records visible in the active scope."""
        if not request_ids:
            return {}
        async with self._lock:
            scope_id = self._get_current_scope_id()
            records: dict[str, PendingApproval] = {}
            for request_id in dict.fromkeys(request_ids):
                record = self._pending.get(request_id) or self._completed.get(
                    request_id,
                )
                if record is not None and record.scope_id == scope_id:
                    records[request_id] = record
            return records

    async def get_request_status(
        self,
        request_id: str,
    ) -> dict[str, Any] | None:
        """Return the current status visible in the active runtime scope."""
        async with self._lock:
            scope_id = self._get_current_scope_id()
            pending = self._pending.get(request_id)
            completed = self._completed.get(request_id)
            record = pending or completed
            if record is None:
                logger.info(
                    "Approval status lookup miss: request_id=%s "
                    "current_scope_id=%s pending_count=%d "
                    "completed_count=%d",
                    request_id,
                    scope_id,
                    len(self._pending),
                    len(self._completed),
                )
                return None
            if record.scope_id != scope_id:
                logger.warning(
                    "Approval status scope mismatch: request_id=%s "
                    "current_scope_id=%s record=%s",
                    request_id,
                    scope_id,
                    self._debug_record_summary(record),
                )
                return None

            external_submission = self._external_submission_summary(record)
            status = record.status
            if status == "pending" and external_submission is not None:
                status = external_submission.get("status") or "submitted"

            response: dict[str, Any] = {
                "request_id": record.request_id,
                "status": status,
                "session_id": record.session_id,
            }
            if external_submission is not None:
                response.update(external_submission)
            return response

    async def get_pending_by_session(
        self,
        session_id: str,
    ) -> PendingApproval | None:
        """Return the next pending approval for *session_id* (FIFO).

        Pending approvals are consumed in creation order, so repeated
        ``/approve`` inputs walk the queue from oldest to newest.
        """
        async with self._lock:
            for pending in self._pending.values():
                if (
                    pending.session_id == session_id
                    and pending.status == "pending"
                    and self._matches_scope(pending)
                ):
                    return pending
        return None

    async def get_all_pending_by_session(
        self,
        session_id: str,
    ) -> list[PendingApproval]:
        """Return all pending approvals for *session_id* (FIFO order)."""
        async with self._lock:
            return [
                p
                for p in self._pending.values()
                if (
                    p.session_id == session_id
                    and p.status == "pending"
                    and self._matches_scope(p)
                )
            ]

    async def cancel_stale_pending_for_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
    ) -> int:
        """Cancel pending approvals whose stored tool_call id matches.

        When a tool call is replayed (e.g. after ``/approve`` triggers
        sibling replay), the guard may create a *new* pending for the
        same logical tool call.  This method cancels the old pending
        first so orphaned records don't accumulate.

        Returns the number of records cancelled.
        """
        now = time.time()
        cancelled = 0
        cancelled_records: list[PendingApproval] = []
        async with self._lock:
            to_cancel = [
                k
                for k, p in self._pending.items()
                if p.session_id == session_id
                and p.status == "pending"
                and self._matches_scope(p)
                and isinstance(p.extra.get("tool_call"), dict)
                and p.extra["tool_call"].get("id") == tool_call_id
            ]
            for k in to_cancel:
                pending = self._pending.pop(k)
                if not pending.future.done():
                    pending.future.set_result(ApprovalDecision.TIMEOUT)
                pending.status = "superseded"
                pending.resolved_at = now
                self._completed[k] = pending
                cancelled += 1
                cancelled_records.append(pending)
        if cancelled:
            logger.info(
                "Tool guard: cancelled %d stale pending approval(s) "
                "for tool_call %s (session %s)",
                cancelled,
                tool_call_id,
                session_id[:8],
            )
        for record in cancelled_records:
            await self._persist_request(record)
            await self.record_event(
                record,
                "superseded",
                status=record.status,
                details={"tool_call_id": tool_call_id},
            )
        return cancelled

    async def supersede_goal_review_approvals(
        self,
        goal_id: str,
        request_ids: tuple[str, ...],
    ) -> int:
        """Invalidate pending Judge approvals belonging to an edited Goal."""
        requested = set(request_ids)
        if not goal_id or not requested:
            return 0
        now = time.time()
        superseded: list[PendingApproval] = []
        async with self._lock:
            for request_id in requested:
                pending = self._pending.get(request_id)
                if (
                    pending is None
                    or not self._matches_scope(pending)
                    or pending.extra.get("goal_id") != goal_id
                ):
                    continue
                self._pending.pop(request_id)
                pending.status = "superseded"
                pending.resolved_at = now
                if not pending.future.done():
                    pending.future.set_result(ApprovalDecision.TIMEOUT)
                self._completed[request_id] = pending
                superseded.append(pending)
            self._gc_completed_locked()
        for pending in superseded:
            await self._persist_request(pending)
            await self.record_event(
                pending,
                "superseded",
                status=pending.status,
                details={"goal_id": goal_id},
            )
        return len(superseded)

    async def supersede_pending_for_turn(
        self,
        chat_id: str,
        msgid: str,
    ) -> int:
        """Invalidate pending approvals belonging to one stopped chat turn."""
        if not chat_id or not msgid:
            return 0
        now = time.time()
        superseded: list[PendingApproval] = []
        async with self._lock:
            for request_id, pending in list(self._pending.items()):
                if (
                    pending.status != "pending"
                    or not self._matches_scope(pending)
                    or pending.extra.get("chat_id") != chat_id
                    or pending.extra.get("msgid") != msgid
                ):
                    continue
                self._pending.pop(request_id)
                pending.status = "superseded"
                pending.resolved_at = now
                if not pending.future.done():
                    pending.future.set_result(ApprovalDecision.TIMEOUT)
                self._completed[request_id] = pending
                superseded.append(pending)
            self._gc_completed_locked()
        for pending in superseded:
            await self._persist_request(pending)
            await self.record_event(
                pending,
                "superseded",
                status=pending.status,
                details={"chat_id": chat_id, "msgid": msgid},
            )
        return len(superseded)

    async def consume_approval(
        self,
        session_id: str,
        tool_name: str,
        tool_params: dict[str, Any] | None = None,
        approval_kind: str = "tool_guard",
    ) -> bool:
        """Check and consume a one-shot tool approval.

        If *tool_name* was recently approved via ``/daemon approve``
        for *session_id*, remove the completed record and return
        ``True`` so the caller can skip the guard check.

        When *tool_params* is given, the approved call's stored
        parameters are compared.  A mismatch causes the approval
        to be rejected (returns ``False``), preventing an approved
        ``rm foo.txt`` from being used to execute ``rm -rf /``.
        """
        consumed_record: PendingApproval | None = None
        async with self._lock:
            for key, completed in list(self._completed.items()):
                if self._is_completed_approval_match(
                    completed,
                    session_id=session_id,
                    tool_name=tool_name,
                    approval_kind=approval_kind,
                ):
                    if tool_params is not None:
                        approved_call = completed.extra.get(
                            "tool_call",
                            {},
                        )
                        approved_params = approved_call.get(
                            "input",
                            {},
                        )
                        if approved_params != tool_params:
                            logger.warning(
                                "Tool guard: params mismatch for "
                                "'%s' (session %s), rejecting "
                                "stale approval",
                                tool_name,
                                session_id[:8],
                            )
                            del self._completed[key]
                            return False
                    completed.consumed = True
                    self._completed[key] = completed
                    consumed_record = completed
                    break
        if consumed_record is None:
            return False
        consumed_at = time.time()
        await self._persist_request(consumed_record, consumed_at=consumed_at)
        await self.record_event(
            consumed_record,
            "consumed",
            status=consumed_record.status,
        )
        return True

    async def record_external_submission(
        self,
        pending: PendingApproval,
        *,
        decision: str,
        source_channel: str,
        source_user_id: str | None = None,
        source_message_id: str | None = None,
    ) -> None:
        """记录外部渠道提交审批决策。"""
        submission = {
            "status": "submitted",
            "decision": decision,
            "source_channel": source_channel,
            "source_user_id": source_user_id,
            "source_message_id": source_message_id,
            "submitted_at": time.time(),
        }
        async with self._lock:
            record = (
                self._pending.get(pending.request_id)
                or self._completed.get(pending.request_id)
                or pending
            )
            extra = (
                dict(record.extra) if isinstance(record.extra, dict) else {}
            )
            extra[_EXTERNAL_SUBMISSION_EXTRA_KEY] = submission
            record.extra = extra
            pending = record

        await self._persist_request(
            pending,
            source_channel=source_channel,
            source_user_id=source_user_id,
            source_message_id=source_message_id,
        )
        await self.record_event(
            pending,
            "decision_submitted",
            status=pending.status,
            actor_channel=source_channel,
            actor_user_id=source_user_id,
            source_message_id=source_message_id,
            details={"decision": decision},
        )

    async def record_event(
        self,
        pending: PendingApproval,
        event_type: str,
        *,
        status: str | None = None,
        actor_channel: str | None = None,
        actor_user_id: str | None = None,
        source_message_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录审批过程事件，审计失败不影响主链路。"""
        store = self._store
        if store is None:
            return
        try:
            await store.add_event(
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
                "Approval audit event failed: %s",
                event_type,
                exc_info=True,
            )

    async def _persist_request(
        self,
        pending: PendingApproval,
        **kwargs: Any,
    ) -> None:
        store = self._store
        if store is None:
            return
        try:
            await store.upsert_request(pending, **kwargs)
        except Exception:
            logger.warning(
                "Approval audit request upsert failed: request_id=%s",
                pending.request_id,
                exc_info=True,
            )

    def _is_completed_approval_match(
        self,
        completed: PendingApproval,
        *,
        session_id: str,
        tool_name: str,
        approval_kind: str,
    ) -> bool:
        """判断已完成审批是否匹配当前运行范围与工具调用。"""
        if completed.session_id != session_id:
            return False
        if completed.tool_name != tool_name:
            return False
        if completed.status != "approved" or completed.consumed:
            return False
        if not self._matches_scope(completed):
            return False
        return (
            completed.extra.get("approval_kind", "tool_guard") == approval_kind
        )

    # ------------------------------------------------------------------
    # Garbage collection
    # ------------------------------------------------------------------

    def _gc_pending_locked(self) -> None:
        """Evict stale pending records whose futures were never resolved.

        Caller must hold ``_lock``.
        """
        now = time.time()
        expired = [
            k
            for k, v in self._pending.items()
            if now - v.created_at > _GC_PENDING_MAX_AGE_SECONDS
        ]
        for k in expired:
            pending = self._pending.pop(k)
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.TIMEOUT)
            pending.status = "timeout"
            pending.resolved_at = now
            self._completed[k] = pending
        if expired:
            inventory = self._request_id_inventory_locked()
            logger.info(
                "Approval pending GC expired records: gc_request_ids=%s "
                "pending_request_ids=%s completed_request_ids=%s "
                "pending_count=%d completed_count=%d",
                expired,
                inventory["pending_request_ids"],
                inventory["completed_request_ids"],
                inventory["pending_count"],
                inventory["completed_count"],
                extra={
                    "approval_gc_request_ids": expired,
                    **inventory,
                },
            )

        overflow = len(self._pending) - _GC_MAX_PENDING
        if overflow <= 0:
            return
        ordered = sorted(
            self._pending.items(),
            key=lambda item: item[1].created_at,
        )
        overflow_ids: list[str] = []
        for key, pending in ordered[:overflow]:
            del self._pending[key]
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.TIMEOUT)
            pending.status = "timeout"
            pending.resolved_at = now
            self._completed[key] = pending
            overflow_ids.append(key)
        if overflow_ids:
            inventory = self._request_id_inventory_locked()
            logger.warning(
                "Approval pending GC overflow records: gc_request_ids=%s "
                "pending_request_ids=%s completed_request_ids=%s "
                "pending_count=%d completed_count=%d",
                overflow_ids,
                inventory["pending_request_ids"],
                inventory["completed_request_ids"],
                inventory["pending_count"],
                inventory["completed_count"],
                extra={
                    "approval_gc_request_ids": overflow_ids,
                    **inventory,
                },
            )

    def _gc_completed_locked(self) -> None:
        """Remove stale/overflow completed records.

        Caller must hold ``_lock``.
        """
        now = time.time()
        expired = [
            k
            for k, v in self._completed.items()
            if v.resolved_at and now - v.resolved_at > _GC_MAX_AGE_SECONDS
        ]
        for k in expired:
            del self._completed[k]

        # Still over cap: evict oldest completed records first.
        overflow = len(self._completed) - _GC_MAX_COMPLETED
        if overflow <= 0:
            return
        ordered = sorted(
            self._completed.items(),
            key=lambda item: item[1].resolved_at or item[1].created_at,
        )
        for key, _pending in ordered[:overflow]:
            del self._completed[key]


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_approval_service: ApprovalService | None = None


def get_approval_service() -> ApprovalService:
    """Return the process-wide approval service singleton."""
    global _approval_service
    if _approval_service is None:
        _approval_service = ApprovalService()
    return _approval_service
