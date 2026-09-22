-- 市场技能软删除标识
-- 下架/删除不再物理删除 swe_marketplace_skills 记录。

DELIMITER $$

CREATE PROCEDURE migrate_marketplace_skill_soft_delete_flags()
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'swe_marketplace_skills'
          AND column_name = 'is_unpublished'
    ) THEN
        ALTER TABLE swe_marketplace_skills
            ADD COLUMN is_unpublished TINYINT(1) DEFAULT 0
            COMMENT '是否下架：1=已下架，0=未下架'
            AFTER include_in_statistics;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'swe_marketplace_skills'
          AND column_name = 'is_deleted'
    ) THEN
        ALTER TABLE swe_marketplace_skills
            ADD COLUMN is_deleted TINYINT(1) DEFAULT 0
            COMMENT '是否删除：1=已删除，0=未删除'
            AFTER is_unpublished;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 'swe_marketplace_skills'
          AND index_name = 'idx_dropdown_eligible'
    ) THEN
        CREATE INDEX idx_dropdown_eligible
            ON swe_marketplace_skills (
                source_id,
                include_in_statistics,
                is_unpublished,
                is_deleted
            );
    END IF;
END$$

DELIMITER ;

CALL migrate_marketplace_skill_soft_delete_flags();
DROP PROCEDURE migrate_marketplace_skill_soft_delete_flags;
