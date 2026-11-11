-- 定时任务回检人工建表材料。
-- 在 SWE 和 Scheduler 共用的 MySQL 数据库中执行。
-- 执行前先检查已存在的表及索引定义，不要删除重建已有表。
-- 所有时间字段使用 UTC 和微秒精度。

CREATE TABLE IF NOT EXISTS swe_cron_recheck_tasks (
  recheck_id VARCHAR(64) NOT NULL
    COMMENT '回检任务标识',
  parent_execution_id VARCHAR(64) NOT NULL
    COMMENT '触发回检的主执行标识',
  tenant_id VARCHAR(128) NOT NULL
    COMMENT '任务所属租户标识',
  source_id VARCHAR(128) NOT NULL
    COMMENT '任务所属来源系统标识',
  agent_id VARCHAR(128) NOT NULL
    COMMENT '任务所属智能体标识',
  job_id VARCHAR(64) NOT NULL
    COMMENT '原定时任务标识',
  actor_id VARCHAR(128) NOT NULL
    COMMENT '触发主执行的真实操作人',
  enqueued_state_version BIGINT NOT NULL
    COMMENT '主执行受理时的任务状态水位',
  payload_json LONGTEXT NOT NULL
    COMMENT '回检指令、会话绑定及主执行结果快照',
  run_at DATETIME(6) NOT NULL
    COMMENT '计划执行时间，UTC',
  next_dispatch_at DATETIME(6) NOT NULL
    COMMENT '下次允许派发时间，UTC',
  created_at DATETIME(6) NOT NULL
    COMMENT '创建时间，UTC',
  updated_at DATETIME(6) NOT NULL
    COMMENT '更新时间，UTC',
  started_at DATETIME(6) DEFAULT NULL
    COMMENT '实际开始时间，UTC',
  status VARCHAR(16) NOT NULL
    COMMENT '回检状态：queued排队、dispatching派发中、accepted已受理、running执行中',
  version BIGINT NOT NULL DEFAULT 1
    COMMENT '并发控制版本',
  dispatch_attempt INT NOT NULL DEFAULT 0
    COMMENT '派发尝试次数',
  dispatch_owner VARCHAR(64) NOT NULL DEFAULT ''
    COMMENT '当前派发租约持有者',
  dispatch_lease_until DATETIME(6) DEFAULT NULL
    COMMENT '派发租约到期时间，UTC',
  run_owner VARCHAR(64) NOT NULL DEFAULT ''
    COMMENT '当前执行租约持有者',
  run_lease_until DATETIME(6) DEFAULT NULL
    COMMENT '执行租约到期时间，UTC',

  PRIMARY KEY (recheck_id)
    COMMENT '回检任务主键',

  UNIQUE KEY uk_cron_recheck_parent_execution (parent_execution_id)
    COMMENT '同一主执行只生成一条回检',

  KEY idx_cron_recheck_due (
    status,
    next_dispatch_at,
    recheck_id
  ) COMMENT '按状态和到期时间领取待派发回检',

  KEY idx_cron_recheck_dispatch_lease (
    status,
    dispatch_lease_until,
    recheck_id
  ) COMMENT '恢复过期的派发租约',

  KEY idx_cron_recheck_run_lease (
    status,
    run_lease_until,
    recheck_id
  ) COMMENT '恢复过期的执行租约',

  KEY idx_cron_recheck_job (
    tenant_id,
    source_id,
    agent_id,
    job_id,
    status
  ) COMMENT '按原定时任务筛选回检'
)
ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_bin
COMMENT='一次性回检活动记录，终态写入事件回执后删除';


-- 回检当前任务和历史记录查询索引。
-- 先检查该索引是否已存在，然后单独执行一次。
ALTER TABLE swe_cron_managed_dispatch_events
  ADD KEY idx_managed_event_recheck_history (
    tenant_id,
    source_id,
    agent_id,
    event_type,
    occurred_at,
    event_order
  ) COMMENT '回检历史记录分页查询';