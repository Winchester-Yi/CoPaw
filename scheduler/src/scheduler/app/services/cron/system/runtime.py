# -*- coding: utf-8 -*-
"""独立系统生命周期，不借用户模型池、旧绑定或启动DDL装配系统能力。"""

import asyncio
from contextlib import asynccontextmanager
import logging
import os

from ....database import get_db_connection
from ..managed.platform import platform_config
from .dispatch_worker import SystemDispatchWorker
from .executions import SystemExecutionStore
from .management_store import SystemManagementStore
from .platform import SystemPlatformClient
from .schema import assert_system_schema_ready
from .swe_client import SystemSweClient
from .sync_worker import SystemSyncWorker

logger = logging.getLogger(__name__)


async def create_system_workers():
    """只验证人工建好的系统表和已有服务配置，不扫描工作区补造任务。"""
    db = get_db_connection()
    if not db.is_connected:
        raise RuntimeError("system_database_unavailable")
    await assert_system_schema_ready(db)
    store = SystemManagementStore(db)
    swe = SystemSweClient(os.environ.get("SCHEDULER_SWE_API_BASE_URL", ""))
    platform = SystemPlatformClient(platform_config())
    return [
        SystemSyncWorker(store, swe, platform),
        SystemDispatchWorker(SystemExecutionStore(db), store, swe),
    ]


def _completed(app, task):
    app.state.system_cron_ready = False
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "系统定时后台异常退出，请核对共库水位和待核实执行 error_type=%s",
            type(task.exception()).__name__,
        )


async def _stop(tasks, stop):
    stop.set()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10,
        )
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.warning("系统后台已取消，未确认工作保留共库状态等待续办或核实")


@asynccontextmanager
async def system_lifespan(app):
    """系统后台先于共库停止，独立失败不改变原有用户运行能力。"""
    app.state.system_cron_prepared = True
    app.state.system_cron_ready = False
    app.state.system_cron_tasks = []
    try:
        workers = await create_system_workers()
    except Exception as error:
        logger.warning(
            "新版系统定时后台未启动 error_type=%s",
            type(error).__name__,
        )
        yield
        return
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(
            worker.run_loop(stop),
            name=f"system-cron:{type(worker).__name__}",
        )
        for worker in workers
    ]
    app.state.system_cron_tasks = tasks
    app.state.system_cron_ready = True
    for task in tasks:
        task.add_done_callback(lambda done: _completed(app, done))
    try:
        yield
    finally:
        app.state.system_cron_ready = False
        await _stop(tasks, stop)
