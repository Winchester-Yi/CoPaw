# -*- coding: utf-8 -*-
"""HTML 预览事件表迁移契约测试。"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    REPO_ROOT / "scripts/sql/html_preview_event_dimensions_migration.sql"
)
BASELINE_PATH = REPO_ROOT / "scripts/sql/html_preview_click_events.sql"
SOURCE_MIGRATION_PATH = (
    REPO_ROOT / "scripts/sql/html_preview_source_dimensions_migration.sql"
)


def test_event_migration_uses_standard_mysql_alter_syntax():
    """迁移不能使用标准 MySQL 不支持的 ADD ... IF NOT EXISTS。"""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS" not in sql
    assert "ADD INDEX IF NOT EXISTS" not in sql
    assert "ALGORITHM=INPLACE" in sql
    assert "LOCK=NONE" in sql


def test_event_target_comments_only_describe_modules():
    """子方案由模板字段关联，event_target 只描述具体模块。"""
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    baseline_sql = BASELINE_PATH.read_text(encoding="utf-8")

    assert "子方案或模块" not in migration_sql
    assert "子方案或模块" not in baseline_sql
    assert "模块的稳定标识" in migration_sql
    assert "模块的稳定标识" in baseline_sql


def test_event_schema_uses_template_type_without_root_template_fields():
    """事件仅保存当前模板，并用 template_type 区分主子模板。"""
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    baseline_sql = BASELINE_PATH.read_text(encoding="utf-8")
    normalized_migration = " ".join(migration_sql.split())
    normalized_baseline = " ".join(baseline_sql.split())

    assert "template_type VARCHAR(16)" in normalized_migration
    assert "template_type VARCHAR(16)" in normalized_baseline
    assert "root_template_id" not in normalized_migration
    assert "root_result_id" not in normalized_migration
    assert "root_template_id" not in normalized_baseline
    assert "root_result_id" not in normalized_baseline


def test_event_schema_documents_three_event_types():
    """数据库事件类型说明应只包含点击、页面查看和模块曝光。"""
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    baseline_sql = BASELINE_PATH.read_text(encoding="utf-8")

    for sql in (migration_sql, baseline_sql):
        assert "button_click/preview_view/module_exposure" in sql
        assert "main_preview_view" not in sql
        assert "sub_preview_view" not in sql


def test_source_dimension_migration_adds_nullable_varchar_50_columns():
    """页面来源和平台来源应以可在线扩展的 nullable VARCHAR(50) 增加。"""
    migration_sql = SOURCE_MIGRATION_PATH.read_text(encoding="utf-8")
    baseline_sql = BASELINE_PATH.read_text(encoding="utf-8")
    normalized_migration = " ".join(migration_sql.split())
    normalized_baseline = " ".join(baseline_sql.split())

    for sql in (normalized_migration, normalized_baseline):
        assert "page_source VARCHAR(50) NULL" in sql
        assert "platform_source VARCHAR(50) NULL" in sql

    assert "ALGORITHM=INPLACE" in migration_sql
    assert "LOCK=NONE" in migration_sql
