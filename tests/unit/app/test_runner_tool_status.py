# -*- coding: utf-8 -*-
from __future__ import annotations

from importlib import import_module

import pytest
from agentscope.message import Msg

from swe.app.runner.utils import agentscope_msg_to_message


def _tool_status_module():
    try:
        return import_module("swe.app.runner.tool_status")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing tool status module: {exc}")
        raise AssertionError("unreachable")


def test_apply_running_tool_status_marks_call_without_error() -> None:
    module = _tool_status_module()
    data = {"name": "grep_search", "arguments": '{"pattern":"tenant"}'}

    result = module.apply_running_tool_status(data)

    assert result is data
    assert result["tool_status"] == module.TOOL_STATUS_RUNNING
    assert module.TOOL_ERROR_FIELD not in result


def test_apply_terminal_tool_status_marks_success_with_null_error() -> None:
    module = _tool_status_module()
    data = {"name": "grep_search", "output": ["a.py:1"]}

    result = module.apply_terminal_tool_status(data)

    assert result["tool_status"] == module.TOOL_STATUS_SUCCESS
    assert result["tool_error"] is None


def test_apply_terminal_tool_status_marks_failed_error_output() -> None:
    module = _tool_status_module()
    data = {"name": "grep_search", "output": {"error": "permission denied"}}

    result = module.apply_terminal_tool_status(data)

    assert result["tool_status"] == module.TOOL_STATUS_FAILED
    assert result["tool_error"] == "permission denied"


def test_apply_terminal_tool_status_uses_default_failed_error_text() -> None:
    module = _tool_status_module()
    data = {"name": "grep_search", "output": {"isError": True}}

    result = module.apply_terminal_tool_status(data)

    assert result["tool_status"] == module.TOOL_STATUS_FAILED
    assert result["tool_error"]


def test_apply_terminal_tool_status_preserves_preexisting_failed_status() -> (
    None
):
    module = _tool_status_module()
    data = {
        "name": "grep_search",
        "output": "tool output was rewritten downstream",
        "tool_status": module.TOOL_STATUS_FAILED,
        "tool_error": "permission denied",
    }

    result = module.apply_terminal_tool_status(data)

    assert result["tool_status"] == module.TOOL_STATUS_FAILED
    assert result["tool_error"] == "permission denied"


def test_apply_terminal_tool_status_truncates_long_error_text() -> None:
    module = _tool_status_module()
    long_error = "x" * (module.TOOL_ERROR_SUMMARY_LIMIT + 20)
    data = {"name": "grep_search", "output": {"error": long_error}}

    result = module.apply_terminal_tool_status(data)

    assert len(result["tool_error"]) == module.TOOL_ERROR_SUMMARY_LIMIT
    assert (
        result["tool_error"] == long_error[: module.TOOL_ERROR_SUMMARY_LIMIT]
    )


def test_history_tool_use_rebuilds_running_status() -> None:
    messages = agentscope_msg_to_message(
        Msg(
            name="Friday",
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "grep_search",
                    "input": {"pattern": "tenant"},
                },
            ],
            timestamp="2026-06-01T08:00:00Z",
        ),
    )

    data = messages[0].content[0].data
    assert data["tool_status"] == "running"
    assert "tool_error" not in data


def test_history_tool_result_rebuilds_success_status() -> None:
    messages = agentscope_msg_to_message(
        Msg(
            name="Friday",
            role="assistant",
            content=[
                {
                    "type": "tool_result",
                    "id": "tool-1",
                    "name": "grep_search",
                    "output": ["a.py:1", "b.py:2"],
                },
            ],
            timestamp="2026-06-01T08:00:00Z",
        ),
    )

    data = messages[0].content[0].data
    assert data["tool_status"] == "success"
    assert data["tool_error"] is None


def test_history_tool_result_rebuilds_failed_status() -> None:
    messages = agentscope_msg_to_message(
        Msg(
            name="Friday",
            role="assistant",
            content=[
                {
                    "type": "tool_result",
                    "id": "tool-1",
                    "name": "grep_search",
                    "output": {"error": "permission denied"},
                },
            ],
            timestamp="2026-06-01T08:00:00Z",
        ),
    )

    data = messages[0].content[0].data
    assert data["tool_status"] == "failed"
    assert data["tool_error"] == "permission denied"


def test_history_tool_result_rebuilds_structured_failed_status() -> None:
    messages = agentscope_msg_to_message(
        Msg(
            name="Friday",
            role="assistant",
            content=[
                {
                    "type": "tool_result",
                    "id": "tool-1",
                    "name": "grep_search",
                    "output": {
                        "isError": True,
                        "error_type": "permission_denied",
                        "content": [
                            {
                                "type": "text",
                                "text": "permission denied",
                            },
                        ],
                    },
                },
            ],
            timestamp="2026-06-01T08:00:00Z",
        ),
    )

    data = messages[0].content[0].data
    assert data["tool_status"] == "failed"
    assert data["tool_error"] == "permission denied"


def test_apply_governance_marks_pending_approval() -> None:
    module = _tool_status_module()
    data = {
        "name": "execute_shell_command",
        "output": {
            "isError": True,
            "error_type": "approval_required",
            "content": [{"type": "text", "text": "risk detected"}],
        },
    }

    module.apply_terminal_tool_status(data)
    result = module.apply_governance_tool_status(data, "pending")

    assert result == module.TOOL_STATUS_PENDING
    assert data["tool_governance"] == module.TOOL_STATUS_PENDING
    assert "tool_status" not in data
    assert "tool_error" not in data


def test_apply_governance_marks_rejected_decision() -> None:
    module = _tool_status_module()
    result = module.apply_governance_tool_status(
        {"name": "execute_shell_command"},
        "rejected",
    )

    assert result == module.TOOL_STATUS_REJECTED


def test_apply_governance_marks_blocked_policy() -> None:
    module = _tool_status_module()
    for governance in ("blocked",):
        result = module.apply_governance_tool_status(
            {"name": "execute_shell_command"},
            governance,
        )
        assert result == module.TOOL_STATUS_BLOCKED


def test_apply_governance_ignores_untrusted_output_error_type() -> None:
    module = _tool_status_module()
    data = {"name": "execute_shell_command"}

    result = module.apply_governance_tool_status(
        data,
        '{"error_type": "approval_rejected"}',
    )

    assert result is None
    assert module.GOVERNANCE_FIELD not in data


def test_apply_governance_ignores_real_execution_failures() -> None:
    module = _tool_status_module()
    data = {"name": "grep_search", "tool_status": module.TOOL_STATUS_FAILED}

    result = module.apply_governance_tool_status(
        data,
        None,
    )

    assert result is None
    assert data["tool_status"] == module.TOOL_STATUS_FAILED
    assert module.GOVERNANCE_FIELD not in data


def test_history_rebuild_uses_trusted_governance_marker() -> None:
    messages = agentscope_msg_to_message(
        Msg(
            name="Friday",
            role="system",
            content=[
                {
                    "type": "tool_result",
                    "id": "tool-1",
                    "name": "execute_shell_command",
                    "_swe_tool_governance": "pending",
                    "output": {
                        "isError": True,
                        "error_type": "approval_required",
                        "content": [{"type": "text", "text": "risk"}],
                    },
                },
            ],
            timestamp="2026-08-31T08:00:00Z",
        ),
    )

    data = messages[0].content[0].data
    assert data["tool_governance"] == "pending"
    assert "tool_status" not in data
    assert "tool_error" not in data
