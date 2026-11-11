# -*- coding: utf-8 -*-
"""SWE复用原系统业务并直接写共库事实，不向Scheduler回传最终结果。"""

import asyncio
import logging
from uuid import uuid4

from .system_results import safe_result

logger = logging.getLogger(__name__)


def business_outcome(result):
    """只统计安全计数；批维护部分失败不能冒充整体成功。"""
    result = result if isinstance(result, dict) else {}
    counters = safe_result(result)
    errors = result.get("errors")
    errors_count = len(errors) if isinstance(errors, list) else 0
    failed = bool(
        errors_count
        or result.get("timed_out")
        or counters.get("scopes_failed")
        or counters.get("workspaces_failed"),
    )
    if not failed:
        return "success", counters, None
    counters["errors_count"] = max(
        errors_count,
        counters.get("errors_count", 0),
    )
    progressed = counters.get("scopes_seen", 0) > counters.get(
        "scopes_failed",
        0,
    ) or any(
        counters.get(key, 0) > 0
        for key in (
            "workspaces_processed",
            "sessions_cleaned",
            "files_archived",
        )
    )
    return (
        "partial" if progressed else "error",
        counters,
        "system_maintenance_partial_failure",
    )


class SystemExecutionService:
    """受理与开始分开，资源准备在事务外，业务开始只能赢得一次围栏。"""

    def __init__(self, writer, read_config, prepare, *, prepare_timeout=300):
        self.writer = writer
        self.read_config = read_config
        self.prepare = prepare
        self.prepare_timeout = prepare_timeout

    async def _prepare(self, run, job, owner):
        config = await self.read_config(job)
        if config.get("enabled") is not True:
            await self.writer.start(
                run["execution_id"],
                job["job_id"],
                owner,
                config,
            )
            return None
        action = await self.prepare(job)
        # 工作区启动耗时期间可能保存关闭配置，动作前必须重新只读核验。
        config = await self.read_config(job)
        started = await self.writer.start(
            run["execution_id"],
            job["job_id"],
            owner,
            config,
        )
        return (action, config) if started is not None else None

    async def execute(self, run, job):
        """同ID的资源准备可竞争，唯有共库running围栏的获得者执行实际动作。"""
        owner = str(uuid4())
        execution_id, job_id = run["execution_id"], job["job_id"]
        try:
            prepared = await asyncio.wait_for(
                self._prepare(run, job, owner),
                timeout=self.prepare_timeout,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self.writer.reject_not_started(
                    execution_id,
                    job_id,
                    "system_resource_prepare_cancelled",
                ),
            )
            raise
        except Exception as error:
            logger.warning(
                "系统业务准备失败 execution_id=%s error_type=%s",
                execution_id,
                type(error).__name__,
            )
            await self.writer.reject_not_started(
                execution_id,
                job_id,
                "system_resource_prepare_failed",
            )
            return
        if prepared is None:
            return
        action, config = prepared
        await self._run(execution_id, job_id, owner, action, config)

    async def _run(self, execution_id, job_id, owner, action, config):
        try:
            result = await asyncio.wait_for(
                action(),
                timeout=config["timeout_seconds"],
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self.writer.unknown(
                    execution_id,
                    job_id,
                    owner,
                    "system_execution_cancelled",
                ),
            )
            raise
        except asyncio.TimeoutError:
            await self.writer.unknown(
                execution_id,
                job_id,
                owner,
                "system_execution_timeout",
            )
            return
        except Exception as error:
            logger.warning(
                "系统业务失败 execution_id=%s error_type=%s",
                execution_id,
                type(error).__name__,
            )
            await self.writer.finish(
                execution_id,
                job_id,
                owner,
                "error",
                error_code="system_execution_failed",
            )
            return
        status, counters, code = business_outcome(result)
        await self.writer.finish(
            execution_id,
            job_id,
            owner,
            status,
            result=counters,
            error_code=code,
        )
