-- ============================================================
-- 应用市场相关表
-- 包含：
--   1. swe_user_item_operation_logs    用户技能/MCP操作日志
--   2. swe_marketplace_operation_logs  市场操作日志（发布/下架/分发）
--   3. swe_marketplace_categories      市场技能分类配置
-- ============================================================

CREATE TABLE IF NOT EXISTS swe_user_item_operation_logs (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_id    VARCHAR(64)  NOT NULL COMMENT '应用入口标识',
    user_id      VARCHAR(128) NOT NULL COMMENT '操作用户ID',
    user_name    VARCHAR(256)          COMMENT '操作用户名称',
    bbk_id       VARCHAR(64)           COMMENT '分行ID',
    operation    VARCHAR(32)  NOT NULL COMMENT '操作类型：create/edit/delete',
    item_type    VARCHAR(16)  NOT NULL COMMENT '条目类型：skill/mcp',
    item_name    VARCHAR(256) NOT NULL COMMENT '条目名称',
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
    INDEX idx_source_id (source_id),
    INDEX idx_user_id (user_id),
    INDEX idx_item_type (item_type),
    INDEX idx_bbk_id (bbk_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户技能/MCP操作日志';

CREATE TABLE IF NOT EXISTS swe_marketplace_operation_logs (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_id        VARCHAR(64)  NOT NULL COMMENT '应用入口标识',
    operator_id      VARCHAR(64)  NOT NULL COMMENT '操作人用户ID',
    operator_name    VARCHAR(256)          COMMENT '操作人用户名称',
    operation        VARCHAR(32)  NOT NULL COMMENT '操作类型：publish/unpublish/distribute',
    item_type        VARCHAR(16)  NOT NULL COMMENT '条目类型：skill/mcp',
    item_id          VARCHAR(64)  NOT NULL COMMENT '市场条目ID',
    item_name        VARCHAR(256)          COMMENT '市场条目名称',
    target_user_id   VARCHAR(64)           COMMENT '分发目标用户ID',
    target_user_name VARCHAR(256)          COMMENT '分发目标用户名称',
    target_bbk_id    VARCHAR(64)           COMMENT '分发目标用户所属机构ID（快照）',
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
    INDEX idx_source_id (source_id),
    INDEX idx_item_id (item_id),
    INDEX idx_target_user_id (target_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='市场操作日志';

CREATE TABLE IF NOT EXISTS swe_marketplace_categories (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_id   VARCHAR(64)  NOT NULL COMMENT '应用入口标识',
    name        VARCHAR(128) NOT NULL COMMENT '分类名称',
    sort_order  INT          NOT NULL DEFAULT 0 COMMENT '排序权重，升序',
    branch_visible TINYINT(1) NOT NULL DEFAULT 1 COMMENT '分行是否可见',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    UNIQUE KEY uk_source_name (source_id, name),
    INDEX idx_source_id (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='市场技能分类配置';
