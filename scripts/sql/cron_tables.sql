-- ============================================================
-- Cron Tables - 定时任务表定义
-- Date: 2026-05-18
-- Description: 创建定时任务相关表
-- ============================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- -----------------------------------------------------------
-- 表: swe_cron_jobs (定时任务定义表)
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_jobs` (
    `id` VARCHAR(64) PRIMARY KEY COMMENT '任务ID (UUID)',
    `name` VARCHAR(255) NOT NULL COMMENT '任务名称',
    `tenant_id` VARCHAR(64) NOT NULL COMMENT '租户ID (分行号)',
    `tenant_name` VARCHAR(255) DEFAULT '' COMMENT '租户姓名',
    `bbk_id` VARCHAR(64) DEFAULT '' COMMENT '分行号',
    `source_id` VARCHAR(64) DEFAULT '' COMMENT '来源标识',
    `enabled` TINYINT(1) DEFAULT 1 COMMENT '是否启用',
    `task_type` VARCHAR(16) NOT NULL COMMENT '任务类型: text/agent',
    `cron_expr` VARCHAR(64) NOT NULL COMMENT 'cron表达式 (5字段)',
    `timezone` VARCHAR(32) DEFAULT 'UTC' COMMENT '时区',
    `channel` VARCHAR(32) NOT NULL COMMENT '分发渠道',
    `target_user_id` VARCHAR(64) DEFAULT '' COMMENT '目标用户ID',
    `target_session_id` VARCHAR(64) DEFAULT '' COMMENT '目标会话ID',
    `timeout_seconds` INT DEFAULT 7200 COMMENT '超时秒数',
    `max_concurrency` INT DEFAULT 1 COMMENT '最大并发数',
    `misfire_grace_seconds` INT DEFAULT 300 COMMENT 'misfire容错秒数',
    `text_content` VARCHAR(4096) DEFAULT '' COMMENT 'text类型任务内容',
    `request_input` VARCHAR(4096) DEFAULT '' COMMENT 'agent类型请求输入',
    `creator_user_id` VARCHAR(64) DEFAULT '' COMMENT '创建者用户ID',
    `task_chat_id` VARCHAR(64) DEFAULT '' COMMENT '关联聊天ID',
    `task_session_id` VARCHAR(64) DEFAULT '' COMMENT '关联会话ID',
    `plan_id` VARCHAR(255) DEFAULT NULL COMMENT '关联计划ID',
    `meta` VARCHAR(4096) DEFAULT '' COMMENT '扩展元数据',
    `status` VARCHAR(16) DEFAULT 'active' COMMENT '状态: active/paused/deleted',
    `pause_reason` VARCHAR(32) DEFAULT '' COMMENT '暂停原因',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    `deleted_at` DATETIME DEFAULT NULL COMMENT '删除时间',
    INDEX `idx_tenant_id` (`tenant_id`),
    INDEX `idx_bbk_id` (`bbk_id`),
    INDEX `idx_source_id` (`source_id`),
    INDEX `idx_creator_user_id` (`creator_user_id`),
    INDEX `idx_status` (`status`),
    INDEX `idx_enabled` (`enabled`),
    INDEX `idx_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='定时任务定义表';

