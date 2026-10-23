# -*- coding: utf-8 -*-
"""Shell script backup worker.

专门用于 Shell 脚本模式的备份/恢复 worker。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from swe.constant import SECRET_DIR, WORKING_DIR
from swe.config.utils import (
    get_tenant_storage_providers_dir,
    get_tenant_storage_secrets_dir,
    get_tenant_working_dir,
    list_all_tenant_ids,
)

from .config import BackupEnvironmentConfig, ShellScriptConfig
from .io_worker import run_backup_io
from .models import BackupTask, BackupTaskStatus
from .s3_client import S3BackupClient
from .task_store import TaskStore

logger = logging.getLogger(__name__)

# 北京时间
BJ_TZ = ZoneInfo("Asia/Shanghai")


class ShellScriptExecutor:
    """Shell 脚本执行器。

    用于执行压缩和解压 Shell 脚本。
    """

    def __init__(self, config: ShellScriptConfig):
        self.config = config
        self._working_dir = (
            Path(config.working_dir) if config.working_dir else WORKING_DIR
        )
        self._secret_dir = (
            Path(config.secret_dir) if config.secret_dir else SECRET_DIR
        )

    async def run_compress(
        self,
        tenant_ids: list[str] | None,
        output_dir: Path,
        backup_date: str,
        backup_hour: int,
        instance_id: str,
    ) -> tuple[list[str], list[str]]:
        """执行压缩脚本。

        Args:
            tenant_ids: 要压缩的租户 ID 列表
            output_dir: 输出目录
            backup_date: 备份日期 YYYY-MM-DD
            backup_hour: 备份小时 0-23
            instance_id: 实例标识

        Returns:
            (tenant_ids, zip_paths) 成功压缩的结果
        """
        cmd = self._build_compress_command(
            tenant_ids,
            output_dir,
            backup_date,
            backup_hour,
            instance_id,
        )
        stdout, _, _ = await self._execute_script(cmd)
        return self._parse_compress_output(stdout)

    async def run_decompress(
        self,
        zip_dir: Path,
        tenant_ids: list[str] | None,
        rollback_dir: Path | None,
        task_id: str,
    ) -> tuple[list[str], list[str]]:
        """执行解压脚本。

        Args:
            zip_dir: zip 文件目录
            tenant_ids: 要恢复的租户 ID 列表
            rollback_dir: 回滚备份目录
            task_id: 任务 ID

        Returns:
            (restored_tenants, rollback_paths) 成功恢复的结果
        """
        cmd = self._build_decompress_command(
            zip_dir,
            tenant_ids,
            rollback_dir,
            task_id,
        )
        stdout, _, _ = await self._execute_script(cmd)
        return self._parse_decompress_output(stdout)

    def _build_compress_command(
        self,
        tenant_ids: list[str] | None,
        output_dir: Path,
        backup_date: str,
        backup_hour: int,
        instance_id: str,
    ) -> list[str]:
        """构建压缩命令。"""
        cmd = [
            self.config.compress_script_path,
            "--working-dir",
            str(self._working_dir),
            "--secret-dir",
            str(self._secret_dir),
            "--output-dir",
            str(output_dir),
            "--date",
            backup_date,
            "--hour",
            str(backup_hour),
            "--instance-id",
            instance_id,
        ]
        if tenant_ids:
            cmd.extend(["--tenants", ",".join(tenant_ids)])
        return cmd

    def _build_decompress_command(
        self,
        zip_dir: Path,
        tenant_ids: list[str] | None,
        rollback_dir: Path | None,
        task_id: str,
    ) -> list[str]:
        """构建解压命令。"""
        cmd = [
            self.config.decompress_script_path,
            "--zip-dir",
            str(zip_dir),
            "--working-dir",
            str(self._working_dir),
            "--secret-dir",
            str(self._secret_dir),
        ]
        if tenant_ids:
            cmd.extend(["--tenants", ",".join(tenant_ids)])
        if rollback_dir:
            cmd.extend(["--rollback-dir", str(rollback_dir)])
        if task_id:
            cmd.extend(["--task-id", task_id])
        return cmd

    async def _execute_script(self, cmd: list[str]) -> tuple[str, str, int]:
        """异步执行脚本。"""
        logger.info("Executing shell script: %s", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            logger.warning("Script execution timed out, killing process")
            process.kill()
            await process.wait()
            raise RuntimeError(
                f"Script timed out after {self.config.timeout_seconds}s",
            ) from exc

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        if process.returncode != 0:
            logger.error(
                "Script failed with code %d: %s",
                process.returncode,
                stderr_str,
            )
            raise RuntimeError(
                f"Script failed with code {process.returncode}: {stderr_str}",
            )

        logger.info("Script executed successfully")
        return stdout_str, stderr_str, process.returncode or 0

    def _parse_compress_output(
        self,
        stdout: str,
    ) -> tuple[list[str], list[str]]:
        """解析压缩脚本输出。"""
        tenant_ids = []
        zip_paths = []

        for line in stdout.splitlines():
            if line.startswith("SUCCESS:"):
                parts = line.split(":")
                if len(parts) >= 3:
                    tenant_ids.append(parts[1])
                    zip_paths.append(parts[2])

        return tenant_ids, zip_paths

    def _parse_decompress_output(
        self,
        stdout: str,
    ) -> tuple[list[str], list[str]]:
        """解析解压脚本输出。"""
        restored_tenants = []
        rollback_paths = []

        for line in stdout.splitlines():
            if line.startswith("SUCCESS:"):
                parts = line.split(":")
                if len(parts) >= 2:
                    restored_tenants.append(parts[1])
            elif line.startswith("ROLLBACK:"):
                parts = line.split(":")
                if len(parts) >= 3:
                    rollback_paths.append(parts[2])

        return restored_tenants, rollback_paths


class ShellBackupWorker:
    """Shell 脚本备份 worker。

    专门用于执行 Shell 脚本模式的备份和恢复操作。
    """

    def __init__(
        self,
        task_store: TaskStore,
        config: BackupEnvironmentConfig,
        shell_config: ShellScriptConfig,
    ):
        self.task_store = task_store
        self.config = config
        self.s3_client = S3BackupClient(config)
        self.shell_executor = ShellScriptExecutor(shell_config)

    # pylint: disable=too-many-statements
    async def run_backup_task(self, task: BackupTask) -> None:
        """执行 Shell 备份任务。"""
        output_dir = None
        task.status = BackupTaskStatus.RUNNING
        task.started_at = datetime.now(BJ_TZ)
        self.task_store.save(task)

        try:
            # 1. 获取租户列表
            if task.target_tenant_ids:
                tenant_ids = task.target_tenant_ids
            else:
                tenant_ids = self._get_all_tenant_ids()

            if not tenant_ids:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            task.total_tenants = len(tenant_ids)
            task.current_step = "compressing"
            self.task_store.save(task)

            # 2. 创建输出目录
            date_str = task.backup_date or datetime.now(BJ_TZ).strftime(
                "%Y-%m-%d",
            )
            hour = (
                task.backup_hour
                if task.backup_hour is not None
                else datetime.now(BJ_TZ).hour
            )
            instance_id = task.instance_id or "default"

            output_dir = (
                Path(tempfile.gettempdir())
                / f"shell_backup_{task.task_id[:8]}"
            )
            output_dir.mkdir(parents=True, exist_ok=True)

            # 3. 执行压缩脚本
            logger.info(
                "Running shell compress script for %d tenants",
                len(tenant_ids),
            )
            (
                _compressed_tenants,
                zip_paths,
            ) = await self.shell_executor.run_compress(
                tenant_ids=tenant_ids,
                output_dir=output_dir,
                backup_date=date_str,
                backup_hour=hour,
                instance_id=instance_id,
            )

            task.processed_tenants = len(zip_paths)
            task.progress_percent = 50
            task.current_step = "uploading"
            self.task_store.save(task)

            if not zip_paths:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            # 4. 上传到 OSS
            s3_keys = []
            upload_start = time.time()
            max_concurrent = 5

            upload_results = []
            for i in range(0, len(zip_paths), max_concurrent):
                batch = zip_paths[i : i + max_concurrent]
                batch_results = await asyncio.gather(
                    *[
                        self._upload_single_file(
                            Path(p),
                            instance_id,
                            date_str,
                            hour,
                        )
                        for p in batch
                    ],
                )
                upload_results.extend(batch_results)

                # 更新进度
                completed = min(i + max_concurrent, len(zip_paths))
                task.processed_tenants = completed
                task.progress_percent = 50 + int(
                    (completed / len(zip_paths)) * 50,
                )
                self.task_store.save(task)

            # 排序并收集结果
            upload_results.sort(key=lambda x: x[0])
            s3_keys = [r[1] for r in upload_results]

            upload_total_time = time.time() - upload_start
            total_size_mb = sum(Path(p).stat().st_size for p in zip_paths) / (
                1024 * 1024
            )
            logger.info(
                "Shell backup upload time: %.2fs, size: %.2fMB, speed: %.2fMB/s",
                upload_total_time,
                total_size_mb,
                total_size_mb / upload_total_time,
            )

            task.s3_keys = s3_keys
            task.local_zip_paths = zip_paths
            task.status = BackupTaskStatus.COMPLETED
            task.current_step = "completed"
            task.progress_percent = 100

        except Exception as e:
            task.status = BackupTaskStatus.FAILED
            task.error_message = str(e)
            task.current_step = "failed"
        finally:
            task.completed_at = datetime.now(BJ_TZ)
            self.task_store.save(task)

            # 5. 清理本地文件
            for path in task.local_zip_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception as e:
                    logger.warning("Failed to delete zip file %s: %s", path, e)

            if output_dir and output_dir.exists():
                try:
                    shutil.rmtree(output_dir)
                except Exception as e:
                    logger.warning(
                        "Failed to cleanup output dir %s: %s",
                        output_dir,
                        e,
                    )

    def _get_restore_tenant_ids(
        self,
        task: BackupTask,
        instance_id: str,
        backup_date: str,
    ) -> list[str]:
        """获取恢复任务的租户列表。"""
        if task.target_tenant_ids:
            return task.target_tenant_ids

        backups = self.s3_client.list_backups(
            instance_id=instance_id,
            date=backup_date,
            hour=task.backup_hour,
        )
        backup_hour = task.backup_hour if task.backup_hour is not None else 0
        return list(
            backups["backups"]
            .get(instance_id, {})
            .get(backup_date, {})
            .get(backup_hour, {})
            .keys(),
        )

    async def _download_backup_zips(
        self,
        task: BackupTask,
        instance_id: str,
        backup_date: str,
        zip_dir: Path,
    ) -> list[str]:
        """下载备份 zip 文件。"""
        downloaded_zips = []
        tenant_ids = task.target_tenant_ids or []
        backup_hour = task.backup_hour

        for i, tenant_id in enumerate(tenant_ids):
            task.processed_tenants = i + 1
            task.progress_percent = int(((i + 1) / len(tenant_ids)) * 40)
            self.task_store.save(task)

            hour_to_restore = await run_backup_io(
                self._get_backup_hour,
                instance_id,
                backup_date,
                backup_hour,
            )
            if hour_to_restore is None:
                logger.warning(
                    "No backup found for %s/%s/%s",
                    instance_id,
                    backup_date,
                    tenant_id,
                )
                continue

            s3_key = self.s3_client.get_backup_key(
                instance_id,
                backup_date,
                hour_to_restore,
                tenant_id,
            )
            zip_path = zip_dir / f"{tenant_id}.zip"

            await asyncio.to_thread(
                self.s3_client.download,
                s3_key,
                zip_path,
            )
            downloaded_zips.append(str(zip_path))

        return downloaded_zips

    def _get_backup_hour(
        self,
        instance_id: str,
        backup_date: str,
        backup_hour: int | None,
    ) -> int | None:
        """获取备份小时。"""
        if backup_hour is not None:
            return backup_hour

        backups = self.s3_client.list_backups(
            instance_id=instance_id,
            date=backup_date,
        )
        hours = list(
            backups["backups"]
            .get(instance_id, {})
            .get(backup_date, {})
            .keys(),
        )
        return max(hours) if hours else None

    async def _do_rollback(
        self,
        rollback_paths: list[str],
    ) -> None:
        """执行回滚操作。"""
        for rollback_path in rollback_paths:
            try:
                path = Path(rollback_path)
                if not path.exists():
                    continue
                tenant_id = path.stem
                tenant_dir = get_tenant_working_dir(tenant_id)

                if tenant_dir.exists():
                    shutil.rmtree(tenant_dir)

                await self._extract_zip(path, tenant_dir, tenant_id)
            except Exception as e:
                logger.error("Failed to rollback %s: %s", rollback_path, e)

    def _cleanup_rollback_dir(self, rollback_dir: Path, task_id: str) -> None:
        """清理回滚目录。"""
        if not rollback_dir.exists():
            return
        task_rollback_dir = rollback_dir / task_id
        if task_rollback_dir.exists():
            try:
                shutil.rmtree(task_rollback_dir)
            except Exception as e:
                logger.warning("Failed to cleanup rollback dir: %s", e)

    async def run_restore_task(self, task: BackupTask) -> None:
        """执行 Shell 恢复任务。"""
        zip_dir = None
        task.status = BackupTaskStatus.RUNNING
        task.started_at = datetime.now(BJ_TZ)
        self.task_store.save(task)

        rollback_paths = []

        try:
            instance_id = task.instance_id or "default"
            backup_date = task.backup_date

            if not backup_date:
                raise ValueError("backup_date is required for restore task")

            tenant_ids = await run_backup_io(
                self._get_restore_tenant_ids,
                task,
                instance_id,
                backup_date,
            )

            if not tenant_ids:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            task.total_tenants = len(tenant_ids)
            task.target_tenant_ids = tenant_ids
            task.current_step = "downloading"
            self.task_store.save(task)

            zip_dir = (
                Path(tempfile.gettempdir())
                / f"shell_restore_{task.task_id[:8]}"
            )
            zip_dir.mkdir(parents=True, exist_ok=True)

            rollback_dir = WORKING_DIR / ".rollback"

            downloaded_zips = await self._download_backup_zips(
                task,
                instance_id,
                backup_date,
                zip_dir,
            )

            task.current_step = "restoring"
            task.progress_percent = 50
            self.task_store.save(task)

            logger.info(
                "Running shell decompress script for %d tenants",
                len(downloaded_zips),
            )
            (
                restored_tenants,
                rollback_paths,
            ) = await self.shell_executor.run_decompress(
                zip_dir=zip_dir,
                tenant_ids=tenant_ids,
                rollback_dir=rollback_dir,
                task_id=task.task_id,
            )

            task.restored_tenants = restored_tenants
            task.rollback_data_paths = rollback_paths
            task.progress_percent = 90
            self.task_store.save(task)

            self._cleanup_rollback_dir(rollback_dir, task.task_id)

            task.current_step = "completed"
            task.status = BackupTaskStatus.COMPLETED
            task.progress_percent = 100

        except Exception as e:
            task.error_message = str(e)
            task.current_step = "rolling_back"
            task.status = BackupTaskStatus.ROLLING_BACK
            self.task_store.save(task)

            await self._do_rollback(rollback_paths)

            task.status = BackupTaskStatus.ROLLED_BACK
        finally:
            task.completed_at = datetime.now(BJ_TZ)
            self.task_store.save(task)

            if zip_dir and zip_dir.exists():
                try:
                    shutil.rmtree(zip_dir)
                except Exception as e:
                    logger.warning(
                        "Failed to cleanup zip dir %s: %s",
                        zip_dir,
                        e,
                    )

    def _get_all_tenant_ids(self) -> list[str]:
        """获取所有租户 ID。"""
        return list_all_tenant_ids()

    async def _upload_single_file(
        self,
        zip_path: Path,
        instance_id: str,
        date_str: str,
        hour: int,
    ) -> tuple[str, str, float]:
        """上传单个文件到 OSS。"""
        tenant_id = zip_path.stem
        zip_size_mb = zip_path.stat().st_size / (1024 * 1024)

        start = time.time()
        s3_key = await asyncio.to_thread(
            self.s3_client.upload,
            zip_path,
            instance_id,
            date_str,
            hour,
            tenant_id,
        )
        elapsed = time.time() - start
        logger.info(
            "Uploaded tenant %s: %.2fMB in %.2fs (%.2fMB/s)",
            tenant_id,
            zip_size_mb,
            elapsed,
            zip_size_mb / elapsed,
        )
        return tenant_id, s3_key, elapsed

    async def _extract_zip(
        self,
        zip_path: Path,
        target_dir: Path,
        tenant_id: str,
    ) -> None:
        """回滚时使用 Python zipfile 解压。"""
        import zipfile

        def _do_extract():
            target_dir.mkdir(parents=True, exist_ok=True)
            secrets_dir = get_tenant_storage_secrets_dir(tenant_id)
            secrets_dir.mkdir(parents=True, exist_ok=True)
            provider_dir = get_tenant_storage_providers_dir(tenant_id)
            provider_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    if member.filename.startswith(".secret/"):
                        relative_path = member.filename[8:]
                        if not relative_path:
                            continue
                        dest_path = secrets_dir / relative_path
                    elif member.filename.startswith(".providers/"):
                        relative_path = member.filename[11:]
                        if not relative_path:
                            continue
                        dest_path = provider_dir / relative_path
                    else:
                        relative_path = member.filename
                        dest_path = target_dir / relative_path

                    # 验证路径安全
                    resolved_dest = dest_path.resolve()
                    if member.filename.startswith(".secret/"):
                        base = secrets_dir.resolve()
                    elif member.filename.startswith(".providers/"):
                        base = provider_dir.resolve()
                    else:
                        base = target_dir.resolve()
                    if not str(resolved_dest).startswith(str(base)):
                        continue

                    if member.is_dir():
                        dest_path.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        with (
                            zf.open(member) as source,
                            open(
                                dest_path,
                                "wb",
                            ) as target,
                        ):
                            target.write(source.read())

        await asyncio.to_thread(_do_extract)
