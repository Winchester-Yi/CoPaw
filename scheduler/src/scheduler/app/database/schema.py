# -*- coding: utf-8 -*-
"""Scheduler-owned database schema bootstrap."""

from __future__ import annotations

import logging

from .connection import get_db_connection
from .batch_run_state_schema import CREATE_CONTROL_TABLE, RUN_STATE_ALTERS

logger = logging.getLogger(__name__)

CREATE_CRON_EXECUTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_executions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT 'execution record id',
    job_id VARCHAR(64) NOT NULL COMMENT 'cron job id',
    job_name VARCHAR(255) DEFAULT '' COMMENT 'job name',
    tenant_id VARCHAR(64) NOT NULL COMMENT 'tenant id',
    scheduled_time DATETIME DEFAULT NULL COMMENT 'scheduled time',
    actual_time DATETIME NOT NULL COMMENT 'actual start time',
    end_time DATETIME DEFAULT NULL COMMENT 'end time',
    duration_ms INT DEFAULT 0 COMMENT 'duration in milliseconds',
    status VARCHAR(16) NOT NULL COMMENT 'success/error/cancelled/timeout/skipped',
    async_status VARCHAR(16) DEFAULT NULL COMMENT 'async execution status',
    error_message VARCHAR(2048) DEFAULT '' COMMENT 'error message',
    instance_id VARCHAR(64) DEFAULT '' COMMENT 'execution instance id',
    executor_leader VARCHAR(64) DEFAULT '' COMMENT 'executor leader id',
    is_manual TINYINT(1) DEFAULT 0 COMMENT 'manual trigger flag',
    trace_id VARCHAR(64) DEFAULT '' COMMENT 'trace id',
    session_id VARCHAR(64) DEFAULT '' COMMENT 'session id',
    input_snapshot TEXT COMMENT 'input snapshot',
    output_preview VARCHAR(512) DEFAULT '' COMMENT 'output preview',
    meta VARCHAR(2048) DEFAULT '' COMMENT 'execution metadata',
    dispatch_intent_id BIGINT DEFAULT NULL COMMENT 'dispatch intent id',
    dispatch_batch_id VARCHAR(64) DEFAULT '' COMMENT 'dispatch batch id',
    dispatch_attempt INT DEFAULT NULL COMMENT 'dispatch attempt count',
    notification_status VARCHAR(16) DEFAULT 'not_required'
        COMMENT 'notification status',
    notification_due_at DATETIME DEFAULT NULL COMMENT 'notification due time',
    notification_timezone VARCHAR(64) DEFAULT '' COMMENT 'notification timezone',
    notification_sent_at DATETIME DEFAULT NULL COMMENT 'notification sent time',
    notification_attempts INT DEFAULT 0 COMMENT 'notification attempts',
    notification_error VARCHAR(2048) DEFAULT '' COMMENT 'notification error',
    notification_lock_owner VARCHAR(128) DEFAULT ''
        COMMENT 'notification lock owner',
    notification_locked_at DATETIME DEFAULT NULL COMMENT 'notification lock time',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    is_read TINYINT(1) DEFAULT 0 COMMENT 'whether execution result was read',
    read_at DATETIME DEFAULT NULL COMMENT 'read time',
    INDEX idx_job_id (job_id),
    INDEX idx_tenant_id (tenant_id),
    INDEX idx_status (status),
    INDEX idx_async_status (async_status),
    INDEX idx_scheduled_time (scheduled_time),
    INDEX idx_actual_time (actual_time),
    INDEX idx_trace_id (trace_id),
    INDEX idx_notification_scan (notification_status, notification_due_at),
    INDEX idx_notification_lock (notification_lock_owner, notification_locked_at),
    INDEX idx_execution_read (is_read, read_at),
    INDEX idx_cron_execution_dispatch (
        dispatch_intent_id, dispatch_batch_id, dispatch_attempt
    ),
    INDEX idx_tenant_actual (tenant_id, actual_time),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron execution history'
