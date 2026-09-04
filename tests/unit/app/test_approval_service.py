# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import logging
import pytest

from swe.app.approvals.service import ApprovalService
from swe.app.runner.runner import AgentRunner
from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)
from swe.app.source_system_config.runtime import bind_source_system_config
from swe.config.context import tenant_context
from swe.security.tool_guard.approval import ApprovalDecision


def _result():
    return SimpleNamespace(findings=[], findings_count=0)


class _GoalWakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def wake(self, goal_id: str, reason: str) -> None:
        self.calls.append((goal_id, reason))


class _ZhaohuChannel:
    def __init__(self) -> None:
        self.result_calls: list[dict[str, Any]] = []

    async def send_cron_approval_result(self, **kwargs):
        self.result_calls.append(kwargs)
        return (0, "noop")


class _ChannelManager:
    def __init__(self) -> None:
        self.zhaohu = _ZhaohuChannel()

    async def get_channel(self, name: str):
        if name == "zhaohu":
            return self.zhaohu
        return None


@pytest.mark.asyncio
async def test_resolved_goal_approval_wakes_its_goal(monkeypatch) -> None:
    service = ApprovalService()
    goal_service = _GoalWakeService()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: goal_service,
    )
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
            extra={"goal_id": "goal-1"},
        )
        await service.resolve_request(
            pending.request_id,
            ApprovalDecision.APPROVED,
        )

    assert goal_service.calls == [("goal-1", "Tool approval approved")]


@pytest.mark.asyncio
async def test_get_requests_batches_scope_filtered_records() -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        visible = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="read_file",
            result=_result(),
        )
    with tenant_context(tenant_id="tenant-b", source_id="source-b"):
        hidden = await service.create_pending(
            session_id="session-2",
            user_id="user-2",
            channel="console",
            tool_name="read_file",
            result=_result(),
        )
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        records = await service.get_requests(
            [
                visible.request_id,
                visible.request_id,
                hidden.request_id,
                "missing",
            ],
        )

    assert records == {visible.request_id: visible}


@pytest.mark.asyncio
async def test_supersede_goal_review_approvals_cancels_only_its_pending_ids() -> (
    None
):
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        target = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="read_file",
            result=_result(),
            extra={"goal_id": "goal-1"},
        )
        other = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="read_file",
            result=_result(),
            extra={"goal_id": "goal-2"},
        )

        count = await service.supersede_goal_review_approvals(
            "goal-1",
            (target.request_id, other.request_id),
        )
        target_status = await service.get_request_status(target.request_id)
        other_status = await service.get_request_status(other.request_id)

    assert count == 1
    assert (
        target_status is not None and target_status["status"] == "superseded"
    )
    assert other_status is not None and other_status["status"] == "pending"


def _runner_with_zhaohu_workspace() -> tuple[AgentRunner, _ZhaohuChannel]:
    runner = AgentRunner()
    channel_manager = _ChannelManager()
    runner.set_workspace(SimpleNamespace(channel_manager=channel_manager))
    return runner, channel_manager.zhaohu


def _source_config_with_zhaohu_tool_guard_notifications():
    raw_config = SourceSystemConfig.model_validate(
        {
            "approval_notifications": {
                "zhaohu_tool_guard_enabled": True,
            },
        },
    )
    return EffectiveSourceSystemConfig(
        source_id="source-a",
        config=raw_config.merged_with_defaults(),
        raw_config=raw_config,
        version=1,
    )


@pytest.mark.asyncio
async def test_approval_lookup_logs_in_memory_request_ids(caplog) -> None:
    service = ApprovalService()
    approval_logger = logging.getLogger("swe.app.approvals.service")
    approval_logger.addHandler(caplog.handler)
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )
        completed = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )
        await service.resolve_request(
            completed.request_id,
            ApprovalDecision.APPROVED,
        )

        caplog.clear()
        try:
            with caplog.at_level(
                logging.INFO,
                logger="swe.app.approvals.service",
            ):
                found = await service.get_request(pending.request_id)
                diagnostic = await service.debug_request_lookup(
                    pending.request_id,
                )
        finally:
            approval_logger.removeHandler(caplog.handler)

    assert found is pending
    assert diagnostic["pending"]["request_id"] == pending.request_id
    assert diagnostic["pending_count"] == 1
    assert diagnostic["completed_count"] == 1


@pytest.mark.asyncio
async def test_hook_approval_is_not_consumed_as_tool_guard_preapproval() -> (
    None
):
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
            extra={
                "approval_kind": "hook_pre_tool_use",
                "tool_call": {
                    "id": "tool-1",
                    "name": "execute_shell_command",
                    "input": {"cmd": "echo original"},
                },
            },
        )
        await service.resolve_request(
            pending.request_id,
            ApprovalDecision.APPROVED,
        )

        consumed = await service.consume_approval(
            "session-1",
            "execute_shell_command",
            tool_params={"cmd": "echo original"},
        )

    assert consumed is False


