# -*- coding: utf-8 -*-
"""只派发已登记的系统执行，传输重试不产生新业务周期。"""

import asyncio
import logging
from uuid import uuid4

logger = logging.getLogger(__name__)


class SystemDispatchWorker:
    """用户模型池之外的有界传输工作者，最终状态由SWE直接写共库。"""

    def __init__(self, store, management, swe, *, concurrency=4):
        self.store = store
        self.management = management
        self.swe = swe
        self.slots = asyncio.Semaphore(concurrency)

    async def process(self, execution_id):
        run = await self.store.claim_dispatch(execution_id, str(uuid4()))
        if run is None:
            return False
        try:
            job = await self.management.get(run["job_id"])
            receipt = await self.swe.execute(job, run)
            if (
                receipt.get("execution_id") != execution_id
                or receipt.get("accepted") is not True
            ):
                raise ValueError("invalid_system_execution_receipt")
            return True
        except Exception as error:
            await self.store.dispatch_failed(
                execution_id,
                run["dispatch_owner"],
            )
            logger.warning(
                "系统派发暂未确认 execution_id=%s error_type=%s",
                execution_id,
                type(error).__name__,
            )
            return False

    async def _bounded(self, execution_id):
        async with self.slots:
            return await self.process(execution_id)

    async def run_once(self):
        """只处理已存在的执行与过期运行，不扫描配置或推算漏掉的周期。"""
        for row in await self.store.expired_running():
            await self.store.mark_unknown(row["execution_id"])
        rows = await self.store.pending()
        results = await asyncio.gather(
            *(self._bounded(row["execution_id"]) for row in rows),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            logger.warning("部分系统派发暂不可推进，保留持久状态和围栏")
        return len(rows)

    async def run_loop(self, stop_event):
        """关闭时中断短等待；未确认的受理以共库事实为准。"""
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception as error:
                logger.warning(
                    "系统派发循环暂不可用 error_type=%s",
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                continue
