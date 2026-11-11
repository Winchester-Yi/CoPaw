-- Apply manually before deploying batch pause support. No job state changes.
ALTER TABLE swe_cron_managed_trigger_receipts
    ADD COLUMN batch_paused TINYINT(1) NOT NULL DEFAULT 0
    COMMENT '仅暂停当前已受理批次';