@pytest.mark.asyncio
async def test_tool_guard_approval_is_consumed_as_preapproval() -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
            extra={
                "approval_kind": "tool_guard",
                "tool_call": {
                    "id": "tool-1",
                    "name": "execute_shell_command",
                    "input": {"cmd": "echo original"},
                },
            },
        )
        await service.resolve_request(
            pending.request_id,
            ApprovalDecision.APPROVED,
        )

        consumed = await service.consume_approval(
            "session-1",
            "execute_shell_command",
            tool_params={"cmd": "echo original"},
        )

    assert consumed is True


@pytest.mark.asyncio
async def test_external_submission_status_marks_pending_as_submitted() -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )

        await service.record_external_submission(
            pending,
            decision="approve",
            source_channel="zhaohu",
            source_user_id="approver-1",
            source_message_id="message-1",
        )
        status = await service.get_request_status(pending.request_id)
        record = await service.get_request(pending.request_id)

    assert status is not None
    assert status["status"] == "submitted"
    assert status["decision"] == "approve"
    assert status["source_channel"] == "zhaohu"
    assert status["source_user_id"] == "approver-1"
    assert status["source_message_id"] == "message-1"
    assert record is not None
    assert record.status == "pending"


@pytest.mark.asyncio
async def test_pending_approval_lookup_is_scope_aware_for_same_session() -> (
    None
):
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending_a = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )
    with tenant_context(tenant_id="tenant-a", source_id="source-b"):
        pending_b = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        selected_a = await service.get_pending_by_session("session-1")
    with tenant_context(tenant_id="tenant-a", source_id="source-b"):
        selected_b = await service.get_pending_by_session("session-1")

    assert selected_a is not None
    assert selected_b is not None
    assert selected_a.request_id == pending_a.request_id
    assert selected_b.request_id == pending_b.request_id


@pytest.mark.asyncio
async def test_unscoped_lookup_cannot_observe_scoped_pending() -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )

    assert await service.get_pending_by_session("session-1") is None


@pytest.mark.asyncio
async def test_unscoped_resolution_cannot_mutate_scoped_pending() -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )

    resolved = await service.resolve_request(
        pending.request_id,
        ApprovalDecision.APPROVED,
    )

    assert resolved is None
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        assert (await service.get_request(pending.request_id)).status == (
            "pending"
        )


@pytest.mark.asyncio
async def test_pending_approval_uses_canonical_scope_key_for_legacy_input() -> (
    None
):
    service = ApprovalService()
    with tenant_context(
        tenant_id="tenant-a",
        source_id="source-a",
        scope_id="scope.v1.dGVuYW50LWE.c291cmNlLWE",
    ):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )

    assert pending.scope_id == "dGVuYW50LWE.c291cmNlLWE"


@pytest.mark.asyncio
async def test_pending_approval_gc_keeps_records_inside_timeout_window() -> (
    None
):
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )
        pending.created_at = time.time() - 3600

        await service.create_pending(
            session_id="session-2",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )

        assert (await service.get_request(pending.request_id)).status == (
            "pending"
        )


@pytest.mark.asyncio
async def test_runner_approves_requested_pending_id_not_fifo_head(
    monkeypatch,
) -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        first = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
            extra={
                "approval_kind": "tool_guard",
                "tool_call": {
                    "id": "tool-1",
                    "name": "execute_shell_command",
                    "input": {"cmd": "echo first"},
                },
            },
        )
        second = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
            extra={
                "approval_kind": "hook_pre_tool_use",
                "hook_ask_handler_ids": ["hook-a"],
                "tool_call": {
                    "id": "tool-2",
                    "name": "execute_shell_command",
                    "input": {"cmd": "echo second"},
                },
            },
        )
    monkeypatch.setattr(
        "swe.app.approvals.service._approval_service",
        service,
    )
    runner = AgentRunner()

    with (
        tenant_context(
            tenant_id="tenant-a",
            source_id="source-a",
        ),
        bind_source_system_config(
            _source_config_with_zhaohu_tool_guard_notifications(),
        ),
    ):
        response, consumed, approved_tool_call = (
            await runner._resolve_pending_approval(
                "session-1",
                f"/approve {second.request_id}",
            )
        )

        assert response is None
        assert consumed is True
        assert approved_tool_call is not None
        assert approved_tool_call["id"] == "tool-2"
        assert approved_tool_call["_approval_replay"]["request_id"] == (
            second.request_id
        )
        assert (await service.get_request(first.request_id)).status == (
            "pending"
        )
        assert (await service.get_request(second.request_id)).status == (
            "approved"
        )


