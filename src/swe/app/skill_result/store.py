# -*- coding: utf-8 -*-
"""技能执行结果数据库存储。"""

import json
from typing import Any, Optional

from .models import SkillResultCreate


class SkillResultStore:
    """负责技能执行结果的落库操作。"""

    def __init__(self, db: Optional[Any] = None):
        """初始化存储。

        Args:
            db: 已连接的数据库对象
        """
        self.db = db
        self._use_db = db is not None and db.is_connected

    @staticmethod
    def _dumps_json(value: Any) -> Optional[str]:
        """把任意值序列化为 JSON 字符串，None 保持为 NULL。"""
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    async def create(
        self,
        payload: SkillResultCreate,
        *,
        source_id: Optional[str] = None,
    ) -> tuple[Optional[int], Optional[str]]:
        """保存一条技能执行结果。

        Args:
            payload: 请求体
            source_id: 当前来源标识

        Returns:
            (记录 ID, trace_id)
        """
        if not self._use_db:
            return None, payload.trace_id

        query = """
            INSERT INTO swe_skill_result (
                source_id, trace_id, skill_id, user_id, bbk,
                cust_list, metadata, result_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        inserted_id = await self.db.insert(
            query,
            (
                source_id,
                payload.trace_id,
                payload.skill_id,
                payload.user_id,
                payload.bbk,
                self._dumps_json(payload.cust_list),
                self._dumps_json(payload.metadata),
                payload.result_id,
            ),
        )
        return inserted_id or None, payload.trace_id
