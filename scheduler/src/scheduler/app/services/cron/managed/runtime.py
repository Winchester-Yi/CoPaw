# -*- coding: utf-8 -*-
"""装配新版持久后台；先检查结构，停止后台后才允许外层关闭共库。"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from .....config.constant import SWE_INTERNAL_API_BASE_URL_ENV
from ....database.connection import get_db_connection
from ..capacity_claim import (
    enable_managed_capacity_accounting,
    set_managed_priority,
)
from ..scheduling_service import get_cron_scheduling_service
from ..system.swe_client import SystemSweClient
from .batch_retry_policy import BatchRetryPolicy
from .capacity import ManagedCapacityGate
from .dispatcher import ManagedSweExecutionClient
from .platform import ManagedPlatformClient, platform_config
from .recovery import ManagedRecoveryWorker
from .schema import assert_managed_schema_ready
from .swe_client import ManagedSweClient
from .feedback import ManagedCapacityFeedback
from .board_schema import assert_managed_board_schema_ready
from .recheck_worker import prepare_recheck_storage

logger = logging.getLogger(__name__)

# 恢复巡检单轮工作量上限：派发候选数、广播/删除/暂停分页、当日补跑页大小共用。
MANAGED_RECOVERY_LIMIT_ENV = "SCHEDULER_MANAGED_RECOVERY_LIMIT"
MANAGED_RECOVERY_LIMIT_DEFAULT = 32
MANAGED_RECOVERY_LIMIT_MAX = 200
# 恢复巡检轮询间隔：每轮结束后等待的秒数。
MANAGED_RECOVERY_INTERVAL_ENV = "SCHEDULER_MANAGED_RECOVERY_INTERVAL_SECONDS"
MANAGED_RECOVERY_INTERVAL_DEFAULT = 2
MANAGED_RECOVERY_INTERVAL_MAX = 60


def _bounded_int_env(name, default, maximum):
    """读取正整数环境变量；缺失或非法时回退默认值，避免启动失败。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= maximum:
        logger.warning(
            "恢复巡检参数无效，回退默认值 env=%s default=%s max=%s",
            name,
            default,
            maximum,
        )
        return default
    return value


def managed_recovery_limit():
    """恢复巡检单轮上限，默认 32，取值 1-200。"""
    return _bounded_int_env(
        MANAGED_RECOVERY_LIMIT_ENV,
        MANAGED_RECOVERY_LIMIT_DEFAULT,
        MANAGED_RECOVERY_LIMIT_MAX,
    )


def managed_recovery_interval():
    """恢复巡检轮询间隔秒数，默认 2，取值 1-60。"""
    return _bounded_int_env(
        MANAGED_RECOVERY_INTERVAL_ENV,
        MANAGED_RECOVERY_INTERVAL_DEFAULT,
        MANAGED_RECOVERY_INTERVAL_MAX,
    )


async def _fetch_batch_retry_overrides():
    client = SystemSweClient(os.environ.get(SWE_INTERNAL_API_BASE_URL_ENV, ""))
    return await client.get_batch_retry_overrides(
        os.environ.get("SWE_INTERNAL_TOKEN"),
    )


async def create_managed_worker(db):
    await assert_managed_schema_ready(db)
    await assert_managed_board_schema_ready(db)
    # 在启动旧循环之前启用合并统计，即使后续新客户端配置失败，
    # 已有新运行占位也不能被旧循环忽略。这里没有新增环境开关。
    enable_managed_capacity_accounting()
    legacy = get_cron_scheduling_service()
    legacy.bind_managed_feedback(ManagedCapacityFeedback(db))
    rechecks_enabled = await prepare_recheck_storage(db)
    base = os.environ.get("SCHEDULER_SWE_API_BASE_URL", "")
    resources = ManagedSweClient(base)
    execution = ManagedSweExecutionClient(base)
    platform = ManagedPlatformClient(platform_config())
    retry_policy = BatchRetryPolicy(_fetch_batch_retry_overrides)
    return ManagedRecoveryWorker(
        db,
        resources,
        platform,
        execution,
        limit=managed_recovery_limit(),
        capacity=ManagedCapacityGate(legacy),
        rechecks_enabled=rechecks_enabled,
        retry_overrides=retry_policy.read_overrides,
    )


async def prepare_managed_runtime(app):
    """在旧循环启动前只准备一次，结构或配置不足时保留旧启动行为。"""
    set_managed_priority(False)
    app.state.managed_cron_runtime_prepared = True
    app.state.managed_cron_runtime_ready = False
    app.state.managed_cron_worker = None
    try:
        db = get_db_connection()
        if not db.is_connected:
            return
        app.state.managed_cron_worker = await create_managed_worker(db)
    except Exception as exc:
        logger.warning(
            "新版定时后台未启动，旧链路保持原入口 table=%s error_type=%s",
            getattr(exc, "table", None),
            type(exc).__name__,
        )


def _completed(app, task):
    set_managed_priority(False)
    app.state.managed_cron_runtime_ready = False
    if not task.cancelled() and task.exception() is not None:
        logger.error("新版定时后台异常退出，需要核对服务日志和共库登记")


async def _stop(task, stop_event):
    stop_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.info("新版定时后台已取消，未完成工作保留持久租约供恢复")
    except asyncio.CancelledError:
        child_was_cancelled = task.cancelled()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not child_was_cancelled:
            raise
        logger.info("新版后台此前已取消，继续完成外层资源关闭")
    except Exception as exc:
        logger.warning(
            "新版后台停止时存在未确认状态 error_type=%s",
            type(exc).__name__,
        )


@asynccontextmanager
async def managed_lifespan(app):
    if not getattr(app.state, "managed_cron_runtime_prepared", False):
        await prepare_managed_runtime(app)
    worker = app.state.managed_cron_worker
    if worker is None:
        yield
        return
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        worker.run_loop(
            stop_event=stop_event,
            interval_seconds=managed_recovery_interval(),
        ),
        name="managed-cron-service",
    )
    app.state.managed_cron_task = task
    app.state.managed_cron_stop_event = stop_event
    app.state.managed_cron_runtime_ready = True
    set_managed_priority(True)
    task.add_done_callback(lambda done: _completed(app, done))
    logger.info("新版定时后台已启动，只恢复已受理的新协议工作")
    try:
        yield
    finally:
        set_managed_priority(False)
        await _stop(task, stop_event)
        app.state.managed_cron_runtime_ready = False
        logger.info("新版定时后台已停止")
