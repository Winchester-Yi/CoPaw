-- 统一异步任务表结构。
-- 由部署或迁移流程执行，应用启动阶段不执行该 DDL。

CREATE TABLE IF NOT EXISTS swe_async_tasks (
    task_id VARCHAR(64) PRIMARY KEY COMMENT '异步任务ID',
    batch_id VARCHAR(128) DEFAULT NULL COMMENT '批次ID',
    service VARCHAR(32) NOT NULL COMMENT '写入服务: swe/market',
    task_type VARCHAR(64) NOT NULL COMMENT '任务类型',
    status VARCHAR(32) NOT NULL COMMENT '任务状态',
    title VARCHAR(255) NOT NULL COMMENT '任务标题',
    summary VARCHAR(1024) DEFAULT NULL COMMENT '任务摘要',
    source_id VARCHAR(128) DEFAULT NULL COMMENT '来源标识',
    actor_user_id VARCHAR(255) DEFAULT NULL COMMENT '操作人ID',
    actor_user_name VARCHAR(255) DEFAULT NULL COMMENT '操作人名称',
    target_count INT NOT NULL DEFAULT 0 COMMENT '目标总数',
    done_count INT NOT NULL DEFAULT 0 COMMENT '完成数量',
    failed_count INT NOT NULL DEFAULT 0 COMMENT '失败数量',
    error_message TEXT DEFAULT NULL COMMENT '错误信息',
    result_json JSON DEFAULT NULL COMMENT '任务结果',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    finished_at DATETIME DEFAULT NULL COMMENT '完成时间',
    INDEX idx_async_tasks_status (status),
    INDEX idx_async_tasks_type (task_type),
    INDEX idx_async_tasks_source (source_id),
    INDEX idx_async_tasks_created (created_at),
    INDEX idx_async_tasks_batch_id (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='统一异步任务主表';

CREATE TABLE IF NOT EXISTS swe_async_task_items (
    task_id VARCHAR(64) NOT NULL COMMENT '异步任务ID',
    target_id VARCHAR(255) NOT NULL COMMENT '目标ID',
    target_name VARCHAR(255) DEFAULT NULL COMMENT '目标名称',
    status VARCHAR(32) NOT NULL COMMENT '目标执行状态',
    error_message TEXT DEFAULT NULL COMMENT '错误信息',
    result_json JSON DEFAULT NULL COMMENT '执行结果',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (task_id, target_id),
    INDEX idx_async_task_items_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='统一异步任务明细表';
