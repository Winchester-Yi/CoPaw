-- 应用市场列表/详情查询迁移：
-- TDSQL 保存市场元数据，NAS 保存技能文件和 MCP 配置内容。

CREATE TABLE IF NOT EXISTS swe_marketplace_mcps (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    item_id VARCHAR(64) NOT NULL,
    client_key VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    chinese_name VARCHAR(256) DEFAULT '',
    description TEXT,
    guidance TEXT,
    version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
    creator_id VARCHAR(64) DEFAULT '',
    creator_name VARCHAR(256) DEFAULT '',
    category_id BIGINT NULL,
    bbk_ids JSON NULL,
    content_path VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    updator_id VARCHAR(64) DEFAULT '',
    updator_name VARCHAR(256) DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_source_item (source_id, item_id),
    UNIQUE KEY uk_source_client_key (source_id, client_key),
    INDEX idx_source_status (source_id, status),
    INDEX idx_source_category (source_id, category_id),
    INDEX idx_source_name (source_id, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='应用市场 MCP 元数据';

ALTER TABLE swe_marketplace_skills
    ADD COLUMN IF NOT EXISTS description TEXT NULL,
    ADD COLUMN IF NOT EXISTS version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS category_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS bbk_ids JSON NULL,
    ADD COLUMN IF NOT EXISTS content_path VARCHAR(512) NULL;
