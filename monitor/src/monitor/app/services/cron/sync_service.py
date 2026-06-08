# -*- coding: utf-8 -*-
"""Sync service for cron job and execution data.

Provides methods to sync job definitions and execution records
from SWE to Monitor database.
"""

import logging
import os
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from ...database import get_db_connection
from ...models.cron import CronJobSyncRequest, ExecutionSyncRequest
from ....utils.bbk import get_bbk_id_by_name, get_bbk_name_by_id
from ....utils.scope_decode import (
    is_encoded_scope_id,
    try_decode_tenant_id,
)

logger = logging.getLogger(__name__)

# 东八区时区（北京时间）
_BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# 用户信息 API URL（与 SWE 服务共用同一个配置）
USER_INFO_API_URL = os.environ.get("MONITOR_USER_INFO_API_URL", "")

# 数据库字段长度限制（基于 swe_cron_executions 表结构）
# VARCHAR 字段需要截断以避免写入失败
# input_snapshot 已改为 TEXT 类型（最大 65535 字节）
# utf8mb4 编码每字符最多 4 字节，保守上限 16000 字符
INPUT_SNAPSHOT_MAX_LENGTH = 16000
ERROR_MESSAGE_MAX_LENGTH = 2048
OUTPUT_PREVIEW_MAX_LENGTH = 512
META_MAX_LENGTH = 2048


def _truncate_string(text: Optional[str], max_length: int) -> str:
    """截断字符串到指定最大长度。

    确保写入数据库不会因超长而报错。

    Args:
        text: 原始字符串（可能为 None）
        max_length: 最大长度限制

    Returns:
        截断后的字符串（不超过 max_length）
    """
    if text is None:
        return ""
    if len(text) <= max_length:
        return text
    # 截断并保留截断标记，便于识别数据被裁剪
    truncated = text[: max_length - 3] + "..."
    logger.warning(
        "String truncated from %d to %d chars for database field limit",
        len(text),
        max_length,
    )
    return truncated


def _get_beijing_now() -> datetime:
    """获取当前东八区时间（无 tzinfo），用于数据库存储。

    Returns:
        当前北京时间（无时区信息）
    """
    return datetime.now(_BEIJING_TZ).replace(tzinfo=None)


