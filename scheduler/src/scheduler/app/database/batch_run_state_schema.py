"""Only the additive schema changes required by independent batch controls."""

CREATE_CONTROL_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_controls (
    source_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    parent_job_id VARCHAR(64) NOT NULL,
    paused TINYINT(1) NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    resumed_at DATETIME(6) DEFAULT NULL,
    updated_by VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, tenant_id, parent_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

RUN_STATE_ALTERS = [
    "ALTER TABLE swe_cron_dispatch_intents "
    "ADD COLUMN claim_token VARCHAR(36) NOT NULL DEFAULT ''",
    "ALTER TABLE swe_cron_dispatch_batches "
    "ADD COLUMN skipped_count INT NOT NULL DEFAULT 0",
]


async def apply_schema(db) -> None:
    await db.execute(CREATE_CONTROL_TABLE)
    for statement in RUN_STATE_ALTERS:
        try:
            await db.execute(statement)
        except Exception as exc:
            if not exc.args or exc.args[0] != 1060:
                raise
