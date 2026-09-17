# -*- coding: utf-8 -*-
"""Service for high-frequency question analysis APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any, Optional

from fastapi import HTTPException
import httpx

from ....config.constant import (
    HFQ_RESULT_POLL_INTERVAL_SECONDS,
    HFQ_RESULT_WAIT_SECONDS,
    HFQ_WORKFLOW_API_KEY,
    HFQ_WORKFLOW_OPEN_ID,
    HFQ_WORKFLOW_RESPONSE_MODE,
    HFQ_WORKFLOW_TIMEOUT_SECONDS,
    HFQ_WORKFLOW_URL,
)
from ...database import DatabaseConnection, get_db_connection
from ...models.high_frequency_question import (
    HighFrequencyQuestionCriteriaRequest,
    HighFrequencyQuestionMessageListResponse,
    HighFrequencyQuestionMessageQueryRequest,
    HighFrequencyQuestionMessageResponse,
    HighFrequencyQuestionPrewarmRequest,
    HighFrequencyQuestionResultSaveRequest,
    HighFrequencyQuestionResultSaveResponse,
    HighFrequencyQuestionResultQueryResponse,
    HighFrequencyQuestionScheduledTaskRequest,
    HighFrequencyQuestionTaskSubmitRequest,
    HighFrequencyQuestionTaskSubmitResponse,
    HighFrequencyQuestionTopic,
    MAX_MESSAGE_ROWS,
)

logger = logging.getLogger(__name__)

MEANINGLESS_USER_MESSAGES = (
    "好",
    "好的",
    "收到",
    "继续",
    "谢谢",
    "谢谢你",
    "感谢",
    "嗯",
    "嗯嗯",
    "ok",
    "你好",
    "OK",
)


TASK_TYPE = "monitor.high.freq.question"
TASK_SERVICE = "monitor"
TASK_TITLE = "用户高频问题分析"
STALE_RESULT_MESSAGE = "最近一次更新失败，当前展示历史结果"
SYSTEM_ACTOR_ID = "SYSTEM"
SYSTEM_ACTOR_NAME = "系统定时任务"
MAX_ERROR_MESSAGE_LENGTH = 512
SKILL_GAP_COVERAGE_THRESHOLD_NUMERATOR = 1
SKILL_GAP_COVERAGE_THRESHOLD_DENOMINATOR = 2


@dataclass(frozen=True)
class _NormalizedCriteria:
    source_id: str
    start_time: datetime
    end_time: datetime
    start_date: date
    end_date: date
    scope_type: str
    bbk_id: str

    @property
    def result_json_request(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "scope_type": self.scope_type,
            "bbk_id": self.bbk_id,
        }


@dataclass(frozen=True)
class _ResultBatch:
    batch_id: str
    stat_start_time: datetime
    stat_end_time: datetime
    result_updated_at: datetime
    result_count: int


class HighFrequencyQuestionService:
    """High-frequency question analysis service."""

    def __init__(self, db: Optional[DatabaseConnection] = None) -> None:
        self._db = db or get_db_connection()

    @classmethod
    def get_instance(cls) -> "HighFrequencyQuestionService":
        return cls(get_db_connection())

    async def query_messages(
        self,
        request: HighFrequencyQuestionMessageQueryRequest,
    ) -> HighFrequencyQuestionMessageListResponse:
        """Query cleaned source user messages from tracing traces."""
        started = time.perf_counter()
        where_sql, params = self._build_message_where_clause(request)
        limit = MAX_MESSAGE_ROWS + 1

        query = f"""
            SELECT
                user_id,
                bbk_id,
                user_message,
                skills_used
            FROM swe_tracing_traces
            WHERE {where_sql}
            ORDER BY start_time ASC, trace_id ASC
            LIMIT %s
        """
        rows = await self._db.fetch_all(query, (*params, limit))

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "High-frequency question messages queried: "
            "source_id=%s start_time=%s end_time=%s bbk_id=%s count=%d elapsed_ms=%d",
            request.source_id,
            request.start_time,
            request.end_time,
            request.bbk_id,
            min(len(rows), MAX_MESSAGE_ROWS),
            elapsed_ms,
        )

        if len(rows) > MAX_MESSAGE_ROWS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "message query exceeds 10000 rows; narrow the time range "
                    "or bbk_id"
                ),
            )

        message_count = len(rows)
        return HighFrequencyQuestionMessageListResponse(
            total=message_count,
            message_count=message_count,
            user_count=self._count_distinct_users(rows),
            data=[self._row_to_message(row) for row in rows],
        )

    async def save_results(
        self,
        request: HighFrequencyQuestionResultSaveRequest,
    ) -> HighFrequencyQuestionResultSaveResponse:
        """Save a full high-frequency question result batch transactionally."""
        started = time.perf_counter()
        params_list = self._build_insert_params(request)

        async with self._db.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        DELETE FROM swe_high_frequency_question_result
                        WHERE source_id = %s AND batch_id = %s
                        """,
                        (request.source_id, request.batch_id),
                    )
                    if params_list:
                        await cur.executemany(
                            """
                            INSERT INTO swe_high_frequency_question_result (
                                source_id,
                                batch_id,
                                stat_start_time,
                                stat_end_time,
                                scope_type,
                                bbk_id,
                                rank_no,
                                topic_name,
                                message_count,
                                valid_message_count,
                                user_count,
                                total_skill_used_count,
                                skill_used_count,
                                top_skill,
                                bbk_dis,
                                sample_questions
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                            )
                            """,
                            params_list,
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "High-frequency question results saved: "
            "source_id=%s batch_id=%s saved_count=%d elapsed_ms=%d",
            request.source_id,
            request.batch_id,
            len(params_list),
            elapsed_ms,
        )
        return HighFrequencyQuestionResultSaveResponse(
            batch_id=request.batch_id,
            saved_count=len(params_list),
        )

    async def submit_task(
        self,
        request: HighFrequencyQuestionTaskSubmitRequest,
        *,
        actor_user_id: str | None = None,
        actor_user_name: str | None = None,
    ) -> HighFrequencyQuestionTaskSubmitResponse:
        """Submit an analysis task or reuse a recent successful result."""
        criteria = self._normalize_criteria(request)
        if not request.force:
            recent = await self._find_result_batch(
                criteria,
                max_age=timedelta(hours=24),
            )
            if recent is not None:
                result = await self._build_result_response(
                    criteria,
                    recent,
                    state="AVAILABLE",
                )
                return HighFrequencyQuestionTaskSubmitResponse(
                    **result.model_dump(),
                )

        task_id = str(uuid.uuid4())
        await self._create_async_task(
            task_id=task_id,
            criteria=criteria,
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
        )
        asyncio.create_task(
            self._run_workflow_and_finish_task(
                task_id=task_id,
                criteria=criteria,
            ),
        )
        return HighFrequencyQuestionTaskSubmitResponse(
            state="RUNNING",
            task_id=task_id,
            batch_id=task_id,
            status="running",
            source_id=criteria.source_id,
            stat_start_time=criteria.start_time,
            stat_end_time=criteria.end_time,
            scope_type=criteria.scope_type,
            bbk_id=criteria.bbk_id,
            result_updated_at=None,
            topics=[],
        )

    async def submit_prewarm(
        self,
        request: HighFrequencyQuestionPrewarmRequest,
    ) -> HighFrequencyQuestionTaskSubmitResponse:
        """Submit scheduler-driven prewarm through the normal task flow."""
        end_time = request.end_time
        start_time = request.start_time
        if start_time is None or end_time is None:
            end_time = datetime.now().replace(microsecond=0)
            start_time = end_time - timedelta(days=7)

        return await self.submit_task(
            HighFrequencyQuestionTaskSubmitRequest(
                source_id=request.source_id,
                start_time=start_time,
                end_time=end_time,
                bbk_id=request.bbk_id,
            ),
            actor_user_id=SYSTEM_ACTOR_ID,
            actor_user_name=SYSTEM_ACTOR_NAME,
        )

    async def submit_scheduled_task(
        self,
        request: HighFrequencyQuestionScheduledTaskRequest,
    ) -> HighFrequencyQuestionTaskSubmitResponse:
        """Submit a body-only scheduler task for the latest seven calendar days."""
        today = datetime.now().date()
        start_time = datetime.combine(today - timedelta(days=6), datetime_time.min)
        end_time = datetime.combine(today, datetime_time.max).replace(microsecond=0)
        return await self.submit_task(
            HighFrequencyQuestionTaskSubmitRequest(
                source_id=request.source_id,
                start_time=start_time,
                end_time=end_time,
                bbk_id=request.bbk_id,
                force=True,
            ),
            actor_user_id=SYSTEM_ACTOR_ID,
            actor_user_name=SYSTEM_ACTOR_NAME,
        )

    async def query_results(
        self,
        request: HighFrequencyQuestionCriteriaRequest,
    ) -> HighFrequencyQuestionResultQueryResponse:
        """Query recent or stale successful results for frontend display."""
        criteria = self._normalize_criteria(request)
        recent = await self._find_result_batch(
            criteria,
            max_age=timedelta(hours=24),
        )
        if recent is not None:
            return await self._build_result_response(
                criteria,
                recent,
                state="AVAILABLE",
            )

        stale = await self._find_result_batch(criteria, max_age=None)
        if stale is not None:
            return await self._build_result_response(
                criteria,
                stale,
                state="AVAILABLE_STALE",
                message=STALE_RESULT_MESSAGE,
            )

        return HighFrequencyQuestionResultQueryResponse(
            state="EMPTY",
            source_id=criteria.source_id,
            scope_type=criteria.scope_type,
            bbk_id=criteria.bbk_id,
            topics=[],
        )

    def _build_message_where_clause(
        self,
        request: HighFrequencyQuestionMessageQueryRequest,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses = [
            "source_id = %s",
            "start_time >= %s",
            "start_time < %s",
            "status = 'completed'",
            "session_id NOT LIKE %s",
            "user_message IS NOT NULL",
            "TRIM(user_message) != ''",
        ]
        params: list[Any] = [
            request.source_id,
            request.start_time,
            request.end_time,
            "cron-task%",
        ]

        placeholders = ", ".join(["%s"] * len(MEANINGLESS_USER_MESSAGES))
        clauses.append(f"TRIM(user_message) NOT IN ({placeholders})")
        params.extend(MEANINGLESS_USER_MESSAGES)

        if request.bbk_id:
            clauses.append("bbk_id = %s")
            params.append(request.bbk_id)

        return " AND ".join(clauses), tuple(params)

    def _row_to_message(
        self,
        row: dict[str, Any],
    ) -> HighFrequencyQuestionMessageResponse:
        return HighFrequencyQuestionMessageResponse(
            user_id=row.get("user_id"),
            bbk_id=row.get("bbk_id"),
            content=str(row.get("user_message") or ""),
            skills_used=self._parse_string_list(row.get("skills_used")),
        )

    def _count_distinct_users(self, rows: list[dict[str, Any]]) -> int:
        return len(
            {
                str(row.get("user_id")).strip()
                for row in rows
                if row.get("user_id") is not None
                and str(row.get("user_id")).strip()
            },
        )

    def _build_insert_params(
        self,
        request: HighFrequencyQuestionResultSaveRequest,
    ) -> list[tuple[Any, ...]]:
        params_list: list[tuple[Any, ...]] = []
        for result in request.results:
            params_list.append(
                (
                    request.source_id,
                    request.batch_id,
                    request.stat_start_time,
                    request.stat_end_time,
                    result.scope_type,
                    result.bbk_id,
                    result.rank_no,
                    result.topic_name,
                    result.message_count,
                    result.valid_message_count,
                    result.user_count,
                    result.total_skill_used_count,
                    result.skill_used_count,
                    result.top_skill,
                    json.dumps(result.bbk_dis, ensure_ascii=False),
                    json.dumps(result.sample_questions, ensure_ascii=False),
                ),
            )
        return params_list

    def _normalize_criteria(
        self,
        request: HighFrequencyQuestionCriteriaRequest,
    ) -> _NormalizedCriteria:
        bbk_id = (request.bbk_id or "").strip()
        if bbk_id:
            scope_type = "ORG"
            normalized_bbk_id = bbk_id
        else:
            scope_type = "ALL"
            normalized_bbk_id = "ALL"
        return _NormalizedCriteria(
            source_id=request.source_id,
            start_time=request.start_time,
            end_time=request.end_time,
            start_date=request.start_time.date(),
            end_date=request.end_time.date(),
            scope_type=scope_type,
            bbk_id=normalized_bbk_id,
        )

    def _day_bounds(self, value: date) -> tuple[datetime, datetime]:
        start = datetime.combine(value, datetime_time.min)
        return start, start + timedelta(days=1)

    async def _find_result_batch(
        self,
        criteria: _NormalizedCriteria,
        *,
        max_age: timedelta | None,
    ) -> _ResultBatch | None:
        start_day_start, start_day_end = self._day_bounds(criteria.start_date)
        end_day_start, end_day_end = self._day_bounds(criteria.end_date)
        params: list[Any] = [
            criteria.source_id,
            criteria.scope_type,
            criteria.bbk_id,
            start_day_start,
            start_day_end,
            end_day_start,
            end_day_end,
        ]
        having_sql = ""
        if max_age is not None:
            having_sql = "HAVING MAX(created_at) >= %s"
            params.append(datetime.now() - max_age)

        row = await self._db.fetch_one(
            f"""
            SELECT
                batch_id,
                MIN(stat_start_time) AS stat_start_time,
                MIN(stat_end_time) AS stat_end_time,
                MAX(created_at) AS result_updated_at,
                COUNT(*) AS result_count
            FROM swe_high_frequency_question_result
            WHERE source_id = %s
              AND scope_type = %s
              AND bbk_id = %s
              AND stat_start_time >= %s
              AND stat_start_time < %s
              AND stat_end_time >= %s
              AND stat_end_time < %s
            GROUP BY batch_id
            {having_sql}
            ORDER BY result_updated_at DESC
            LIMIT 1
            """,
            tuple(params),
        )
        if row is None:
            return None
        return _ResultBatch(
            batch_id=str(row["batch_id"]),
            stat_start_time=row["stat_start_time"],
            stat_end_time=row["stat_end_time"],
            result_updated_at=row["result_updated_at"],
            result_count=int(row.get("result_count") or 0),
        )

    async def _build_result_response(
        self,
        criteria: _NormalizedCriteria,
        batch: _ResultBatch,
        *,
        state: str,
        message: str | None = None,
    ) -> HighFrequencyQuestionResultQueryResponse:
        rows = await self._db.fetch_all(
            """
            SELECT rank_no, topic_name, message_count, valid_message_count,
                   user_count, total_skill_used_count, skill_used_count,
                   top_skill, bbk_dis, sample_questions
            FROM swe_high_frequency_question_result
            WHERE source_id = %s
              AND batch_id = %s
              AND scope_type = %s
              AND bbk_id = %s
            ORDER BY rank_no ASC
            """,
            (
                criteria.source_id,
                batch.batch_id,
                criteria.scope_type,
                criteria.bbk_id,
            ),
        )
        topics = [self._row_to_topic(row) for row in rows]
        summary = self._build_result_summary(rows, topics)
        return HighFrequencyQuestionResultQueryResponse(
            state=state,
            task_id=batch.batch_id,
            batch_id=batch.batch_id,
            status="succeeded",
            source_id=criteria.source_id,
            stat_start_time=batch.stat_start_time,
            stat_end_time=batch.stat_end_time,
            scope_type=criteria.scope_type,
            bbk_id=criteria.bbk_id,
            result_updated_at=batch.result_updated_at,
            topics=topics,
            message=message,
            **summary,
        )

    def _row_to_topic(self, row: dict[str, Any]) -> HighFrequencyQuestionTopic:
        return HighFrequencyQuestionTopic(
            rank_no=int(row.get("rank_no") or 0),
            topic_name=str(row.get("topic_name") or ""),
            message_count=int(row.get("message_count") or 0),
            valid_message_count=int(row.get("valid_message_count") or 0),
            skill_used_count=int(row.get("skill_used_count") or 0),
            top_skill=self._parse_optional_text(row.get("top_skill")),
            bbk_dis=self._parse_bbk_distribution(row.get("bbk_dis")),
            sample_questions=self._parse_sample_questions(
                row.get("sample_questions"),
            ),
        )

    def _build_result_summary(
        self,
        rows: list[dict[str, Any]],
        topics: list[HighFrequencyQuestionTopic],
    ) -> dict[str, int]:
        first_row = rows[0] if rows else {}
        return {
            "message_count": int(first_row.get("valid_message_count") or 0),
            "user_count": int(first_row.get("user_count") or 0),
            "total_skill_used_count": int(
                first_row.get("total_skill_used_count") or 0,
            ),
            "topic_count": len(topics),
            "skill_gap_topic_count": sum(
                1
                for topic in topics
                if self._has_skill_coverage_gap(topic)
            ),
        }

    def _has_skill_coverage_gap(
        self,
        topic: HighFrequencyQuestionTopic,
    ) -> bool:
        if topic.message_count <= 0:
            return False
        return (
            topic.skill_used_count * SKILL_GAP_COVERAGE_THRESHOLD_DENOMINATOR
            < topic.message_count * SKILL_GAP_COVERAGE_THRESHOLD_NUMERATOR
        )

    def _parse_optional_text(self, value: Any) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    def _parse_bbk_distribution(self, value: Any) -> dict[str, Any]:
        if value is None or value == "":
            return {}
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): item for key, item in value.items()}

    def _parse_sample_questions(self, value: Any) -> list[str]:
        return self._parse_string_list(value)

    def _parse_string_list(self, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item is not None]

    async def _create_async_task(
        self,
        *,
        task_id: str,
        criteria: _NormalizedCriteria,
        actor_user_id: str | None,
        actor_user_name: str | None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO swe_async_tasks (
                task_id, service, task_type, status, title, summary,
                source_id, actor_user_id, actor_user_name,
                target_count, done_count, failed_count, error_message,
                result_json, finished_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                task_id,
                TASK_SERVICE,
                TASK_TYPE,
                "running",
                TASK_TITLE,
                self._build_task_summary(criteria),
                criteria.source_id,
                actor_user_id or "",
                actor_user_name or "",
                1,
                0,
                0,
                None,
                json.dumps(
                    {"request": criteria.result_json_request},
                    ensure_ascii=False,
                ),
                None,
            ),
        )

    def _build_task_summary(self, criteria: _NormalizedCriteria) -> str:
        scope_text = "全部机构" if criteria.scope_type == "ALL" else criteria.bbk_id
        return (
            f"{criteria.start_date.isoformat()} 至 "
            f"{criteria.end_date.isoformat()}，{scope_text}"
        )

    async def _run_workflow_and_finish_task(
        self,
        *,
        task_id: str,
        criteria: _NormalizedCriteria,
    ) -> None:
        error_message: str | None = None
        try:
            await self._call_workflow(task_id=task_id, criteria=criteria)
            result_count = await self._count_result_rows(
                source_id=criteria.source_id,
                batch_id=task_id,
            )
            if result_count <= 0:
                raise RuntimeError("workflow returned success but no result rows")
            await self._mark_task_succeeded(
                task_id=task_id,
                criteria=criteria,
                result_count=result_count,
            )
            return
        except Exception as exc:  # pylint: disable=broad-except
            error_message = self._safe_error_message(exc)
            logger.warning(
                "High-frequency question workflow failed or ambiguous: "
                "task_id=%s source_id=%s error=%s",
                task_id,
                criteria.source_id,
                error_message,
            )

        result_count = await self._wait_for_result_rows(
            source_id=criteria.source_id,
            batch_id=task_id,
        )
        if result_count > 0:
            await self._mark_task_succeeded(
                task_id=task_id,
                criteria=criteria,
                result_count=result_count,
            )
        else:
            await self._mark_task_failed(
                task_id=task_id,
                criteria=criteria,
                error_message=error_message or "workflow failed",
            )

    async def _call_workflow(
        self,
        *,
        task_id: str,
        criteria: _NormalizedCriteria,
    ) -> None:
        if not HFQ_WORKFLOW_URL or not HFQ_WORKFLOW_API_KEY:
            raise RuntimeError("high-frequency question workflow is not configured")
        payload = {
            "inputParams": {
                "source_id": criteria.source_id,
                "batch_id": task_id,
                "start_time": criteria.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": criteria.end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "bbk_id": "" if criteria.scope_type == "ALL" else criteria.bbk_id,
            },
            "openId": HFQ_WORKFLOW_OPEN_ID,
            "responseMode": HFQ_WORKFLOW_RESPONSE_MODE or "noStreaming",
        }
        async with httpx.AsyncClient(timeout=HFQ_WORKFLOW_TIMEOUT_SECONDS) as client:
            response = await client.post(
                HFQ_WORKFLOW_URL,
                headers={
                    "API-Key": HFQ_WORKFLOW_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or body.get("message") != "success":
            raise RuntimeError("workflow returned unexpected response")

    async def _count_result_rows(self, *, source_id: str, batch_id: str) -> int:
        row = await self._db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM swe_high_frequency_question_result
            WHERE source_id = %s AND batch_id = %s
            """,
            (source_id, batch_id),
        )
        return int(row.get("count", 0)) if row else 0

    async def _wait_for_result_rows(self, *, source_id: str, batch_id: str) -> int:
        wait_seconds = max(float(HFQ_RESULT_WAIT_SECONDS), 0.0)
        interval_seconds = max(float(HFQ_RESULT_POLL_INTERVAL_SECONDS), 1.0)
        deadline = time.monotonic() + wait_seconds

        logger.info(
            "Waiting for high-frequency question result rows: "
            "task_id=%s source_id=%s wait_seconds=%s interval_seconds=%s",
            batch_id,
            source_id,
            wait_seconds,
            interval_seconds,
        )

        while True:
            result_count = await self._count_result_rows(
                source_id=source_id,
                batch_id=batch_id,
            )
            if result_count > 0 or time.monotonic() >= deadline:
                return result_count

            remaining_seconds = max(deadline - time.monotonic(), 0.0)
            await asyncio.sleep(min(interval_seconds, remaining_seconds))

    async def _mark_task_succeeded(
        self,
        *,
        task_id: str,
        criteria: _NormalizedCriteria,
        result_count: int,
    ) -> None:
        await self._db.execute(
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
                "succeeded",
                1,
                0,
                None,
                json.dumps(
                    {
                        "request": criteria.result_json_request,
                        "result": {
                            "batch_id": task_id,
                            "result_count": result_count,
                        },
                    },
                    ensure_ascii=False,
                ),
                datetime.now(),
                task_id,
            ),
        )

    async def _mark_task_failed(
        self,
        *,
        task_id: str,
        criteria: _NormalizedCriteria,
        error_message: str,
    ) -> None:
        await self._db.execute(
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
                "failed",
                0,
                1,
                error_message,
                json.dumps(
                    {
                        "request": criteria.result_json_request,
                        "result": {
                            "batch_id": task_id,
                            "result_count": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                datetime.now(),
                task_id,
            ),
        )

    def _safe_error_message(self, exc: Exception) -> str:
        message = str(exc) or exc.__class__.__name__
        message = " ".join(message.split())
        if len(message) > MAX_ERROR_MESSAGE_LENGTH:
            return f"{message[:MAX_ERROR_MESSAGE_LENGTH]}..."
        return message
