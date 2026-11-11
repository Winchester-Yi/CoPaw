-- Final pre-cutover DDL for the offline v3 migration journal.
-- Validated by isolated integration tests on MySQL 8.0.46; verify compatibility
-- against the actual target MySQL/TDSQL service before production execution.
-- DBA applies once to the selected application schema. CLI never executes this file.
-- Operational journals are outside the seven-table business backup/restore set.
-- No IF NOT EXISTS, DROP, foreign keys or SQL JSON. Times are explicit UTC.
-- First import requires six empty business tables. All services use one job.
-- Entry IDs are durable tickets for session/chat/turn/message/publication only.
-- Retention uses its own AUTO_INCREMENT and scoped business-key reconciliation.
-- Do not rebuild journals or append imports after online authority has started.

CREATE TABLE swe_conversation_migration_jobs (
    migration_id VARBINARY(36) NOT NULL COMMENT '规范UUID；失败续跑复用同一作业',
    manifest_version INT UNSIGNED NOT NULL COMMENT '导入协议版本，应用校验为3',
    state VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT 'planned/importing/imported/verified/ready/blocked',
    target_binding_text MEDIUMTEXT NOT NULL COMMENT '目标服务器、应用schema、全部服务及原NAS根绑定，不含凭据',
    source_binding_text MEDIUMTEXT NOT NULL COMMENT '冻结清单引用、完整性指纹、解析版本和停写证据，不作领域身份',
    verification_text MEDIUMTEXT NULL COMMENT '验收覆盖和凭证元数据，不含会话正文',
    created_at DATETIME(6) NOT NULL COMMENT '创建UTC时间',
    updated_at DATETIME(6) NOT NULL COMMENT '最近进度UTC时间',
    PRIMARY KEY (migration_id),
    KEY ix_conv_migration_job_state (state, migration_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='人工预建离线迁移作业；应用schema级专用连接锁串行化写入';

CREATE TABLE swe_conversation_migration_entries (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '持久日志及全局整数实体ID票据；首次迁移六业务表为空，五类预分配实体target_id=id',
    migration_id VARBINARY(36) NOT NULL COMMENT '所属作业，应用事务校验',
    entry_kind VARBINARY(32) NOT NULL COMMENT 'session/chat/turn/message/publication/retention/share/batch等明确种类',
    source_key VARBINARY(2048) NOT NULL COMMENT '含服务及原相对位置的稳定规范键，不用遍历序号或摘要；超长阻断',
    state VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT 'planned/mapped/prepared/committed/verified/blocked，按种类校验',
    target_id BIGINT UNSIGNED NULL COMMENT '五类预分配实体等于本行id；Retention保存实际ID；token或纯批次可NULL',
    operation_key VARBINARY(64) NULL COMMENT 'publication首次预留时固定的操作身份',
    details_text MEDIUMTEXT NULL COMMENT 'publication scope/path locator、受限计划、分享扩展列回填前值及对账证据，不存正文或凭据',
    created_at DATETIME(6) NOT NULL COMMENT '创建UTC时间',
    updated_at DATETIME(6) NOT NULL COMMENT '最近变更UTC时间',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_migration_source (migration_id, entry_kind, source_key),
    KEY ix_conv_migration_entry_state (migration_id, state, id),
    KEY ix_conv_migration_target (entry_kind, target_id)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='源到业务ID的事务映射和批次日志，不替代publication协议';
