ALTER TABLE swe_marketplace_categories
    ADD COLUMN IF NOT EXISTS branch_visible TINYINT(1) NOT NULL DEFAULT 1
    COMMENT '分行是否可见';
