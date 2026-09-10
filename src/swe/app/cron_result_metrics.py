# -*- coding: utf-8 -*-
"""定时任务结果一致性指标的进程内计数器。"""

from __future__ import annotations

from collections import Counter
from threading import Lock

CRON_EMPTY_MODEL_OUTPUT_TOTAL = "cron_empty_model_output_total"
CRON_SUCCESS_WITHOUT_ASSISTANT_TOTAL = (
    "cron_success_without_assistant_total"
)
CRON_SESSION_COMMIT_FAILURE_TOTAL = "cron_session_commit_failure_total"
CRON_CANCEL_AFTER_COMPLETED_TOTAL = "cron_cancel_after_completed_total"
CRON_SESSION_ROUTE_MISMATCH_TOTAL = "cron_session_route_mismatch_total"
CRON_EXECUTION_KEY_CONFLICT_TOTAL = "cron_execution_key_conflict_total"

_counts: Counter[str] = Counter()
_lock = Lock()


def increment_cron_result_metric(name: str) -> None:
    """递增一个受控的 Cron 结果一致性指标。"""
    with _lock:
        _counts[name] += 1


def get_cron_result_metrics() -> dict[str, int]:
    """返回所有结果一致性指标，未发生的指标也显式为零。"""
    names = (
        CRON_EMPTY_MODEL_OUTPUT_TOTAL,
        CRON_SUCCESS_WITHOUT_ASSISTANT_TOTAL,
        CRON_SESSION_COMMIT_FAILURE_TOTAL,
        CRON_CANCEL_AFTER_COMPLETED_TOTAL,
        CRON_SESSION_ROUTE_MISMATCH_TOTAL,
        CRON_EXECUTION_KEY_CONFLICT_TOTAL,
    )
    with _lock:
        return {name: _counts[name] for name in names}


def reset_cron_result_metrics_for_test() -> None:
    """清空进程内计数器，仅供测试隔离使用。"""
    with _lock:
        _counts.clear()