def _to_beijing_naive(value: Optional[datetime]) -> Optional[datetime]:
    """将有时区时间统一转为北京时区的无时区值。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(_BEIJING_TZ).replace(tzinfo=None)


def _extract_bbk_id_from_path_name(path_name: Optional[str]) -> Optional[str]:
    """从 pathName 中提取 BBK ID。

    pathName 格式如: "某企业/总行/生产部/某组"
    提取第一个和第二个"/"之间的内容，映射为 BBK ID。

    Args:
        path_name: 路径名称字符串

    Returns:
        BBK ID 或 None
    """
    if not path_name:
        return None

    parts = path_name.split("/")
    if len(parts) >= 2 and parts[1]:
        return get_bbk_id_by_name(parts[1])

    return None


async def _fetch_user_info(
    tenant_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    """调用用户信息 API 获取 userName 和 bbk_id。

    Args:
        tenant_id: 租户/用户 ID

    Returns:
        (user_name, bbk_id) 元组
    """
    if not USER_INFO_API_URL or not tenant_id:
        return None, None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                USER_INFO_API_URL,
                json={
                    "keyWord": tenant_id,
                    "compareType": "EQ",
                },
            )

        if not response.is_success:
            logger.warning(
                "User info API failed for tenant %s: %d",
                tenant_id,
                response.status_code,
            )
            return None, None

        data = response.json()
        outer_data = data.get("data")

        if outer_data is None:
            return None, None

        # 处理响应结构：可能是 {"data": [...]} 或 {"data": {"data": [...}}
        if isinstance(outer_data, list):
            result_data = outer_data
        elif isinstance(outer_data, dict):
            result_data = outer_data.get("data", [])
            if not isinstance(result_data, list):
                result_data = []
        else:
            result_data = []

        if not result_data:
            return None, None

        user_info = result_data[0]
        if not isinstance(user_info, dict):
            return None, None

        user_name = user_info.get("userName")
        path_name = user_info.get("pathName")
        bbk_id = _extract_bbk_id_from_path_name(path_name)

        return user_name, bbk_id

    except Exception as e:
        logger.warning(
            "Error fetching user info for tenant %s: %s",
            tenant_id,
            e,
        )
        return None, None


async def _enrich_sync_request(
    request: CronJobSyncRequest,
) -> CronJobSyncRequest:
    """补全同步请求中缺失的 tenant_name 和 bbk_id。

    如果 tenant_id 是加密格式，先解码为原始值，
    然后调用用户信息 API 补全缺失字段。
    即使任何步骤失败，也返回原始请求，确保数据不丢失。

    Args:
        request: 原始同步请求

    Returns:
        补全后的同步请求（失败时返回原始请求）
    """
    try:
        tenant_id_for_query = request.tenant_id

        # 1. 解码 tenant_id（如果是加密格式）
        if request.tenant_id and is_encoded_scope_id(request.tenant_id):
            decoded_tenant_id, _ = try_decode_tenant_id(
                request.tenant_id,
            )
            if decoded_tenant_id != request.tenant_id:
                # 更新请求对象
                request = request.model_copy(
                    update={"tenant_id": decoded_tenant_id},
                )
                tenant_id_for_query = decoded_tenant_id

        # 2. 补全 tenant_name 和标准 bbk_id（如果缺失或现有 bbk_id 无法映射）
        should_fetch_user_info = (
            not request.tenant_name
            or not request.bbk_id
            or not get_bbk_name_by_id(str(request.bbk_id).strip())
        )
        if should_fetch_user_info:
            user_name, bbk_id = await _fetch_user_info(tenant_id_for_query)

            update_fields = {}
            if user_name and not request.tenant_name:
                update_fields["tenant_name"] = user_name
            if bbk_id and (
                not request.bbk_id
                or not get_bbk_name_by_id(str(request.bbk_id).strip())
            ):
                update_fields["bbk_id"] = bbk_id

            if update_fields:
                request = request.model_copy(update=update_fields)

        return request

    except Exception as e:
        # 任何异常都返回原始请求，确保数据不丢失
        logger.warning("Failed to enrich sync request %s: %s", request.id, e)
        return request


class SyncService:
    """Service for syncing cron data from SWE."""

    def __init__(self) -> None:
        """Initialize sync service."""
        pass

    async def sync_job(self, request: CronJobSyncRequest) -> bool:
        """Sync or update a cron job definition.

        This method upserts a job definition into the database.
        If the job already exists, it updates all fields.
        If the job was previously soft-deleted, it restores it.

        Args:
            request: Sync request from SWE

        Returns:
            True if sync succeeded
        """
        # 补全缺失的 tenant_name 和 bbk_id（写入前处理）
        request = await _enrich_sync_request(request)

        db = get_db_connection()

        # Check if job exists and was deleted
        existing = await db.fetch_one(
            "SELECT id, deleted_at FROM swe_cron_jobs WHERE id = %s",
            (request.id,),
        )

        now = _get_beijing_now()

        if existing:
            # Update existing job, clear deleted_at if it was deleted
            deleted_at_value = (
                None
                if existing.get("deleted_at")
                else existing.get("deleted_at")
            )

            await db.execute(
                """
                UPDATE swe_cron_jobs SET
                    name = %s,
                    tenant_id = %s,
                    tenant_name = %s,
                    bbk_id = %s,
                    source_id = %s,
                    enabled = %s,
                    task_type = %s,
                    cron_expr = %s,
                    timezone = %s,
                    channel = %s,
                    target_user_id = %s,
                    target_session_id = %s,
                    timeout_seconds = %s,
                    max_concurrency = %s,
                    misfire_grace_seconds = %s,
                    text_content = %s,
                    request_input = %s,
                    creator_user_id = %s,
                    task_chat_id = %s,
                    task_session_id = %s,
                    job_origin = %s,
                    subscription_key = %s,
                    meta = %s,
                    status = %s,
                    pause_reason = %s,
                    updated_at = %s,
                    deleted_at = %s
                WHERE id = %s
                """,
                (
                    request.name,
                    request.tenant_id,
                    request.tenant_name,
                    request.bbk_id,
                    request.source_id,
                    request.enabled,
                    request.task_type,
                    request.cron_expr,
                    request.timezone,
                    request.channel,
                    request.target_user_id,
                    request.target_session_id,
                    request.timeout_seconds,
                    request.max_concurrency,
                    request.misfire_grace_seconds,
                    request.text_content,
                    request.request_input,
                    request.creator_user_id,
                    request.task_chat_id,
                    request.task_session_id,
                    request.job_origin,
                    request.subscription_key,
                    request.meta,
                    request.status,
                    request.pause_reason,
                    now,
                    deleted_at_value,
                    request.id,
                ),
            )
            logger.info(
                "Updated cron job: id=%s name=%s",
                request.id,
                request.name,
            )
        else:
            # Insert new job
            await db.execute(
                """
                INSERT INTO swe_cron_jobs (
                    id, name, tenant_id, tenant_name, bbk_id, source_id, enabled, task_type,
                    cron_expr, timezone, channel, target_user_id, target_session_id,
                    timeout_seconds, max_concurrency, misfire_grace_seconds,
                    text_content, request_input,
                    creator_user_id, task_chat_id, task_session_id,
                    job_origin, subscription_key,
                    meta,
                    status, pause_reason, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    request.id,
                    request.name,
                    request.tenant_id,
                    request.tenant_name,
                    request.bbk_id,
                    request.source_id,
                    request.enabled,
                    request.task_type,
                    request.cron_expr,
                    request.timezone,
                    request.channel,
                    request.target_user_id,
                    request.target_session_id,
                    request.timeout_seconds,
                    request.max_concurrency,
                    request.misfire_grace_seconds,
                    request.text_content,
                    request.request_input,
                    request.creator_user_id,
                    request.task_chat_id,
                    request.task_session_id,
                    request.job_origin,
                    request.subscription_key,
                    request.meta,
                    request.status,
                    request.pause_reason,
                    now,
                    now,
                ),
            )
            logger.info(
                "Inserted cron job: id=%s name=%s",
                request.id,
                request.name,
            )

        return True

    async def delete_job(self, job_id: str) -> bool:
        """Soft delete a cron job.

        Marks the job as deleted by setting deleted_at timestamp.
        The job remains in the database for historical reference.

        Args:
            job_id: Job ID to delete

        Returns:
            True if deletion succeeded, False if job not found
        """
        db = get_db_connection()

        # Check if job exists and is not already deleted
        existing = await db.fetch_one(
            "SELECT id, deleted_at FROM swe_cron_jobs WHERE id = %s",
            (job_id,),
        )

        if not existing:
            logger.warning("Job not found for deletion: id=%s", job_id)
            return False

        if existing.get("deleted_at"):
            logger.info("Job already deleted: id=%s", job_id)
            return True

        # Soft delete
        now = _get_beijing_now()
        await db.execute(
            """
            UPDATE swe_cron_jobs SET
                status = 'deleted',
                enabled = 0,
                deleted_at = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (now, now, job_id),
        )

        logger.info("Soft deleted cron job: id=%s", job_id)
        return True

    async def record_execution(
        self,
        request: ExecutionSyncRequest,
    ) -> Optional[int]:
        """Record an execution history entry.

        Args:
            request: Execution sync request from SWE

        Returns:
            Execution record ID if succeeded, None if failed
        """
        db = get_db_connection()

        now = _get_beijing_now()

        # 截断超长字段，确保写入数据库不会报错
        input_snapshot = _truncate_string(
            request.input_snapshot,
            INPUT_SNAPSHOT_MAX_LENGTH,
        )
        error_message = _truncate_string(
            request.error_message,
            ERROR_MESSAGE_MAX_LENGTH,
        )
        output_preview = _truncate_string(
            request.output_preview,
            OUTPUT_PREVIEW_MAX_LENGTH,
        )
        meta = _truncate_string(request.meta, META_MAX_LENGTH)

        await db.execute(
            """
            INSERT INTO swe_cron_executions (
                job_id, job_name, tenant_id,
                scheduled_time, actual_time, end_time, duration_ms,
                status, error_message,
                instance_id, executor_leader, is_manual,
                trace_id, session_id,
                input_snapshot, output_preview, meta,
                notification_status, notification_due_at, notification_timezone,
                is_read, read_at,
                created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                request.job_id,
                request.job_name,
                request.tenant_id,
                request.scheduled_time,
                request.actual_time,
                request.end_time,
                request.duration_ms,
                request.status,
                error_message,
                request.instance_id,
                request.executor_leader,
                request.is_manual,
                request.trace_id,
                request.session_id,
                input_snapshot,
                output_preview,
                meta,
                request.notification_status,
                _to_beijing_naive(request.notification_due_at),
                request.notification_timezone,
                request.is_read,
                request.read_at,
                now,
            ),
        )

        # Get the inserted ID
        result = await db.fetch_one("SELECT LAST_INSERT_ID() as id")
        execution_id = result.get("id") if result else None

        logger.info(
            "Recorded execution: job_id=%s execution_id=%s status=%s",
            request.job_id,
            execution_id,
            request.status,
        )

        return execution_id


# Global sync service instance
_sync_service: Optional[SyncService] = None


def get_sync_service() -> SyncService:
    """Get the sync service instance.

    Returns:
        SyncService instance
    """
    global _sync_service
    if _sync_service is None:
        _sync_service = SyncService()
    return _sync_service
