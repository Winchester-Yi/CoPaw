# -*- coding: utf-8 -*-
"""系统控制面测试执行真实 SQL；并发锁的生产证明另用独立 MySQL。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import re
import sqlite3


class SystemDatabase:
    """仅转换 MySQL DDL 方言，不模拟系统任务仓库的查询结果。"""

    is_connected = True

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.statements = []
        path = (
            Path(__file__).resolve().parents[2]
            / "scripts/sql/cron_system_tasks.sql"
        )
        sql = path.read_text(encoding="utf-8")
        sql = re.sub(r"COMMENT\s+'[^']*'", "", sql)
        sql = re.sub(r"CHARACTER SET\s+\w+|COLLATE\s+\w+", "", sql)
        sql = re.sub(r"(?m)^    KEY [^\n]*\n", "", sql)
        sql = sql.replace("UNIQUE KEY uk_system_identity", "UNIQUE")
        sql = re.sub(r"\) ENGINE=[^;]+;", ");", sql)
        sql = re.sub(r",\s*\)", ")", sql)
        self.conn.executescript(sql)

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.rollback()
                raise
            self.conn.commit()

    def _run(self, query, params):
        self.statements.append(query)
        values = tuple(
            value.isoformat(" ") if isinstance(value, datetime) else value
            for value in (params or ())
        )
        query = query.replace(" FOR UPDATE", "").replace("%s", "?")
        query = query.replace(
            " ON DUPLICATE KEY UPDATE job_id=job_id",
            " ON CONFLICT DO NOTHING",
        )
        return self.conn.execute(query, values)

    async def execute(self, query, params=None):
        return self._run(query, params).rowcount

    async def fetch_all(self, query, params=None):
        return [dict(row) for row in self._run(query, params).fetchall()]

    async def fetch_one(self, query, params=None):
        row = self._run(query, params).fetchone()
        return dict(row) if row is not None else None
