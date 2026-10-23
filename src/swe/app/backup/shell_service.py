# -*- coding: utf-8 -*-
"""Shell script backup service layer.

提供独立的 Shell 脚本备份服务。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from swe.config.utils import list_all_tenant_ids
from .config import BackupConfig, load_backup_config
from .io_worker import run_backup_io
from .models import BackupTask, BackupTaskStatus, BackupTaskType
from .task_store import TaskStore
from .shell_worker import ShellBackupWorker

logger = logging.getLogger(__name__)


class ShellBackupService:
    """Shell 脚本备份服务。

    提供独立的 Shell 备份接口，不依赖原有的 Python zipfile 备份。
    """

    def __init__(self):
        self.task_store = TaskStore()
        self._worker: ShellBackupWorker | None = None
        self._lock = asyncio.Lock()
        self._worker_lock = threading.Lock()

    def _get_backup_config(self) -> BackupConfig:
        """获取备份配置。"""
        config = load_backup_config()
        if config is None:
            raise HTTPException(
                status_code=400,
                detail="Backup not configured",
            )
        return config

    def _get_worker(self) -> ShellBackupWorker:
        """获取或创建 Shell 备份 worker 实例。"""
        with self._worker_lock:
            if self._worker is None:
                backup_config = self._get_backup_config()
                env_config = backup_config.get_active_config()
                if env_config is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Backup environment not configured",
                    )

                # 检查平台兼容性
                if sys.platform == "win32":
                    logger.warning(
                        "Shell backup mode not supported on Windows platform",
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Shell backup mode is only supported on Linux/Unix platforms",
                    )

                self._worker = ShellBackupWorker(
                    self.task_store,
                    env_config,
                    backup_config.shell_script,
                )
                logger.info("ShellBackupWorker initialized")
            return self._worker

    async def create_backup_task(
        self,
        tenant_ids: list[str] | None = None,
        instance_id: str | None = None,
        backup_date: str | None = None,
        backup_hour: int | None = None,
    ) -> BackupTask:
        """创建 Shell 备份任务。

        Args:
            tenant_ids: 指定租户列表，None 表示所有租户
            instance_id: 实例标识
            backup_date: 备份日期 YYYY-MM-DD，默认当前日期
            backup_hour: 备份小时 0-23，默认当前小时
        """
        async with self._lock:
            if self.task_store.has_running_task():
                raise HTTPException(
                    status_code=409,
                    detail="Another backup task is already running",
                )
            worker = await run_backup_io(self._get_worker)

        # 设置备份日期和时间（北京时间），默认当前时间
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if backup_date is None:
            backup_date = now.strftime("%Y-%m-%d")
        if backup_hour is None:
            backup_hour = now.hour

        task = BackupTask(
            task_id=str(uuid.uuid4()),
            task_type=BackupTaskType.BACKUP,
            status=BackupTaskStatus.PENDING,
            created_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            target_tenant_ids=tenant_ids,
            instance_id=instance_id,
            backup_date=backup_date,
            backup_hour=backup_hour,
        )
        self.task_store.save(task)

        # 启动异步执行
        asyncio.create_task(worker.run_backup_task(task))

        return task

    async def create_restore_task(
        self,
        date: str | None = None,
        hour: int | None = None,
        instance_id: str | None = None,
        tenant_ids: list[str] | None = None,
    ) -> BackupTask:
        """创建 Shell 恢复任务。

        Args:
            date: 备份日期 YYYY-MM-DD，默认当天
            hour: 备份小时 0-23
            instance_id: 实例标识
            tenant_ids: 指定租户列表
        """
        async with self._lock:
            if self.task_store.has_running_task():
                raise HTTPException(
                    status_code=409,
                    detail="Another backup task is already running",
                )
            worker = await run_backup_io(self._get_worker)

        # 设置日期
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if date is None:
            date = now.strftime("%Y-%m-%d")

        task = BackupTask(
            task_id=str(uuid.uuid4()),
            task_type=BackupTaskType.RESTORE,
            status=BackupTaskStatus.PENDING,
            created_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            backup_date=date,
            backup_hour=hour,
            instance_id=instance_id,
            target_tenant_ids=tenant_ids,
        )
        self.task_store.save(task)

        # 启动异步执行
        asyncio.create_task(worker.run_restore_task(task))

        return task

    def get_task(self, task_id: str) -> BackupTask | None:
        """获取任务详情。"""
        return self.task_store.get(task_id)

    def list_tasks(
        self,
        status: BackupTaskStatus | None = None,
        limit: int = 50,
    ) -> list[BackupTask]:
        """查询任务列表。"""
        return self.task_store.get_all(status=status, limit=limit)

    def list_available_backups(
        self,
        instance_id: str | None = None,
        date: str | None = None,
        hour: int | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        """列出 OSS 上可用的备份。

        Args:
            instance_id: 实例标识
            date: 日期 YYYY-MM-DD
            hour: 小时 0-23
            tenant_id: 租户 ID
        """
        backup_config = self._get_backup_config()
        env_config = backup_config.get_active_config()
        if env_config is None:
            raise HTTPException(
                status_code=400,
                detail="Backup environment not configured",
            )

        from .s3_client import S3BackupClient

        s3_client = S3BackupClient(env_config)
        return s3_client.list_backups(
            instance_id=instance_id,
            date=date,
            hour=hour,
            tenant_id=tenant_id,
        )

    @staticmethod
    def list_all_tenant_ids() -> list[str]:
        """列出所有租户 ID。"""
        return list_all_tenant_ids()
