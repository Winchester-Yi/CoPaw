# -*- coding: utf-8 -*-
"""验证独立系统表的只读契约与人工建表材料。"""

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scheduler.app.services.cron.system.schema import (
    REQUIRED_COLUMNS,
    SystemSchemaUnavailable,
    assert_system_schema_ready,
)


class SchemaDatabase:
    """仅模拟缺表响应，记录校验器是否发出业务写入。"""

    def __init__(self, missing=None):
        self.missing = missing
        self.reads = []
        self.execute = AsyncMock()

    async def fetch_all(self, query):
        self.reads.append(query)
        if self.missing and f"`{self.missing}`" in query:
            raise RuntimeError("missing schema")
        return []


@pytest.mark.asyncio
async def test_system_schema_check_only_reads_required_columns():
    db = SchemaDatabase()
    await assert_system_schema_ready(db)

    assert set(REQUIRED_COLUMNS) == {
        "swe_cron_system_jobs",
        "swe_cron_system_executions",
    }
    assert len(db.reads) == 2
    assert all(
        query.startswith("SELECT ") and query.endswith(" LIMIT 0")
        for query in db.reads
    )
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table",
    ["swe_cron_system_jobs", "swe_cron_system_executions"],
)
async def test_system_schema_missing_table_is_unavailable_without_repair(
    table,
):
    db = SchemaDatabase(table)
    with pytest.raises(SystemSchemaUnavailable) as error:
        await assert_system_schema_ready(db)

    assert str(error.value) == "cron_system_schema_unavailable"
    assert error.value.table == table
    db.execute.assert_not_awaited()


def test_system_manual_sql_covers_contract_with_chinese_comments():
    sql_path = (
        Path(__file__).resolve().parents[3]
        / "scripts/sql/cron_system_tasks.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")
    for table, fields in REQUIRED_COLUMNS.items():
        body = sql.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split(
            ") ENGINE=",
            1,
        )[0]
        columns = set(
            re.findall(
                r"^    ([a-z_]+)\s+(?:VARCHAR|CHAR|BIGINT|INT|TINYINT|DATETIME|LONGTEXT)",
                body,
                re.MULTILINE,
            ),
        )
        assert set(fields.split()) <= columns
        for line in body.splitlines():
            if re.match(
                r"    [a-z_]+\s+(?:VARCHAR|CHAR|BIGINT|INT|TINYINT|DATETIME|LONGTEXT)",
                line,
            ):
                assert "COMMENT '" in line and re.search(
                    r"[\u4e00-\u9fff]",
                    line,
                )
    assert "COMMENT='系统定时任务" in sql
    assert "UNIQUE KEY uk_system_identity" in sql
    assert "tenant_id VARCHAR(128)" in sql
    assert "NOT NULL DEFAULT ''" in sql
    assert not re.search(r"\b(DROP|ALTER|TRUNCATE)\b", sql, re.IGNORECASE)
