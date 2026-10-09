-- Apply to the shared SWE/Monitor/Scheduler database before enabling workflow jobs.
CREATE TABLE IF NOT EXISTS swe_workflow_bindings (
    binding_id VARCHAR(64) PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    skill_id VARCHAR(200) NOT NULL,
    current_version INT NOT NULL DEFAULT 0,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_workflow_source_skill (source_id, skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS swe_workflow_binding_versions (
    binding_id VARCHAR(64) NOT NULL,
    version INT NOT NULL,
    skill_id VARCHAR(200) NOT NULL,
    config_json TEXT NOT NULL,
    published_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (binding_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @workflow_binding_column_exists = (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'swe_cron_jobs'
      AND column_name = 'workflow_binding_id'
);
SET @workflow_binding_column_sql = IF(
    @workflow_binding_column_exists = 0,
    'ALTER TABLE swe_cron_jobs ADD COLUMN workflow_binding_id VARCHAR(64) DEFAULT NULL COMMENT ''Only workflow jobs carry this binding ID''',
    'SELECT 1'
);
PREPARE workflow_binding_column_stmt FROM @workflow_binding_column_sql;
EXECUTE workflow_binding_column_stmt;
DEALLOCATE PREPARE workflow_binding_column_stmt;