@pytest.mark.asyncio
async def test_runner_rejects_requested_pending_id_from_other_source(
    monkeypatch,
) -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )
    monkeypatch.setattr(
        "swe.app.approvals.service._approval_service",
        service,
    )
    runner = AgentRunner()

    with tenant_context(tenant_id="tenant-a", source_id="source-b"):
        response, consumed, approved_tool_call = (
            await runner._resolve_pending_approval(
                "session-1",
                f"/approve {pending.request_id}",
            )
        )

    assert response is None
    assert consumed is False
    assert approved_tool_call is None
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        assert (await service.get_request(pending.request_id)).status == (
            "pending"
        )


@pytest.mark.asyncio
async def test_runner_console_approve_notifies_zhaohu_result(
    monkeypatch,
) -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
            extra={
                "approval_kind": "tool_guard",
                "tool_call": {
                    "id": "tool-1",
                    "name": "execute_shell_command",
                    "input": {"cmd": "echo hi"},
                },
            },
        )
    monkeypatch.setattr(
        "swe.app.approvals.service._approval_service",
        service,
    )
    runner, zhaohu = _runner_with_zhaohu_workspace()

    with (
        tenant_context(
            tenant_id="tenant-a",
            source_id="source-a",
        ),
        bind_source_system_config(
            _source_config_with_zhaohu_tool_guard_notifications(),
        ),
    ):
        response, consumed, approved_tool_call = (
            await runner._resolve_pending_approval(
                "session-1",
                f"/approve {pending.request_id}",
            )
        )

    assert response is None
    assert consumed is True
    assert approved_tool_call is not None
    assert approved_tool_call["id"] == "tool-1"
    assert zhaohu.result_calls == [
        {
            "request_id": pending.request_id,
            "session_id": "session-1",
            "user_id": "user-1",
            "tool_name": "execute_shell_command",
            "decision": "approved",
            "source_channel": "console",
        },
    ]


@pytest.mark.asyncio
async def test_runner_console_deny_notifies_zhaohu_result(
    monkeypatch,
) -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )
    monkeypatch.setattr(
        "swe.app.approvals.service._approval_service",
        service,
    )
    runner, zhaohu = _runner_with_zhaohu_workspace()

    with (
        tenant_context(
            tenant_id="tenant-a",
            source_id="source-a",
        ),
        bind_source_system_config(
            _source_config_with_zhaohu_tool_guard_notifications(),
        ),
    ):
        response, consumed, approved_tool_call = (
            await runner._resolve_pending_approval(
                "session-1",
                f"/deny {pending.request_id}",
            )
        )

    assert response is not None
    assert consumed is True
    assert approved_tool_call is None
    assert zhaohu.result_calls == [
        {
            "request_id": pending.request_id,
            "session_id": "session-1",
            "user_id": "user-1",
            "tool_name": "execute_shell_command",
            "decision": "denied",
            "source_channel": "console",
        },
    ]


@pytest.mark.asyncio
async def test_runner_console_approval_does_not_override_external_submission(
    monkeypatch,
) -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
            extra={
                "approval_kind": "tool_guard",
                "tool_call": {
                    "id": "tool-1",
                    "name": "execute_shell_command",
                    "input": {"cmd": "echo hi"},
                },
            },
        )
        await service.record_external_submission(
            pending,
            decision="approve",
            source_channel="zhaohu",
            source_user_id="approver-1",
        )
    monkeypatch.setattr(
        "swe.app.approvals.service._approval_service",
        service,
    )
    runner = AgentRunner()

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        response, consumed, approved_tool_call = (
            await runner._resolve_pending_approval(
                "session-1",
                f"/approve {pending.request_id}",
            )
        )
        record = await service.get_request(pending.request_id)

    assert response is not None
    assert consumed is True
    assert approved_tool_call is None
    assert record is not None
    assert record.status == "pending"


@pytest.mark.asyncio
async def test_runner_external_approval_does_not_duplicate_zhaohu_result(
    monkeypatch,
) -> None:
    service = ApprovalService()
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        pending = await service.create_pending(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            tool_name="execute_shell_command",
            result=_result(),
        )
        await service.record_external_submission(
            pending,
            decision="approve",
            source_channel="zhaohu",
        )
    monkeypatch.setattr(
        "swe.app.approvals.service._approval_service",
        service,
    )
    runner, zhaohu = _runner_with_zhaohu_workspace()
    request = SimpleNamespace(
        channel_meta={
            "approval_source_channel": "zhaohu",
        },
    )

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        response, consumed, approved_tool_call = (
            await runner._resolve_pending_approval(
                "session-1",
                f"/approve {pending.request_id}",
                request=request,
            )
        )

    assert response is None
    assert consumed is True
    assert approved_tool_call is None
    assert zhaohu.result_calls == []
