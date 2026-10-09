-- 为已有部署增加批次查询能力。
ALTER TABLE swe_async_tasks
    ADD COLUMN batch_id VARCHAR(128) DEFAULT NULL COMMENT '批次ID',
    ADD INDEX idx_async_tasks_batch_id (batch_id);