"""

CREATE_CRON_DISPATCH_BATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_batches (
    batch_id VARCHAR(64) PRIMARY KEY COMMENT 'dispatch batch id',
    parent_job_id VARCHAR(64) NOT NULL COMMENT 'batch parent cron job id',
    parent_external_job_id VARCHAR(64) DEFAULT '' COMMENT 'external scheduler job id',
    tenant_id VARCHAR(64) NOT NULL COMMENT 'parent tenant id',
    source_id VARCHAR(64) DEFAULT '' COMMENT 'source id',
    provider_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    model_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    agent_id VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT 'agent id',
    scheduled_fire_at DATETIME NOT NULL COMMENT 'parent scheduled fire time',
    callback_received_at DATETIME NOT NULL COMMENT 'scheduler callback receive time',
    status VARCHAR(16) NOT NULL DEFAULT 'received'
        COMMENT 'received/pending/running/completed/failed',
    lock_owner VARCHAR(128) DEFAULT '' COMMENT '批次派发锁持有者',
    locked_at DATETIME DEFAULT NULL COMMENT '批次派发锁时间',
    total_count INT NOT NULL DEFAULT 0 COMMENT 'total intents',
    completed_count INT NOT NULL DEFAULT 0 COMMENT 'completed intents',
    failed_count INT NOT NULL DEFAULT 0 COMMENT 'failed intents',
    skipped_count INT NOT NULL DEFAULT 0 COMMENT 'skipped or cancelled intents',
    callback_metadata JSON DEFAULT NULL COMMENT 'raw callback metadata',
    error_message VARCHAR(2048) DEFAULT '' COMMENT 'batch error summary',
    completed_at DATETIME DEFAULT NULL COMMENT 'batch completed time',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        COMMENT 'updated time',
    UNIQUE INDEX uk_dispatch_batch_parent_fire (
        parent_job_id, scheduled_fire_at
    ),
    INDEX idx_dispatch_batch_parent (parent_job_id, created_at),
    INDEX idx_dispatch_batch_source (source_id, scheduled_fire_at),
    INDEX idx_dispatch_batch_status (status, updated_at),
    INDEX idx_dispatch_batch_lock (lock_owner, locked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron dispatch batch runs'
"""