-- -----------------------------------------------------------
-- 表: swe_cron_executions (定时任务执行历史表)
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_executions` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '执行记录ID',
    `job_id` VARCHAR(64) NOT NULL COMMENT '任务ID',
    `job_name` VARCHAR(255) DEFAULT '' COMMENT '任务名称',
    `tenant_id` VARCHAR(64) NOT NULL COMMENT '租户ID',
    `scheduled_time` DATETIME DEFAULT NULL COMMENT '计划执行时间',
    `actual_time` DATETIME NOT NULL COMMENT '实际开始时间',
    `end_time` DATETIME DEFAULT NULL COMMENT '结束时间',
    `duration_ms` INT DEFAULT 0 COMMENT '执行耗时 (毫秒)',
    `status` VARCHAR(16) NOT NULL COMMENT '状态: success/error/cancelled/timeout/skipped',
    `async_status` VARCHAR(16) DEFAULT NULL COMMENT '异步任务执行状态: success/error',
    `error_message` VARCHAR(2048) DEFAULT '' COMMENT '错误信息',
    `instance_id` VARCHAR(64) DEFAULT '' COMMENT '执行实例标识',
    `executor_leader` VARCHAR(64) DEFAULT '' COMMENT '执行者 leader ID',
    `is_manual` TINYINT(1) DEFAULT 0 COMMENT '是否手动触发',
    `trace_id` VARCHAR(64) DEFAULT '' COMMENT '关联的 trace ID',
    `session_id` VARCHAR(64) DEFAULT '' COMMENT '关联的 session ID',
    `input_snapshot` VARCHAR(2048) DEFAULT '' COMMENT '执行时的输入快照',
    `output_preview` VARCHAR(512) DEFAULT '' COMMENT '输出预览 (前100字符)',
    `meta` VARCHAR(2048) DEFAULT '' COMMENT '执行元数据',
    `dispatch_intent_id` BIGINT DEFAULT NULL COMMENT '批调度派发意图ID',
    `dispatch_batch_id` VARCHAR(64) DEFAULT '' COMMENT '批调度批次ID',
    `dispatch_attempt` INT DEFAULT NULL COMMENT '批调度派发尝试次数',
    `notification_status` VARCHAR(16) DEFAULT 'not_required' COMMENT '通知状态',
    `notification_due_at` DATETIME DEFAULT NULL COMMENT '计划通知时间',
    `notification_timezone` VARCHAR(64) DEFAULT '' COMMENT '通知计算时区',
    `notification_sent_at` DATETIME DEFAULT NULL COMMENT '通知发送时间',
    `notification_attempts` INT DEFAULT 0 COMMENT '通知尝试次数',
    `notification_error` VARCHAR(2048) DEFAULT '' COMMENT '通知错误',
    `notification_lock_owner` VARCHAR(128) DEFAULT '' COMMENT '通知锁持有者',
    `notification_locked_at` DATETIME DEFAULT NULL COMMENT '通知锁时间',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `is_read` TINYINT(1) DEFAULT 0 COMMENT 'whether execution result was read',
    `read_at` DATETIME DEFAULT NULL COMMENT 'read time',
    INDEX `idx_job_id` (`job_id`),
    INDEX `idx_tenant_id` (`tenant_id`),
    INDEX `idx_status` (`status`),
    INDEX `idx_async_status` (`async_status`),
    INDEX `idx_scheduled_time` (`scheduled_time`),
    INDEX `idx_actual_time` (`actual_time`),
    INDEX `idx_trace_id` (`trace_id`),
    INDEX `idx_notification_scan` (`notification_status`, `notification_due_at`),
    INDEX `idx_notification_lock` (`notification_lock_owner`, `notification_locked_at`),
    INDEX `idx_execution_read` (`is_read`, `read_at`),
    INDEX `idx_cron_execution_dispatch` (
        `dispatch_intent_id`, `dispatch_batch_id`, `dispatch_attempt`
    ),
    INDEX `idx_tenant_actual` (`tenant_id`, `actual_time`),
    INDEX `idx_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='定时任务执行历史表';

-- -----------------------------------------------------------
-- Table: swe_cron_dispatch_batches
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_dispatch_batches` (
    `batch_id` VARCHAR(64) PRIMARY KEY COMMENT 'dispatch batch id',
    `parent_job_id` VARCHAR(64) NOT NULL COMMENT 'batch parent cron job id',
    `parent_external_job_id` VARCHAR(64) DEFAULT '' COMMENT 'external scheduler job id',
    `tenant_id` VARCHAR(64) NOT NULL COMMENT 'parent tenant id',
    `source_id` VARCHAR(64) DEFAULT '' COMMENT 'source id',
    `provider_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    `model_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    `agent_id` VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT 'agent id',
    `scheduled_fire_at` DATETIME NOT NULL COMMENT 'parent scheduled fire time',
    `callback_received_at` DATETIME NOT NULL COMMENT 'scheduler callback receive time',
    `status` VARCHAR(16) NOT NULL DEFAULT 'received' COMMENT 'received/pending/running/completed/failed',
    `lock_owner` VARCHAR(128) DEFAULT '' COMMENT '批次派发锁持有者',
    `locked_at` DATETIME DEFAULT NULL COMMENT '批次派发锁时间',
    `total_count` INT NOT NULL DEFAULT 0 COMMENT 'total intents',
    `completed_count` INT NOT NULL DEFAULT 0 COMMENT 'completed intents',
    `failed_count` INT NOT NULL DEFAULT 0 COMMENT 'failed intents',
    `callback_metadata` JSON DEFAULT NULL COMMENT 'raw callback metadata',
    `error_message` VARCHAR(2048) DEFAULT '' COMMENT 'batch error summary',
    `completed_at` DATETIME DEFAULT NULL COMMENT 'batch completed time',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'updated time',
    UNIQUE INDEX `uk_dispatch_batch_parent_fire` (`parent_job_id`, `scheduled_fire_at`),
    INDEX `idx_dispatch_batch_parent` (`parent_job_id`, `created_at`),
    INDEX `idx_dispatch_batch_source` (`source_id`, `scheduled_fire_at`),
    INDEX `idx_dispatch_batch_status` (`status`, `updated_at`),
    INDEX `idx_dispatch_batch_lock` (`lock_owner`, `locked_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron dispatch batch runs';

