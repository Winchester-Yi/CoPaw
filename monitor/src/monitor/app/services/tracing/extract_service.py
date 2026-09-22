# -*- coding: utf-8 -*-
"""客户姓名提取服务.

从 swe_tracing_traces 的 user_message 和 ES 的 model_output 中提取客户姓名。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from ...database import get_db_connection, get_es_client
from ....config.constant import EXTRACT_CUSTOMER_NAMES_URL
from ..tracing.query_service import build_bbk_in_filter

logger = logging.getLogger(__name__)

# 外部 API 最大并发数
MAX_EXTRACT_CONCURRENCY = 5


class ExtractCustomerNamesService:
    """客户姓名提取服务."""

    _instance: Optional["ExtractCustomerNamesService"] = None

    def __init__(self):
        """初始化服务."""
        self._semaphore = asyncio.Semaphore(MAX_EXTRACT_CONCURRENCY)

    @classmethod
    def get_instance(cls) -> "ExtractCustomerNamesService":
        """获取单例实例."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def extract_names(
        self,
        skill_names: list[str],
        user_ids: Optional[list[str]] = None,
        bbk_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> dict:
        """提取客户姓名.

        Args:
            skill_names: 技能名称列表
            user_ids: 用户 ID 列表筛选
            bbk_id: 分行 ID 筛选
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            统计结果字典
        """
        db = get_db_connection()
        if not db.is_connected:
            raise RuntimeError("Database not connected")

        # 1. 查询待处理的 trace_id 列表（用 NOT EXISTS 直接跳过已有记录）
        traces_info = await self._query_traces_by_skill(
            db,
            skill_names,
            user_ids,
            bbk_id,
            start_date,
            end_date,
        )

        if not traces_info:
            logger.info("No traces found for skill_names: %s", skill_names)
            return {
                "total_traces": 0,
                "names_extracted": 0,
                "user_message_names": 0,
                "model_output_names": 0,
            }

        logger.info("Processing %d traces", len(traces_info))

        # 2. 批量查询 user_message
        trace_ids = [t["trace_id"] for t in traces_info]
        user_messages = await self._batch_query_user_messages(db, trace_ids)

        # 3. 批量查询 ES 获取 model_output
        model_outputs = await self._batch_query_model_outputs(trace_ids)

        # 4. 调用提取 API
        extract_results = await self._batch_extract_names(
            user_messages,
            model_outputs,
        )

        # 5. 写入数据库（仅保存有结果的记录）
        await self._save_extracted_names(db, traces_info, extract_results)

        # 统计结果
        total_user_names = sum(
            len(r.get("user_message_names", []))
            for r in extract_results.values()
        )
        total_model_names = sum(
            len(r.get("model_output_names", []))
            for r in extract_results.values()
        )

        return {
            "total_traces": len(traces_info),
            "names_extracted": total_user_names + total_model_names,
            "user_message_names": total_user_names,
            "model_output_names": total_model_names,
        }

    async def _query_traces_by_skill(
        self,
        db,
        skill_names: list[str],
        user_ids: Optional[list[str]],
        bbk_id: Optional[str],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> list[dict]:
        """按技能名称查询待处理的 trace_id 列表.

        从 swe_tracing_spans 表按技能名称查询记录，
        使用 NOT EXISTS 跳过已有提取记录的 trace。
        """
        # 构建技能名称 IN 条件
        skill_placeholders = ", ".join(["%s"] * len(skill_names))

        # 基础查询（嵌入 NOT EXISTS 跳过已处理记录）
        query = f"""
            SELECT DISTINCT
                s.trace_id,
                s.user_id,
                s.bbk_id,
                s.skill_name
            FROM swe_tracing_spans s
            WHERE s.skill_name IN ({skill_placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM swe_extracted_customer_names e
                  WHERE e.trace_id = s.trace_id AND e.skill_name = s.skill_name
              )
        """
        params: list = list(skill_names)

        # 添加用户 ID 筛选
        if user_ids:
            user_placeholders = ", ".join(["%s"] * len(user_ids))
            query += f" AND s.user_id IN ({user_placeholders})"
            params.extend(user_ids)

        # 添加分行 ID 筛选
        if bbk_id:
            bbk_filter, bbk_params = build_bbk_in_filter(bbk_id)
            if bbk_filter:
                query += (
                    f" AND s.bbk_id IN ({', '.join(['%s'] * len(bbk_params))})"
                )
                params.extend(bbk_params)

        # 添加日期筛选（使用 BETWEEN，两边界均包含）
        if start_date and end_date:
            query += " AND s.created_at BETWEEN %s AND %s"
            params.extend([start_date, end_date])
        elif start_date:
            query += " AND s.created_at >= %s"
            params.append(start_date)
        elif end_date:
            query += " AND s.created_at <= %s"
            params.append(end_date)

        rows = await db.fetch_all(query, tuple(params))
        return rows

    async def _batch_query_user_messages(
        self,
        db,
        trace_ids: list[str],
    ) -> dict[str, str]:
        """批量查询 user_message."""
        if not trace_ids:
            return {}

        placeholders = ", ".join(["%s"] * len(trace_ids))
        query = f"""
            SELECT trace_id, user_message
            FROM swe_tracing_traces
            WHERE trace_id IN ({placeholders})
        """
        rows = await db.fetch_all(query, tuple(trace_ids))

        return {r["trace_id"]: r["user_message"] or "" for r in rows}

    async def _batch_query_model_outputs(
        self,
        trace_ids: list[str],
    ) -> dict[str, str]:
        """批量查询 ES 获取 model_output."""
        if not trace_ids:
            return {}

        es_client = get_es_client()
        if es_client is None or not es_client.is_connected:
            logger.warning(
                "ES not connected, skipping model_output extraction",
            )
            return {}

        model_outputs = {}
        for trace_id in trace_ids:
            try:
                output = await es_client.get_message(trace_id)
                model_outputs[trace_id] = output or ""
            except Exception as e:
                logger.warning(
                    "Failed to get model_output for trace %s: %s",
                    trace_id,
                    e,
                )
                model_outputs[trace_id] = ""

        return model_outputs

    async def _batch_extract_names(
        self,
        user_messages: dict[str, str],
        model_outputs: dict[str, str],
    ) -> dict[str, dict]:
        """批量调用提取 API 提取姓名."""
        if not EXTRACT_CUSTOMER_NAMES_URL:
            logger.warning("EXTRACT_CUSTOMER_NAMES_URL not configured")
            return {}

        # 收集所有需要处理的 trace_id
        all_trace_ids = set(user_messages.keys()) | set(model_outputs.keys())

        results = {}

        async def extract_for_trace(trace_id: str):
            user_msg = user_messages.get(trace_id, "")
            model_out = model_outputs.get(trace_id, "")

            user_names = []
            model_names = []

            # 提取 user_message 中的姓名
            if user_msg:
                user_names = await self._call_extract_api(user_msg)

            # 提取 model_output 中的姓名
            if model_out:
                model_names = await self._call_extract_api(model_out)

            return trace_id, {
                "user_message_names": user_names,
                "model_output_names": model_names,
            }

        # 并发调用，使用信号量限制并发数
        tasks = [extract_for_trace(tid) for tid in all_trace_ids]

        async with self._semaphore:
            results_list = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results_list:
            if isinstance(result, tuple):
                trace_id, names = result
                results[trace_id] = names
            else:
                logger.warning("Extract task failed: %s", result)

        return results

    async def _call_extract_api(self, text: str) -> list[str]:
        """调用外部 API 提取姓名.

        Args:
            text: 待提取的文本

        Returns:
            提取的姓名列表
        """
        if not text or not EXTRACT_CUSTOMER_NAMES_URL:
            return []

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    EXTRACT_CUSTOMER_NAMES_URL,
                    json={"text": text},
                    headers={"Content-Type": "application/json"},
                )

                if not response.is_success:
                    logger.warning(
                        "Extract API failed: status=%d",
                        response.status_code,
                    )
                    return []

                data = response.json()
                names = data.get("names", [])
                return names if isinstance(names, list) else []

        except Exception as e:
            logger.warning("Extract API call failed: %s", e)
            return []

    async def _save_extracted_names(
        self,
        db,
        traces_info: list[dict],
        extract_results: dict[str, dict],
    ) -> None:
        """保存提取结果到数据库（仅保存有结果的记录）."""
        if not extract_results:
            return

        # 准备插入数据（使用别名方式替代已弃用的 VALUES() 函数）
        insert_query = """
            INSERT INTO swe_extracted_customer_names
                (trace_id, skill_name, user_message_names, model_output_names, user_id, bbk_id)
            VALUES (%s, %s, %s, %s, %s, %s) AS new
            ON DUPLICATE KEY UPDATE
                user_message_names = new.user_message_names,
                model_output_names = new.model_output_names,
                user_id = new.user_id,
                bbk_id = new.bbk_id,
                updated_at = CURRENT_TIMESTAMP
        """

        params_list = []
        skipped_count = 0
        for t in traces_info:
            trace_id = t["trace_id"]
            skill_name = t["skill_name"]
            user_id = t.get("user_id") or ""
            bbk_id = t.get("bbk_id") or ""

            result = extract_results.get(trace_id, {})
            user_names = result.get("user_message_names", [])
            model_names = result.get("model_output_names", [])

            # 空结果不写入数据库
            if not user_names and not model_names:
                skipped_count += 1
                continue

            params_list.append(
                (
                    trace_id,
                    skill_name,
                    json.dumps(user_names, ensure_ascii=False),
                    json.dumps(model_names, ensure_ascii=False),
                    user_id,
                    bbk_id,
                ),
            )

        if params_list:
            await db.execute_many(insert_query, params_list)
            logger.info(
                "Saved %d records with names (skipped %d empty)",
                len(params_list),
                skipped_count,
            )