CREATE_CRON_DISPATCH_INTENTS_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_intents (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT 'dispatch intent id',
    batch_id VARCHAR(64) NOT NULL COMMENT 'dispatch batch id',
    intent_role VARCHAR(16) NOT NULL COMMENT 'parent/child',
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        COMMENT 'pending/claimed/dispatched/completed/failed/cancelled',
    source_id VARCHAR(64) DEFAULT '' COMMENT 'source id',
    provider_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    model_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    tenant_id VARCHAR(64) NOT NULL COMMENT 'runtime tenant id',
    agent_id VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT 'agent id',
    job_id VARCHAR(64) NOT NULL COMMENT 'cron job id',
    parent_job_id VARCHAR(64) DEFAULT '' COMMENT 'parent broadcast job id',
    scheduled_fire_at DATETIME DEFAULT NULL COMMENT 'parent scheduled fire time',
    due_at DATETIME NOT NULL COMMENT 'earliest claim time',
    dispatch_order INT NOT NULL DEFAULT 0 COMMENT 'stable order inside batch',
    viewer_heat_score DECIMAL(12,4) NOT NULL DEFAULT 0
        COMMENT 'bounded read heat score',
    attempt_count INT NOT NULL DEFAULT 0 COMMENT 'attempt count',
    max_attempts INT NOT NULL DEFAULT 3 COMMENT 'max attempts',
    lock_owner VARCHAR(128) DEFAULT '' COMMENT 'worker lock owner',
    claim_token VARCHAR(36) NOT NULL DEFAULT '' COMMENT 'unique claim generation',
    locked_at DATETIME DEFAULT NULL COMMENT 'lock time',
    acked_at DATETIME DEFAULT NULL COMMENT 'worker acknowledged time',
    completed_at DATETIME DEFAULT NULL COMMENT 'completion time',
    error_message VARCHAR(2048) DEFAULT '' COMMENT 'last error',
    payload JSON DEFAULT NULL COMMENT 'intent payload',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        COMMENT 'updated time',
    UNIQUE INDEX uk_dispatch_batch_role_job (
        batch_id, intent_role, tenant_id, job_id
    ),
    INDEX idx_dispatch_claim (status, due_at, dispatch_order, id),
    INDEX idx_dispatch_batch (batch_id, dispatch_order),
    INDEX idx_dispatch_lock (lock_owner, locked_at),
    INDEX idx_dispatch_job (job_id),
    INDEX idx_dispatch_source (source_id),
    INDEX idx_dispatch_scope_claim (
        source_id, provider_id, model_id, status, due_at, dispatch_order, id
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron dispatch intent queue'
"""

CREATE_CRON_DISPATCH_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT 'dispatch event id',
    batch_id VARCHAR(64) NOT NULL COMMENT 'dispatch batch id',
    intent_id BIGINT DEFAULT NULL COMMENT 'dispatch intent id',
    event_type VARCHAR(64) NOT NULL COMMENT 'event type',
    worker_id VARCHAR(128) DEFAULT '' COMMENT 'worker id',
    job_id VARCHAR(64) DEFAULT '' COMMENT 'job id',
    tenant_id VARCHAR(64) DEFAULT '' COMMENT 'tenant id',
    source_id VARCHAR(64) DEFAULT '' COMMENT 'source id',
    details JSON DEFAULT NULL COMMENT 'event details',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    INDEX idx_dispatch_events_batch (batch_id, created_at),
    INDEX idx_dispatch_events_intent (intent_id),
    INDEX idx_dispatch_events_type (event_type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SWE cron dispatch telemetry events'
"""

CREATE_CRON_DISPATCH_WORKER_CAPACITY_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_worker_capacity (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT 'capacity snapshot id',
    worker_id VARCHAR(128) NOT NULL COMMENT 'worker id',
    source_id VARCHAR(64) DEFAULT '' COMMENT 'source id',
    provider_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    model_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    strategy_id VARCHAR(64) DEFAULT '' COMMENT 'worker strategy id',
    previous_workers INT NOT NULL DEFAULT 0 COMMENT 'previous effective workers',
    baseline_workers INT NOT NULL DEFAULT 1 COMMENT 'baseline workers',
    min_workers INT NOT NULL DEFAULT 1 COMMENT 'minimum workers',
    max_workers INT NOT NULL DEFAULT 1 COMMENT 'max workers',
    effective_workers INT NOT NULL DEFAULT 1 COMMENT 'effective workers',
    pending_count INT NOT NULL DEFAULT 0 COMMENT 'pending intents',
    claimed_count INT NOT NULL DEFAULT 0 COMMENT 'claimed intents',
    running_count INT NOT NULL DEFAULT 0 COMMENT 'running intents',
    success_count INT NOT NULL DEFAULT 0 COMMENT 'recent success count',
    failure_count INT NOT NULL DEFAULT 0 COMMENT 'recent terminal failure count',
    error_rate DECIMAL(8,6) NOT NULL DEFAULT 0 COMMENT 'terminal failure rate',
    matched_rule JSON DEFAULT NULL COMMENT 'matched adjustment rule',
    avg_latency_ms INT NOT NULL DEFAULT 0 COMMENT 'recent average latency',
    decision_reason VARCHAR(255) DEFAULT '' COMMENT 'capacity decision reason',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    INDEX idx_dispatch_capacity_worker (worker_id, created_at),
    INDEX idx_dispatch_capacity_source (source_id, created_at),
    INDEX idx_dispatch_capacity_scope (
        source_id, provider_id, model_id, strategy_id, created_at
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='SWE cron dispatch worker capacity snapshots'
"""

CREATE_CRON_DISPATCH_SCOPE_LEASES_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_scope_leases (
    source_id VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'source id',
    provider_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    model_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    lock_owner VARCHAR(128) NOT NULL DEFAULT '' COMMENT 'scope lease owner',
    locked_at DATETIME DEFAULT NULL COMMENT 'scope lock time',
    lease_expires_at DATETIME DEFAULT NULL COMMENT 'scope lease expiry time',
    heartbeat_at DATETIME DEFAULT NULL COMMENT 'scope owner heartbeat time',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        COMMENT 'updated time',
    PRIMARY KEY (source_id, provider_id, model_id),
    INDEX idx_scope_lease_owner (lock_owner, lease_expires_at),
    INDEX idx_scope_lease_expiry (lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Cron dispatch model scope leases'
"""

CREATE_CRON_DISPATCH_MODEL_WORKER_POLICY_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_model_worker_policy (
    source_id VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT 'source id',
    provider_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'provider id',
    model_id VARCHAR(128) NOT NULL DEFAULT 'default' COMMENT 'model id',
    default_strategy_id VARCHAR(64) NOT NULL COMMENT 'default strategy id',
    strategy_schedule JSON DEFAULT NULL COMMENT 'time-window strategy schedule',
    enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'enabled',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        COMMENT 'updated time',
    PRIMARY KEY (source_id, provider_id, model_id),
    INDEX idx_dispatch_worker_policy_strategy (default_strategy_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Cron dispatch model worker policy'
"""

CREATE_CRON_DISPATCH_WORKER_STRATEGY_TABLE = """
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_worker_strategy (
    strategy_id VARCHAR(64) PRIMARY KEY COMMENT 'strategy id',
    min_workers INT NOT NULL DEFAULT 1 COMMENT 'minimum workers',
    baseline_workers INT NOT NULL DEFAULT 1 COMMENT 'baseline workers',
    max_workers INT NOT NULL DEFAULT 1 COMMENT 'maximum workers',
    adjust_interval_seconds INT NOT NULL DEFAULT 300 COMMENT 'adjust interval',
    feedback_window_seconds INT NOT NULL DEFAULT 300 COMMENT 'feedback window',
    stale_execution_seconds INT NOT NULL DEFAULT 7800 COMMENT 'stale dispatch timeout',
    error_rate_rules JSON DEFAULT NULL COMMENT 'error-rate adjustment rules',
    enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'enabled',
    description VARCHAR(255) DEFAULT '' COMMENT 'strategy description',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'created time',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        COMMENT 'updated time'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Cron dispatch worker strategy'
"""

ALTER_STATEMENTS = [
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN notification_status VARCHAR(16) DEFAULT 'not_required'
    COMMENT 'notification status'
    AFTER meta
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN notification_due_at DATETIME DEFAULT NULL
    COMMENT 'notification due time'
    AFTER notification_status
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN notification_timezone VARCHAR(64) DEFAULT ''
    COMMENT 'notification timezone'
    AFTER notification_due_at
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN is_read TINYINT(1) DEFAULT 0
    COMMENT 'whether execution result was read'
    AFTER notification_timezone
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN read_at DATETIME DEFAULT NULL
    COMMENT 'read time'
    AFTER is_read
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN dispatch_intent_id BIGINT DEFAULT NULL
    COMMENT 'dispatch intent id'
    AFTER meta
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN dispatch_batch_id VARCHAR(64) DEFAULT ''
    COMMENT 'dispatch batch id'
    AFTER dispatch_intent_id
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD COLUMN dispatch_attempt INT DEFAULT NULL
    COMMENT 'dispatch attempt count'
    AFTER dispatch_batch_id
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD INDEX idx_cron_execution_dispatch (
        dispatch_intent_id, dispatch_batch_id, dispatch_attempt
    )
    """,
    """
    ALTER TABLE swe_cron_executions
    ADD INDEX idx_execution_read (is_read, read_at)
    """,
    """
    ALTER TABLE swe_cron_dispatch_batches
    ADD COLUMN provider_id VARCHAR(128) NOT NULL DEFAULT 'default'
    COMMENT 'provider id'
    AFTER source_id
    """,
    """
    ALTER TABLE swe_cron_dispatch_batches
    ADD COLUMN model_id VARCHAR(128) NOT NULL DEFAULT 'default'
    COMMENT 'model id'
    AFTER provider_id
    """,
    """
    ALTER TABLE swe_cron_dispatch_batches
    ADD COLUMN lock_owner VARCHAR(128) DEFAULT ''
    COMMENT '批次派发锁持有者'
    AFTER status
    """,
    """
    ALTER TABLE swe_cron_dispatch_batches
    ADD COLUMN locked_at DATETIME DEFAULT NULL
    COMMENT '批次派发锁时间'
    AFTER lock_owner
    """,
    """
    ALTER TABLE swe_cron_dispatch_batches
    ADD INDEX idx_dispatch_batch_lock (lock_owner, locked_at)
    """,
    """
    ALTER TABLE swe_cron_dispatch_intents
    ADD COLUMN provider_id VARCHAR(128) NOT NULL DEFAULT 'default'
    COMMENT 'provider id'
    AFTER source_id
    """,
    """
    ALTER TABLE swe_cron_dispatch_intents
    ADD COLUMN model_id VARCHAR(128) NOT NULL DEFAULT 'default'
    COMMENT 'model id'
    AFTER provider_id
    """,
    """
    ALTER TABLE swe_cron_dispatch_intents
    ADD COLUMN scheduled_fire_at DATETIME DEFAULT NULL
    COMMENT 'parent scheduled fire time'
    AFTER parent_job_id
    """,
    """
    ALTER TABLE swe_cron_dispatch_intents
    ADD INDEX idx_dispatch_scope_claim (
        source_id, provider_id, model_id, status, due_at, dispatch_order, id
    )
    """,
]

CREATE_TABLE_STATEMENTS = [
    CREATE_CONTROL_TABLE,
    CREATE_CRON_EXECUTIONS_TABLE,
    CREATE_CRON_DISPATCH_BATCHES_TABLE,
    CREATE_CRON_DISPATCH_INTENTS_TABLE,
    CREATE_CRON_DISPATCH_EVENTS_TABLE,
    CREATE_CRON_DISPATCH_WORKER_CAPACITY_TABLE,
    CREATE_CRON_DISPATCH_SCOPE_LEASES_TABLE,
    CREATE_CRON_DISPATCH_MODEL_WORKER_POLICY_TABLE,
    CREATE_CRON_DISPATCH_WORKER_STRATEGY_TABLE,
]

ALTER_STATEMENTS.extend(RUN_STATE_ALTERS)


async def init_database_tables() -> None:
    """Create/upgrade the tables the standalone Scheduler directly uses."""
    db = get_db_connection()
    for statement in CREATE_TABLE_STATEMENTS:
        await db.execute(statement)
    for statement in ALTER_STATEMENTS:
        try:
            await db.execute(statement)
        except Exception as exc:  # pylint: disable=broad-except
            message = str(exc).lower()
            if "duplicate" not in message and "exists" not in message:
                raise
    logger.info("Scheduler database tables initialized")
