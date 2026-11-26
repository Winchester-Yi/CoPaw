-- 在 Scheduler 和 Monitor 共库先手工升级；存量父任务默认未关注。
ALTER TABLE swe_cron_job_states
  ADD COLUMN batch_follow_level TINYINT NOT NULL DEFAULT 0 COMMENT '父任务关注等级：0未关注，1-5越高越优先',
  ADD COLUMN batch_follow_updated_at DATETIME(6) NULL COMMENT '等级最近切换时间，UTC',
  ADD COLUMN batch_follow_updated_by VARCHAR(128) NULL COMMENT '最近切换等级的操作人';
