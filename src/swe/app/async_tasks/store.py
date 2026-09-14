# -*- coding: utf-8 -*-
"""统一异步任务表的本地写入器。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ...database.connection import DatabaseConnection

TASK_TYPE_TITLE_MAP = {
    "cron.broadcast.distribute": "定时任务分发",
    "market.mcp.distribute": "MCP 分发",
    "market.skill.distribute": "技能分发",
    "provider.active_model.distribute": "模型分发",
    "provider.providers.distribute": "供应商分发",
    "tenant.bootstrap": "用户初始化",
}
TASK_TYPE_SUMMARY_TEMPLATE_MAP = {
    "cron.broadcast.distribute": "向 {target_count} 个用户分发定时任务",
    "market.mcp.distribute": "向 {target_count} 个用户分发 MCP",
    "market.skill.distribute": "向 {target_count} 个用户分发技能",
    "provider.active_model.distribute": (
        "向 {target_count} 个用户分发当前活跃模型"
    ),
    "provider.providers.distribute": "向 {target_count} 个用户分发供应商配置",
    "tenant.bootstrap": "批量初始化 {target_count} 个用户",
}


def _json_dumps(value: Any) -> str | None:
    """将结果对象序列化为数据库 JSON 字段。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _resolve_task_title(task_type: str, title: str | None = None) -> str:
    """按任务类型生成展示标题，未知类型保留调用方标题兜底。"""
    return TASK_TYPE_TITLE_MAP.get(task_type, title or task_type or "-")


def _resolve_task_summary(
    task_type: str,
    target_count: int,
    summary: str | None = None,
) -> str | None:
    """按任务类型生成默认摘要，显式摘要优先保留。"""
    if summary:
        return summary
    template = TASK_TYPE_SUMMARY_TEMPLATE_MAP.get(task_type)
    if template is None:
        return None
    return template.format(
        target_count=target_count,
    )


def _resolve_target_name(
    target_id: str,
    target_names: dict[str, str | None] | None,
) -> str | None:
    """从目标名称映射中读取可展示名称。"""
    if not target_names:
        return None
    name = target_names.get(target_id)
    if name is None:
        return None
    stripped_name = str(name).strip()
    return stripped_name or None


def _extract_target_name(result: Any) -> str | None:
    """从执行结果中提取可回填到明细表的目标名称。"""
    if not isinstance(result, dict):
        return None
    for key in ("target_name", "user_name", "tenant_name"):
        value = result.get(key)
        if value:
            stripped_value = str(value).strip()
            if stripped_value:
                return stripped_value
    return None


