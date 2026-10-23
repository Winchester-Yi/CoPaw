# -*- coding: utf-8 -*-
"""回答反馈数据库存储。"""

import json
from datetime import datetime
from typing import Any, Optional

from .models import FeedbackCreate, FeedbackRecord


class FeedbackStore:
    """负责回答反馈的落库操作。"""

    def __init__(self, db: Optional[Any] = None):
        """初始化反馈存储。

        Args:
            db: 已连接的数据库对象
        """
        self.db = db
        self._use_db = db is not None and db.is_connected

    @staticmethod
    def _loads_feedback_options(raw: Any) -> list[str]:
        """解析数据库中的快捷反馈选项字段。"""
        if isinstance(raw, list):
            return [str(item) for item in raw]
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return []
            if isinstance(value, list):
                return [str(item) for item in value]
        return []

    @staticmethod
    def _to_feedback_record(row: dict[str, Any]) -> FeedbackRecord:
        """把数据库行转换为反馈记录模型。"""
        return FeedbackRecord(
            id=int(row["id"]),
            source_id=row.get("source_id"),
            feedback_user_name=row.get("feedback_user_name"),
            feedback_user_sap=row.get("feedback_user_sap"),
            feedback_branch=row.get("feedback_branch"),
            feedback_sub_branch=row.get("feedback_sub_branch"),
            feedback_position=row.get("feedback_position"),
            cron_task_name=row.get("cron_task_name"),
            cron_task_id=row.get("cron_task_id"),
            response_id=row.get("response_id"),
            trace_id=row.get("trace_id"),
            chat_id=row.get("chat_id"),
            session_id=row.get("session_id"),
            feedback_options=FeedbackStore._loads_feedback_options(
                row.get("feedback_options"),
            ),
            feedback_content=row.get("feedback_content") or "",
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    async def get_feedback(
        self,
        *,
        source_id: Optional[str],
        feedback_id: Optional[int] = None,
        response_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> FeedbackRecord | None:
        """按反馈 ID、回答 ID 或 trace ID 查询单条反馈。"""
        if not self._use_db:
            return None

        where_clauses: list[str] = []
        params: list[Any] = []

        if feedback_id is not None:
            where_clauses.append("id = %s")
            params.append(feedback_id)
        elif response_id:
            where_clauses.append("response_id = %s")
            params.append(response_id)
        elif trace_id:
            where_clauses.append("trace_id = %s")
            params.append(trace_id)
        else:
            return None

        if source_id:
            where_clauses.append("source_id <=> %s")
            params.append(source_id)

        query = (
            "SELECT * FROM swe_response_feedback "
            f"WHERE {' AND '.join(where_clauses)} "
            "ORDER BY id DESC LIMIT 1"
        )
        row = await self.db.fetch_one(query, tuple(params))
        if not row:
            return None
        return self._to_feedback_record(row)

    async def list_feedbacks_by_session(
        self,
        *,
        source_id: Optional[str],
        session_id: Optional[str],
        chat_id: Optional[str] = None,
    ) -> list[FeedbackRecord]:
        """按聊天 ID 或运行时会话 ID 查询全部反馈记录。"""
        if not self._use_db or (not chat_id and not session_id):
            return []

        identity_clauses: list[str] = []
        params: list[Any] = []
        if chat_id:
            identity_clauses.append("chat_id = %s")
            params.append(chat_id)
        if session_id:
            identity_clauses.append("session_id = %s")
            params.append(session_id)

        where_clauses: list[str] = [
            f"({' OR '.join(identity_clauses)})",
        ]

        if source_id:
            where_clauses.append("source_id <=> %s")
            params.append(source_id)

        query = (
            "SELECT * FROM swe_response_feedback "
            f"WHERE {' AND '.join(where_clauses)} "
            "ORDER BY updated_at DESC, id DESC"
        )
        rows = await self.db.fetch_all(query, tuple(params))
        return [self._to_feedback_record(row) for row in rows]

    async def resolve_trace_id(
        self,
        *,
        trace_id: Optional[str],
        session_id: Optional[str],
        source_id: Optional[str],
        response_id: Optional[str] = None,
    ) -> Optional[str]:
        """优先使用显式 trace_id，否则按现有 tracing 记录兜底匹配。"""
        if trace_id:
            return trace_id
        if not self._use_db or not session_id:
            return None

        query = """
            SELECT trace_id
            FROM swe_tracing_traces
            WHERE session_id = %s
              AND (%s IS NULL OR source_id <=> %s)
            ORDER BY start_time DESC
            LIMIT 1
        """
        row = await self.db.fetch_one(
            query,
            (session_id, source_id, source_id),
        )
        if not row:
            return None
        value = row.get("trace_id")
        return str(value) if value else None

    async def upsert_feedback(
        self,
        feedback: FeedbackCreate,
        *,
        source_id: Optional[str],
    ) -> tuple[int | None, bool, Optional[str]]:
        """创建或更新一条回答反馈。

        Args:
            feedback: 前端提交的反馈内容
            source_id: 当前来源标识

        Returns:
            (反馈 ID, 是否为更新, 最终保存的 trace_id)
        """
        if not self._use_db:
            return None, False, feedback.trace_id

        resolved_trace_id = await self.resolve_trace_id(
            trace_id=feedback.trace_id,
            session_id=feedback.session_id,
            source_id=source_id,
            response_id=feedback.response_id,
        )

        existing: FeedbackRecord | None = None
        if feedback.id is not None:
            existing = await self.get_feedback(
                source_id=source_id,
                feedback_id=feedback.id,
            )
        elif feedback.response_id:
            existing = await self.get_feedback(
                source_id=source_id,
                response_id=feedback.response_id,
            )
        elif feedback.trace_id:
            existing = await self.get_feedback(
                source_id=source_id,
                trace_id=feedback.trace_id,
            )

        feedback_options_json = json.dumps(
            feedback.feedback_options,
            ensure_ascii=False,
        )

        if existing is not None:
            query = """
                UPDATE swe_response_feedback
                SET feedback_user_name = %s,
                    feedback_user_sap = %s,
                    feedback_branch = %s,
                    feedback_sub_branch = %s,
                    feedback_position = %s,
                    cron_task_name = %s,
                    cron_task_id = %s,
                    response_id = %s,
                    trace_id = %s,
                    chat_id = %s,
                    session_id = %s,
                    feedback_options = %s,
                    feedback_content = %s,
                    updated_at = %s
                WHERE id = %s
            """
            await self.db.execute(
                query,
                (
                    feedback.feedback_user_name,
                    feedback.feedback_user_sap,
                    feedback.feedback_branch,
                    feedback.feedback_sub_branch,
                    feedback.feedback_position,
                    feedback.cron_task_name,
                    feedback.cron_task_id,
                    feedback.response_id,
                    resolved_trace_id,
                    feedback.chat_id,
                    feedback.session_id,
                    feedback_options_json,
                    feedback.feedback_content,
                    datetime.now(),
                    existing.id,
                ),
            )
            return existing.id, True, resolved_trace_id

        query = """
            INSERT INTO swe_response_feedback (
                source_id,
                feedback_user_name,
                feedback_user_sap,
                feedback_branch,
                feedback_sub_branch,
                feedback_position,
                cron_task_name,
                cron_task_id,
                response_id,
                trace_id,
                chat_id,
                session_id,
                feedback_options,
                feedback_content
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        inserted_id = await self.db.insert(
            query,
            (
                source_id,
                feedback.feedback_user_name,
                feedback.feedback_user_sap,
                feedback.feedback_branch,
                feedback.feedback_sub_branch,
                feedback.feedback_position,
                feedback.cron_task_name,
                feedback.cron_task_id,
                feedback.response_id,
                resolved_trace_id,
                feedback.chat_id,
                feedback.session_id,
                feedback_options_json,
                feedback.feedback_content,
            ),
        )
        return inserted_id or None, False, resolved_trace_id
