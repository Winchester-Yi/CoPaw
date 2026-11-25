-- 批调度简报，由运维在 Scheduler 共库手工执行，时间字段使用 UTC。
CREATE TABLE IF NOT EXISTS swe_cron_batch_status_reports (
    report_id CHAR(36) NOT NULL COMMENT '报告标识',
    source_id VARCHAR(128) NOT NULL COMMENT '来源标识',
    business_date DATE NOT NULL COMMENT '北京时间业务日',
    recipient_ids JSON NOT NULL COMMENT '排序去重后的收件员工号',
    status VARCHAR(32) NOT NULL COMMENT '报告处理状态',
    available_at DATETIME(6) NOT NULL COMMENT '最早取数时间',
    collection_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '只读取数尝试次数',
    lease_token CHAR(36) NULL COMMENT '取数租约令牌',
    lease_until DATETIME(6) NULL COMMENT '取数租约截止时间',
    snapshot_at DATETIME(6) NULL COMMENT '快照取数时间',
    batch_count INT UNSIGNED NULL COMMENT '可报告批次数',
    snapshot_json JSON NULL COMMENT '冻结的批次和模型池统计',
    parts_json JSON NULL COMMENT '冻结的招乎正文分段',
    template_version INT UNSIGNED NULL COMMENT '正文模板版本',
    error_code VARCHAR(128) NULL COMMENT '公开错误码',
    created_at DATETIME(6) NOT NULL COMMENT '请求受理时间',
    finished_at DATETIME(6) NULL COMMENT '处理结束时间',
    PRIMARY KEY (report_id),
    KEY idx_report_collect (status, available_at, report_id),
    KEY idx_report_lease (status, lease_until, report_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='定时任务批调度简报';

CREATE TABLE IF NOT EXISTS swe_cron_batch_status_report_deliveries (
    report_id CHAR(36) NOT NULL COMMENT '报告标识',
    recipient_id CHAR(8) NOT NULL COMMENT '招乎收件员工号',
    part_no INT UNSIGNED NOT NULL COMMENT '正文段序号，从1开始',
    delivery_id CHAR(36) NOT NULL COMMENT '单段投递标识',
    status VARCHAR(24) NOT NULL COMMENT '单段投递状态',
    started_at DATETIME(6) NULL COMMENT '投递开始时间',
    finished_at DATETIME(6) NULL COMMENT '投递结束时间',
    error_code VARCHAR(128) NULL COMMENT '公开错误码',
    receipt_ids JSON NULL COMMENT '招乎渠道回执标识',
    PRIMARY KEY (report_id, recipient_id, part_no),
    UNIQUE KEY uk_report_delivery_id (delivery_id),
    KEY idx_report_delivery_status (status, started_at, report_id),
    KEY idx_report_delivery_result (report_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='批调度简报逐人逐段投递记录';
