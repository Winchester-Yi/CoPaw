# -*- coding: utf-8 -*-
"""Async worker for executing backup/restore tasks."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from swe.constant import SECRET_DIR, WORKING_DIR
from swe.config.utils import (
    get_tenant_working_dir,
    get_tenant_secrets_dir,
    list_all_tenant_ids,
)

from .config import BackupEnvironmentConfig
from .models import BackupTask, BackupTaskStatus
from .s3_client import S3BackupClient
from .task_store import TaskStore

logger = logging.getLogger(__name__)

# Beijing timezone for consistent time handling
BJ_TZ = ZoneInfo("Asia/Shanghai")

# Encodings to try when UTF-8 fails (common for Chinese Windows systems)
FALLBACK_ENCODINGS = ["gb18030", "gbk", "big5", "cp936", "latin-1"]


def safe_archive_name(file_path: Path, base_dir: Path) -> str | None:
    """Safely convert file path to archive name with multi-encoding support."""
    try:
        relative = file_path.relative_to(base_dir)
        name = str(relative)

        try:
            name.encode("utf-8")
            return name
        except UnicodeEncodeError:
            pass

        for encoding in FALLBACK_ENCODINGS:
            try:
                encoded = name.encode(encoding, errors="strict")
                decoded = encoded.decode(encoding)
                return decoded.encode("utf-8", errors="replace").decode(
                    "utf-8",
                )
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue

        try:
            return name.encode("utf-8", errors="surrogatepass").decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            logger.warning(f"Cannot encode file name: {file_path}")
            return None
    except Exception as e:
        logger.warning(f"Error processing path {file_path}: {e}")
        return None


def _compress_directory(
    zf: zipfile.ZipFile,
    source_dir: Path,
    base_dir: Path,
    prefix: str = "",
    skipped: list | None = None,
) -> None:
    """Compress a directory into a zip file.

    Args:
        zf: ZipFile object to write to.
        source_dir: Directory to compress.
        base_dir: Base directory for relative paths.
        prefix: Optional prefix for archive names (e.g., ".secret/").
        skipped: Optional list to collect skipped file paths.
    """
    if not source_dir.exists():
        return

    for file in source_dir.rglob("*"):
        try:
            arcname = safe_archive_name(file, base_dir)
            if not arcname:
                if skipped is not None:
                    skipped.append(str(file))
                continue

            if prefix:
                arcname = f"{prefix}{arcname}"

            if file.is_file():
                zf.write(file, arcname)
            elif file.is_dir() and not any(file.iterdir()):
                zf.writestr(f"{arcname}/", "")
        except (PermissionError, OSError) as e:
            logger.warning(f"Error accessing {file}: {e}")
            if skipped is not None:
                skipped.append(str(file))


class BackupWorker:
    """Async worker for backup and restore operations."""

    def __init__(self, task_store: TaskStore, config: BackupEnvironmentConfig):
        self.task_store = task_store
        self.config = config
        self.s3_client = S3BackupClient(config)

    # pylint: disable=too-many-statements
    async def run_backup_task(
        self,
        task: BackupTask,
    ) -> None:
        """Execute a backup task."""
        task.status = BackupTaskStatus.RUNNING
        task.started_at = datetime.now(BJ_TZ)
        self.task_store.save(task)

        try:
            if task.target_tenant_ids:
                tenant_ids = task.target_tenant_ids
            else:
                tenant_ids = self._get_all_tenant_ids()

            # Check for empty tenant list to avoid division by zero
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

            s3_keys = []
            local_paths = []

            # Use task's backup_date and backup_hour
            date_str = task.backup_date or datetime.now(BJ_TZ).strftime(
                "%Y-%m-%d",
            )
            hour = (
                task.backup_hour
                if task.backup_hour is not None
                else datetime.now(BJ_TZ).hour
            )
            instance_id = task.instance_id or "default"

            # Prepare compression tasks
            compress_tasks = []
            for tenant_id in tenant_ids:
                tenant_dir = get_tenant_working_dir(tenant_id)
                if not tenant_dir.exists():
                    continue
                zip_path = (
                    Path(tempfile.gettempdir()) / f"backup_{tenant_id}.zip"
                )
                compress_tasks.append((tenant_id, tenant_dir, zip_path))

            task.total_tenants = len(compress_tasks)
            self.task_store.save(task)

            # Parallel compression with concurrency limit
            compress_start = time.time()
            max_compress_concurrent = 3  # Limit to avoid high memory usage

            results = []
            for i in range(0, len(compress_tasks), max_compress_concurrent):
                batch = compress_tasks[i : i + max_compress_concurrent]
                batch_results = await asyncio.gather(
                    *[
                        self._compress_single_tenant(tid, td, zp)
                        for tid, td, zp in batch
                    ],
                )
                results.extend(batch_results)

                # Update progress
                completed = min(
                    i + max_compress_concurrent,
                    len(compress_tasks),
                )
                task.processed_tenants = completed
                task.progress_percent = int(
                    (completed / len(compress_tasks)) * 50,
                )
                self.task_store.save(task)

            # Sort by tenant_id and collect paths
            results.sort(key=lambda x: x[0])
            local_paths = [r[1] for r in results]

            compress_total_time = time.time() - compress_start
            total_size_mb = sum(r[2] for r in results)
            logger.info(
                f"Total compression time (parallel, max {max_compress_concurrent}): "
                f"{compress_total_time:.2f}s for {len(local_paths)} tenants, "
                f"total size: {total_size_mb:.2f}MB",
            )

            # Upload to S3
            task.current_step = "uploading"
            self.task_store.save(task)

            # Check for empty local_paths to avoid division by zero
            if not local_paths:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            upload_start = time.time()

            # Parallel upload with concurrency limit
            max_concurrent = (
                5  # Limit concurrent uploads to avoid overwhelming S3
            )
            upload_results = []
            for i in range(0, len(local_paths), max_concurrent):
                batch = local_paths[i : i + max_concurrent]
                batch_results = await asyncio.gather(
                    *[
                        self._upload_single_file(
                            Path(p),  # type: ignore[arg-type]
                            instance_id,
                            date_str,
                            hour,
                        )
                        for p in batch
                    ],
                )
                upload_results.extend(batch_results)

                # Update progress
                completed = min(i + max_concurrent, len(local_paths))
                task.processed_tenants = completed
                task.progress_percent = 50 + int(
                    (completed / len(local_paths)) * 50,
                )
                self.task_store.save(task)

            # Sort results by tenant_id to maintain consistent order
            upload_results.sort(key=lambda x: x[0])
            s3_keys = [r[1] for r in upload_results]

            upload_total_time = time.time() - upload_start
            total_size_mb = sum(
                Path(p).stat().st_size for p in local_paths
            ) / (1024 * 1024)
            logger.info(
                f"Total upload time (parallel, max {max_concurrent}): {upload_total_time:.2f}s, "
                f"total size: {total_size_mb:.2f}MB, "
                f"effective speed: {total_size_mb / upload_total_time:.2f}MB/s",
            )

            task.s3_keys = s3_keys
            task.local_zip_paths = local_paths
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
            # Cleanup temp files
            for path in task.local_zip_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp file {path}: {e}")

    # pylint: disable=too-many-statements,too-many-branches
    async def run_restore_task(
        self,
        task: BackupTask,
    ) -> None:
        """Execute a restore task."""
        task.status = BackupTaskStatus.RUNNING
        task.started_at = datetime.now(BJ_TZ)
        self.task_store.save(task)

        rollback_paths = []

        try:
            instance_id = task.instance_id or "default"
            backup_date = task.backup_date
            backup_hour = task.backup_hour

            if not backup_date:
                raise ValueError("backup_date is required for restore task")

            # Get target tenants
            if task.target_tenant_ids:
                tenant_ids = task.target_tenant_ids
            else:
                # List all backups for the date/hour/instance
                backups = self.s3_client.list_backups(
                    instance_id=instance_id,
                    date=backup_date,
                    hour=backup_hour,
                )
                tenant_ids = list(
                    backups["backups"]
                    .get(instance_id, {})
                    .get(backup_date, {})
                    .get(backup_hour if backup_hour is not None else 0, {})
                    .keys(),
                )

            # Check for empty tenant list to avoid division by zero
            if not tenant_ids:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            task.total_tenants = len(tenant_ids)
            task.current_step = "backing_up_current"
            self.task_store.save(task)

            # Backup current data for rollback
            for i, tenant_id in enumerate(tenant_ids):
                tenant_dir = get_tenant_working_dir(tenant_id)
                if tenant_dir.exists():
                    rollback_path = await self._create_rollback_backup(
                        task.task_id,
                        tenant_id,
                        tenant_dir,
                    )
                    rollback_paths.append(rollback_path)

            task.rollback_data_paths = rollback_paths
            task.current_step = "downloading"
            self.task_store.save(task)

            # Download and restore
            restored_tenants = []
            for i, tenant_id in enumerate(tenant_ids):
                task.processed_tenants = i + 1
                task.progress_percent = int(((i + 1) / len(tenant_ids)) * 50)
                self.task_store.save(task)

                # Get the backup hour (use task's hour or find latest available)
                hour_to_restore = backup_hour
                if hour_to_restore is None:
                    # Find the latest hour with backup for this tenant
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
                    if hours:
                        hour_to_restore = max(hours)
                    else:
                        logger.warning(
                            f"No backup found for {instance_id}/{backup_date}/{tenant_id}",
                        )
                        continue

                s3_key = self.s3_client.get_backup_key(
                    instance_id,
                    backup_date,
                    hour_to_restore,
                    tenant_id,
                )
                zip_path = (
                    Path(tempfile.gettempdir()) / f"restore_{tenant_id}.zip"
                )

                await asyncio.to_thread(
                    self.s3_client.download,
                    s3_key,
                    zip_path,
                )

                tenant_dir = get_tenant_working_dir(tenant_id)
                await self._extract_zip(zip_path, tenant_dir, tenant_id)
                restored_tenants.append(tenant_id)

            task.restored_tenants = restored_tenants

            # Clean up rollback data after successful restore
            rollback_dir = WORKING_DIR / ".rollback" / task.task_id
            if rollback_dir.exists():
                try:
                    shutil.rmtree(rollback_dir)
                except Exception as e:
                    logger.warning(f"Failed to cleanup rollback dir: {e}")

            task.current_step = "completed"
            task.status = BackupTaskStatus.COMPLETED
            task.progress_percent = 100

        except Exception as e:
            task.error_message = str(e)
            task.current_step = "rolling_back"
            task.status = BackupTaskStatus.ROLLING_BACK
            self.task_store.save(task)

            # Rollback all tenants
            await self._rollback_all(rollback_paths)

            task.status = BackupTaskStatus.ROLLED_BACK
        finally:
            task.completed_at = datetime.now(BJ_TZ)
            self.task_store.save(task)

    def _get_all_tenant_ids(self) -> list[str]:
        """Get all tenant IDs from working directory."""
        return list_all_tenant_ids()

    async def _compress_tenant(
        self,
        tenant_id: str,
        tenant_dir: Path,
        zip_path: Path,
    ) -> str:
        """Compress tenant directory to zip including working dir, secrets, and providers."""

        def _do_compress():
            skipped_files = []
            with zipfile.ZipFile(
                zip_path,
                "w",
                zipfile.ZIP_DEFLATED,
                compresslevel=1,
            ) as zf:
                # 1. Compress tenant working directory
                _compress_directory(
                    zf,
                    tenant_dir,
                    tenant_dir,
                    skipped=skipped_files,
                )

                # 2. Compress tenant secrets (WORKING_DIR / tenant_id / ".secret")
                secrets_dir = get_tenant_secrets_dir(tenant_id)
                if secrets_dir.exists():
                    _compress_directory(
                        zf,
                        secrets_dir,
                        secrets_dir,
                        prefix=".secret/",
                        skipped=skipped_files,
                    )

                # 3. Compress provider configs (SECRET_DIR / tenant_id / providers)
                provider_dir = SECRET_DIR / tenant_id / "providers"
                if provider_dir.exists():
                    _compress_directory(
                        zf,
                        provider_dir,
                        provider_dir,
                        prefix=".providers/",
                        skipped=skipped_files,
                    )

            if skipped_files:
                logger.warning(
                    f"Skipped {len(skipped_files)} files due to encoding/access "
                    f"issues for tenant {tenant_id}",
                )

            return str(zip_path)

        return await asyncio.to_thread(_do_compress)

    async def _compress_single_tenant(
        self,
        tenant_id: str,
        tenant_dir: Path,
        zip_path: Path,
    ) -> tuple[str, str, float, float]:
        """Compress a single tenant with timing log.

        Returns:
            (tenant_id, zip_path_str, size_mb, elapsed_time)
        """
        start = time.time()
        await self._compress_tenant(tenant_id, tenant_dir, zip_path)
        elapsed = time.time() - start
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"Compressed tenant {tenant_id}: {size_mb:.2f}MB in {elapsed:.2f}s "
            f"({size_mb / elapsed:.2f}MB/s)",
        )
        return tenant_id, str(zip_path), size_mb, elapsed

    async def _upload_single_file(
        self,
        zip_path: Path,
        instance_id: str,
        date_str: str,
        hour: int,
    ) -> tuple[str, str, float]:
        """Upload a single file to S3 with timing log.

        Returns:
            (tenant_id, s3_key, elapsed_time)
        """
        tenant_id = zip_path.stem.replace("backup_", "")
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
            f"Uploaded tenant {tenant_id}: {zip_size_mb:.2f}MB in {elapsed:.2f}s "
            f"({zip_size_mb / elapsed:.2f}MB/s)",
        )
        return tenant_id, s3_key, elapsed

    async def _create_rollback_backup(
        self,
        task_id: str,
        tenant_id: str,
        tenant_dir: Path,
    ) -> str:
        """Create a backup of current data before restore."""
        rollback_dir = WORKING_DIR / ".rollback" / task_id
        rollback_dir.mkdir(parents=True, exist_ok=True)
        zip_path = rollback_dir / f"{tenant_id}.zip"

        def _do_compress():
            with zipfile.ZipFile(
                zip_path,
                "w",
                zipfile.ZIP_DEFLATED,
                compresslevel=1,
            ) as zf:
                _compress_directory(zf, tenant_dir, tenant_dir)
                secrets_dir = get_tenant_secrets_dir(tenant_id)
                if secrets_dir.exists():
                    _compress_directory(
                        zf,
                        secrets_dir,
                        secrets_dir,
                        prefix=".secret/",
                    )
                provider_dir = SECRET_DIR / tenant_id / "providers"
                if provider_dir.exists():
                    _compress_directory(
                        zf,
                        provider_dir,
                        provider_dir,
                        prefix=".providers/",
                    )
            return str(zip_path)

        await asyncio.to_thread(_do_compress)
        return str(zip_path)

    async def _extract_zip(
        self,
        zip_path: Path,
        target_dir: Path,
        tenant_id: str,
    ) -> None:
        """Extract zip to target, routing .secret/ and .providers/ to correct directories.

        Args:
            zip_path: Path to the zip file to extract.
            target_dir: Target directory for non-secret files.
            tenant_id: Tenant ID for determining secret and provider directories.

        Raises:
            ValueError: If path traversal is detected in zip entries.
        """

        def _do_extract():
            target_dir.mkdir(parents=True, exist_ok=True)
            secrets_dir = get_tenant_secrets_dir(tenant_id)
            secrets_dir.mkdir(parents=True, exist_ok=True)

            provider_dir = SECRET_DIR / tenant_id / "providers"
            provider_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    # Determine target directory based on prefix
                    if member.filename.startswith(".secret/"):
                        # Route to secret directory
                        relative_path = member.filename[
                            8:
                        ]  # Remove .secret/ prefix
                        if (
                            not relative_path
                        ):  # Skip if it's just .secret/ directory
                            continue
                        extract_dir = secrets_dir
                        dest_path = secrets_dir / relative_path
                    elif member.filename.startswith(".providers/"):
                        # Route to provider directory
                        relative_path = member.filename[
                            11:
                        ]  # Remove .providers/ prefix
                        if (
                            not relative_path
                        ):  # Skip if it's just .providers/ directory
                            continue
                        extract_dir = provider_dir
                        dest_path = provider_dir / relative_path
                    else:
                        # Route to target directory
                        relative_path = member.filename
                        extract_dir = target_dir
                        dest_path = target_dir / relative_path

                    # Validate path traversal
                    resolved_dest = dest_path.resolve()
                    resolved_extract = extract_dir.resolve()
                    if not str(resolved_dest).startswith(
                        str(resolved_extract),
                    ):
                        raise ValueError(
                            "Path traversal detected in zip entry: "
                            f"{member.filename}",
                        )

                    # Extract the file/directory
                    if member.is_dir():
                        dest_path.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as source, open(
                            dest_path,
                            "wb",
                        ) as target:
                            target.write(source.read())

        await asyncio.to_thread(_do_extract)

    async def _rollback_all(self, rollback_paths: list[str]) -> None:
        """Rollback all tenants to pre-restore state."""
        for rollback_path in rollback_paths:
            try:
                path = Path(rollback_path)
                if not path.exists():
                    continue
                tenant_id = path.stem
                tenant_dir = get_tenant_working_dir(tenant_id)

                # Remove current data
                if tenant_dir.exists():
                    shutil.rmtree(tenant_dir)

                # Restore from rollback
                await self._extract_zip(path, tenant_dir, tenant_id)
            except Exception as e:
                # Continue rollback for other tenants
                logger.error(f"Failed to rollback {rollback_path}: {e}")