class AsyncTaskStore:
    """SWE 服务内的异步任务写入器。

    第一版采用层级一共享方式：SWE 直接写统一表，Monitor 只读查询。
    """

    def __init__(self, db: DatabaseConnection) -> None:
        """初始化写入器。

        Args:
            db: SWE 服务当前数据库连接
        """
        self.db = db

    async def start_task(
        self,
        *,
        task_id: str,
        batch_id: str | None = None,
        service: str,
        task_type: str,
        target_ids: list[str],
        target_names: dict[str, str | None] | None = None,
        title: str | None = None,
        summary: str | None = None,
        source_id: str | None = None,
        actor_user_id: str | None = None,
        actor_user_name: str | None = None,
        result: Any = None,
    ) -> bool:
        """创建主任务并批量写入目标明细。"""
        if isinstance(self.db, DatabaseConnection):
            async with self.db.transaction():
                return await self._start_task(
                    task_id=task_id,
                    batch_id=batch_id,
                    service=service,
                    task_type=task_type,
                    target_ids=target_ids,
                    target_names=target_names,
                    title=title,
                    summary=summary,
                    source_id=source_id,
                    actor_user_id=actor_user_id,
                    actor_user_name=actor_user_name,
                    result=result,
                )
        return await self._start_task(
            task_id=task_id,
            batch_id=batch_id,
            service=service,
            task_type=task_type,
            target_ids=target_ids,
            target_names=target_names,
            title=title,
            summary=summary,
            source_id=source_id,
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
            result=result,
        )

    async def _start_task(
        self,
        *,
        task_id: str,
        batch_id: str | None,
        service: str,
        task_type: str,
        target_ids: list[str],
        target_names: dict[str, str | None] | None,
        title: str | None,
        summary: str | None,
        source_id: str | None,
        actor_user_id: str | None,
        actor_user_name: str | None,
        result: Any,
    ) -> bool:
        """在当前事务中创建主任务及目标明细。"""
        insert_task_sql = """
            INSERT INTO swe_async_tasks (
                task_id, batch_id, service, task_type, status, title, summary,
                source_id, actor_user_id, actor_user_name,
                target_count, done_count, failed_count, result_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, %s)
            ON DUPLICATE KEY UPDATE task_id = task_id
        """
        affected_rows = await self.db.execute(
            insert_task_sql,
            (
                task_id,
                batch_id,
                service,
                task_type,
                "queued",
                _resolve_task_title(task_type, title),
                _resolve_task_summary(
                    task_type,
                    len(target_ids),
                    summary,
                ),
                source_id,
                actor_user_id,
                actor_user_name,
                len(target_ids),
                _json_dumps(result),
            ),
        )
        insert_items_sql = """
            INSERT IGNORE INTO swe_async_task_items (
                task_id, target_id, target_name, status, error_message,
                result_json
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        await self.db.execute_many(
            insert_items_sql,
            [
                (
                    task_id,
                    target_id,
                    _resolve_target_name(target_id, target_names),
                    "queued",
                    None,
                    None,
                )
                for target_id in target_ids
            ],
        )
        return affected_rows == 1

    async def mark_running(self, task_id: str) -> None:
        """将主任务标记为运行中。"""
        await self.db.execute(
            """
            UPDATE swe_async_tasks
            SET status = %s
            WHERE task_id = %s
            """,
            ("running", task_id),
        )

    async def mark_item_running(
        self,
        *,
        task_id: str,
        target_id: str,
    ) -> None:
        """将单个目标标记为运行中。"""
        await self.db.execute(
            """
            UPDATE swe_async_task_items
            SET status = %s
            WHERE task_id = %s AND target_id = %s
            """,
            ("running", task_id, target_id),
        )

    async def record_item_result(
        self,
        *,
        task_id: str,
        target_id: str,
        success: bool,
        item_status: str | None = None,
        result: Any = None,
        error_message: str | None = None,
    ) -> None:
        """记录单个目标的执行结果。"""
        status = item_status or ("succeeded" if success else "failed")
        target_name = _extract_target_name(result)
        await self.db.execute(
            """
            UPDATE swe_async_task_items
            SET status = %s,
                target_name = COALESCE(%s, target_name),
                result_json = %s,
                error_message = %s
            WHERE task_id = %s AND target_id = %s
            """,
            (
                status,
                target_name,
                _json_dumps(result),
                error_message,
                task_id,
                target_id,
            ),
        )
        await self._refresh_task_progress(task_id)

    async def _refresh_task_progress(self, task_id: str) -> None:
        """按明细状态刷新主任务进度，供列表页实时展示。"""
        await self.db.execute(
            """
            UPDATE swe_async_tasks
            SET done_count = (
                    SELECT COUNT(*)
                    FROM swe_async_task_items
                    WHERE task_id = %s
                      AND status IN ('succeeded', 'created', 'skipped', 'failed')
                ),
                failed_count = (
                    SELECT COUNT(*)
                    FROM swe_async_task_items
                    WHERE task_id = %s AND status = 'failed'
                )
            WHERE task_id = %s
            """,
            (task_id, task_id, task_id),
        )

    async def finish_task(
        self,
        *,
        task_id: str,
        status: str,
        done_count: int,
        failed_count: int,
        error_message: str | None = None,
        result: Any = None,
        finished_at: datetime | None = None,
    ) -> None:
        """汇总更新主任务最终状态。"""
        await self.db.execute(
            """
            UPDATE swe_async_tasks
            SET status = %s,
                done_count = %s,
                failed_count = %s,
                error_message = %s,
                result_json = %s,
                finished_at = %s
            WHERE task_id = %s
            """,
            (
                status,
                done_count,
                failed_count,
                error_message,
                _json_dumps(result),
                finished_at or datetime.now(),
                task_id,
            ),
        )

    async def fail_task(
        self,
        task_id: str,
        error_message: str,
    ) -> None:
        """在任务级异常时直接标记主任务失败。"""
        await self.finish_task(
            task_id=task_id,
            status="failed",
            done_count=0,
            failed_count=0,
            error_message=error_message,
        )
