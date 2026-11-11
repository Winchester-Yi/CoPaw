# -*- coding: utf-8 -*-
"""只同步已受理的系统配置操作，平台HTTP不占用任务行锁。"""

import asyncio
import logging
from uuid import uuid4

from ..managed.operations import RegistrationRejected
from .configuration import normalize_config

logger = logging.getLogger(__name__)


def _failure_code(phase, error):
    if phase == "register":
        return (
            "system_platform_registration_rejected"
            if isinstance(error, RegistrationRejected)
            else "system_platform_registration_unknown"
        )
    return {
        "config": "system_config_unavailable",
        "update": "system_platform_update_failed",
        "enable": "system_platform_enable_failed",
        "pause": "system_platform_pause_failed",
    }.get(phase, "system_sync_failed")


class SystemSyncWorker:
    """后台串行推进同身份；不同身份可受限并发，绝不全量初始化配置。"""

    def __init__(self, store, swe, platform, *, concurrency=4):
        self.store = store
        self.swe = swe
        self.platform = platform
        self.slots = asyncio.Semaphore(concurrency)

    async def process(self, job_id):
        """续办一个平台同步操作，失败只写安全阶段码，不重跑业务。"""
        claimed = await self.store.claim(job_id, str(uuid4()))
        if claimed is None:
            return False
        phase = "config"
        try:
            config = normalize_config(await self.swe.get_config(claimed))
            if not await self.store.prepare(claimed, config):
                return False
            external_id = claimed["external_job_id"]
            if external_id is None and not config["enabled"]:
                return await self.store.finish(claimed, None, config)
            if external_id is not None and not config["enabled"]:
                phase = "pause"
                if not await self.store.stage(claimed, "pausing"):
                    return False
                await self.platform.set_enabled(external_id, False)
                if not config["cron"]:
                    return await self.store.finish(
                        claimed,
                        external_id,
                        config,
                    )
            if external_id is None:
                phase = "register"
                if not await self.store.stage(claimed, "registering"):
                    return False
                external_id = await self.platform.register(claimed, config)
                if not await self.store.bind(claimed, external_id):
                    return False
            else:
                phase = "update"
                if not await self.store.stage(claimed, "updating"):
                    return False
                await self.platform.update(external_id, claimed, config)
            phase = "enable"
            if not await self.store.stage(claimed, "enabling"):
                return False
            await self.platform.set_enabled(external_id, config["enabled"])
            return await self.store.finish(claimed, external_id, config)
        except Exception as error:
            code = _failure_code(phase, error)
            await self.store.fail(claimed, code)
            logger.warning(
                "系统任务平台同步未完成 job_id=%s stage=%s code=%s error_type=%s",
                job_id,
                phase,
                code,
                type(error).__name__,
            )
            return False

    async def _bounded(self, job_id):
        async with self.slots:
            return await self.process(job_id)

    async def run_once(self):
        """仅领取表中待同步/租约过期的操作，不从文件或配置扫描补建。"""
        rows = await self.store.pending()
        results = await asyncio.gather(
            *(self._bounded(row["job_id"]) for row in rows),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            logger.warning("部分系统配置操作暂不可推进，保留各自水位和租约")
        return len(rows)

    async def run_loop(self, stop_event):
        """生命周期结束可中断等待，未确认的平台阶段由持久状态保留。"""
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception as error:
                logger.warning(
                    "系统配置同步循环暂不可用 error_type=%s",
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                continue