-- -----------------------------------------------------------
-- Table: swe_cron_dispatch_intents
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_dispatch_intents` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT 'dispatch intent id',
    `batch_id` VARCHAR(64) NOT NULL COMMENT 'dispatch batch id',
    `intent_role` VARCHAR(16) NOT NULL COMMENT 'parent/child',
    `status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/claimed/dispatched/completed/failed/cancelled',
    `source_id` VARCHAR(64) DEFAULT '' COMMENT 'source id',
    `provider_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    `model_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    `tenant_id` VARCHAR(64) NOT NULL COMMENT 'runtime tenant id',
    `agent_id` VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT 'agent id',
    `job_id` VARCHAR(64) NOT NULL COMMENT 'cron job id',
    `parent_job_id` VARCHAR(64) DEFAULT '' COMMENT 'parent broadcast job id',
    `scheduled_fire_at` DATETIME DEFAULT NULL COMMENT 'parent scheduled fire time',
    `due_at` DATETIME NOT NULL COMMENT 'earliest claim time',
    `dispatch_order` INT NOT NULL DEFAULT 0 COMMENT 'stable order inside batch',
    `viewer_heat_score` DECIMAL(12,4) NOT NULL DEFAULT 0 COMMENT 'bounded read heat score',
    `attempt_count` INT NOT NULL DEFAULT 0 COMMENT 'attempt count',
    `max_attempts` INT NOT NULL DEFAULT 3 COMMENT 'max attempts',
    `lock_owner` VARCHAR(128) DEFAULT '' COMMENT 'worker lock owner',
    `locked_at` DATETIME DEFAULT NULL COMMENT 'lock time',
    `acked_at` DATETIME DEFAULT NULL COMMENT 'worker acknowledged time',
    `completed_at` DATETIME DEFAULT NULL COMMENT 'completion time',
    `error_message` VARCHAR(2048) DEFAULT '' COMMENT 'last error',
    `payload` JSON DEFAULT NULL COMMENT 'intent payload',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'updated time',
    UNIQUE INDEX `uk_dispatch_batch_role_job` (`batch_id`, `intent_role`, `tenant_id`, `job_id`),
    INDEX `idx_dispatch_claim` (`status`, `due_at`, `dispatch_order`, `id`),
    INDEX `idx_dispatch_batch` (`batch_id`, `dispatch_order`),
    INDEX `idx_dispatch_lock` (`lock_owner`, `locked_at`),
    INDEX `idx_dispatch_job` (`job_id`),
    INDEX `idx_dispatch_source` (`source_id`),
    INDEX `idx_dispatch_scope_claim` (
        `source_id`,
        `provider_id`,
        `model_id`,
        `status`,
        `due_at`,
        `dispatch_order`,
        `id`
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron dispatch intent queue';

-- -----------------------------------------------------------
-- Table: swe_cron_dispatch_events
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_dispatch_events` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT 'dispatch event id',
    `batch_id` VARCHAR(64) NOT NULL COMMENT 'dispatch batch id',
    `intent_id` BIGINT DEFAULT NULL COMMENT 'dispatch intent id',
    `event_type` VARCHAR(64) NOT NULL COMMENT 'event type',
    `worker_id` VARCHAR(128) DEFAULT '' COMMENT 'worker id',
    `job_id` VARCHAR(64) DEFAULT '' COMMENT 'job id',
    `tenant_id` VARCHAR(64) DEFAULT '' COMMENT 'tenant id',
    `source_id` VARCHAR(64) DEFAULT '' COMMENT 'source id',
    `details` JSON DEFAULT NULL COMMENT 'event details',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    INDEX `idx_dispatch_events_batch` (`batch_id`, `created_at`),
    INDEX `idx_dispatch_events_intent` (`intent_id`),
    INDEX `idx_dispatch_events_type` (`event_type`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron dispatch telemetry events';

-- -----------------------------------------------------------
-- Table: swe_cron_dispatch_worker_capacity
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_dispatch_worker_capacity` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT 'capacity snapshot id',
    `worker_id` VARCHAR(128) NOT NULL COMMENT 'worker id',
    `source_id` VARCHAR(64) DEFAULT '' COMMENT 'source id',
    `provider_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    `model_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    `strategy_id` VARCHAR(64) DEFAULT '' COMMENT 'worker strategy id',
    `previous_workers` INT NOT NULL DEFAULT 0 COMMENT 'previous effective workers',
    `baseline_workers` INT NOT NULL DEFAULT 1 COMMENT 'baseline workers',
    `min_workers` INT NOT NULL DEFAULT 1 COMMENT 'minimum workers',
    `max_workers` INT NOT NULL DEFAULT 1 COMMENT 'max workers',
    `effective_workers` INT NOT NULL DEFAULT 1 COMMENT 'effective workers',
    `pending_count` INT NOT NULL DEFAULT 0 COMMENT 'pending intents',
    `claimed_count` INT NOT NULL DEFAULT 0 COMMENT 'claimed intents',
    `running_count` INT NOT NULL DEFAULT 0 COMMENT 'running intents',
    `success_count` INT NOT NULL DEFAULT 0 COMMENT 'recent success count',
    `failure_count` INT NOT NULL DEFAULT 0 COMMENT 'recent terminal failure count',
    `error_rate` DECIMAL(8,6) NOT NULL DEFAULT 0 COMMENT 'terminal failure rate',
    `matched_rule` JSON DEFAULT NULL COMMENT 'matched adjustment rule',
    `avg_latency_ms` INT NOT NULL DEFAULT 0 COMMENT 'recent average latency',
    `decision_reason` VARCHAR(255) DEFAULT '' COMMENT 'capacity decision reason',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    INDEX `idx_dispatch_capacity_worker` (`worker_id`, `created_at`),
    INDEX `idx_dispatch_capacity_source` (`source_id`, `created_at`),
    INDEX `idx_dispatch_capacity_scope` (`source_id`, `provider_id`, `model_id`, `strategy_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron dispatch worker capacity snapshots';

-- -----------------------------------------------------------
-- Table: swe_cron_dispatch_scope_leases
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_dispatch_scope_leases` (
    `source_id` VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'source id',
    `provider_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    `model_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    `lock_owner` VARCHAR(128) NOT NULL DEFAULT '' COMMENT 'scope lease owner',
    `locked_at` DATETIME DEFAULT NULL COMMENT 'scope lock time',
    `lease_expires_at` DATETIME DEFAULT NULL COMMENT 'scope lease expiry time',
    `heartbeat_at` DATETIME DEFAULT NULL COMMENT 'scope owner heartbeat time',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'updated time',
    PRIMARY KEY (`source_id`, `provider_id`, `model_id`),
    INDEX `idx_scope_lease_owner` (`lock_owner`, `lease_expires_at`),
    INDEX `idx_scope_lease_expiry` (`lease_expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Cron dispatch model scope leases';

-- -----------------------------------------------------------
-- Table: swe_cron_dispatch_model_worker_policy
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_dispatch_model_worker_policy` (
    `source_id` VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT 'source id',
    `provider_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    `model_id` VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    `default_strategy_id` VARCHAR(64) NOT NULL COMMENT 'default strategy id',
    `strategy_schedule` JSON DEFAULT NULL COMMENT 'time-window strategy schedule',
    `enabled` TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'enabled',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'updated time',
    PRIMARY KEY (`source_id`, `provider_id`, `model_id`),
    INDEX `idx_dispatch_worker_policy_strategy` (`default_strategy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Cron dispatch model worker policy';

-- -----------------------------------------------------------
-- Table: swe_cron_dispatch_worker_strategy
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `swe_cron_dispatch_worker_strategy` (
    `strategy_id` VARCHAR(64) PRIMARY KEY COMMENT 'strategy id',
    `min_workers` INT NOT NULL DEFAULT 1 COMMENT 'minimum workers',
    `baseline_workers` INT NOT NULL DEFAULT 1 COMMENT 'baseline workers',
    `max_workers` INT NOT NULL DEFAULT 1 COMMENT 'maximum workers',
    `adjust_interval_seconds` INT NOT NULL DEFAULT 300 COMMENT 'adjust interval',
    `feedback_window_seconds` INT NOT NULL DEFAULT 300 COMMENT 'feedback window',
    `stale_execution_seconds` INT NOT NULL DEFAULT 7800 COMMENT 'stale dispatch timeout',
    `error_rate_rules` JSON DEFAULT NULL COMMENT 'error-rate adjustment rules',
    `enabled` TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'enabled',
    `description` VARCHAR(255) DEFAULT '' COMMENT 'strategy description',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'updated time'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Cron dispatch worker strategy';

SET FOREIGN_KEY_CHECKS = 1;
