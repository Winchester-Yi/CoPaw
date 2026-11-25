-- First-time installation of the complete mysql-metadata schema.
-- Select the target application database before running this file.
-- Run only when none of the four tables exists. Times are UTC.
CREATE TABLE swe_conversation_sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部自增主键',
    service_unit_key VARBINARY(256) NOT NULL COMMENT 'CMB_CAAS_SERVICEUNITID原值，稳定标识共享同一NAS的服务单元',
    tenant_key VARBINARY(256) NOT NULL COMMENT '租户标识原值，按UTF-8字节精确匹配',
    source_key VARBINARY(256) NOT NULL COMMENT '来源标识原值，按UTF-8字节精确匹配',
    agent_key VARBINARY(256) NOT NULL COMMENT 'Agent标识原值，按UTF-8字节精确匹配',
    user_key VARBINARY(256) NOT NULL COMMENT '用户标识原值，按字节精确匹配',
    channel_key VARBINARY(64) NOT NULL COMMENT '渠道标识原值，按字节精确匹配',
    external_session_key VARBINARY(1024) NOT NULL COMMENT '外部逻辑Session标识，与完整scope及user和channel联合唯一',
    nas_filename VARBINARY(255) NULL,
    draft_chat_id BIGINT UNSIGNED NULL COMMENT '专家草稿重试固定的Chat内部ID；失效映射不可自动复活',
    created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC，微秒精度',
    updated_at DATETIME(6) NOT NULL COMMENT '最近更新的UTC时间，微秒精度',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_session_identity
        (service_unit_key, tenant_key, source_key, agent_key, user_key, channel_key, external_session_key),
    UNIQUE KEY uq_conv_nas_filename
        (service_unit_key, tenant_key, source_key, agent_key, nas_filename),
    KEY ix_conv_session_lookup
        (service_unit_key, tenant_key, source_key, agent_key, channel_key, external_session_key, user_key)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='逻辑会话身份，完整scope内唯一';

CREATE TABLE swe_conversation_chats (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部自增主键',
    service_unit_key VARBINARY(256) NOT NULL COMMENT '所属服务单元原值，与关联Session同事务维护，不由客户端指定',
    session_id BIGINT UNSIGNED NOT NULL COMMENT '关联逻辑Session内部ID，由应用事务校验关联和作用域',
    external_chat_key VARBINARY(128) NOT NULL COMMENT '保留现有外部Chat UUID，不替换为数据库整数ID',
    external_session_key VARBINARY(1024) NOT NULL COMMENT '同事务复制Session外部ID，供无关联摘要查询',
    registry_order BIGINT UNSIGNED NOT NULL COMMENT '原registry位置；新增记录递增分配，兼容相同更新时间的选择顺序',
    tenant_key VARBINARY(256) NOT NULL COMMENT '租户标识原值，按UTF-8字节精确匹配',
    source_key VARBINARY(256) NOT NULL COMMENT 'Chat所属来源标识，与关联Session同事务维护',
    agent_key VARBINARY(256) NOT NULL COMMENT 'Chat所属Agent标识，与关联Session同事务维护',
    user_key VARBINARY(256) NOT NULL COMMENT '用户标识原值，按字节精确匹配',
    channel_key VARBINARY(64) NOT NULL COMMENT '渠道标识原值，按字节精确匹配',
    title TEXT NOT NULL COMMENT '会话标题，保留完整显示文本',
    metadata_text MEDIUMTEXT NOT NULL COMMENT '白名单轻量元数据序列化文本，由应用校验，不存正文或指令',
    extended_metadata_text MEDIUMTEXT NULL,
    resources_cleaned_at DATETIME(6) NULL,
    metadata_revision BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '标题及普通元数据CAS版本，与历史提交版本独立',
    expert_binding_revision BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '服务端专家绑定CAS版本，绑定和解绑均递增',
    expert_id VARBINARY(256) NULL COMMENT '绑定的专家定义精确ID；NULL表示当前未绑定',
    status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'idle' COMMENT '兼容Chat状态',
    created_at DATETIME(6) NOT NULL COMMENT '创建时间，UTC，微秒精度',
    updated_at DATETIME(6) NOT NULL COMMENT '最近更新的UTC时间，微秒精度',
    deleted_at DATETIME(6) NULL COMMENT '逻辑删除UTC时间；NULL表示未删除，权限即时生效',
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_chat_external (service_unit_key, tenant_key, source_key, agent_key, external_chat_key),
    KEY ix_conv_chat_recent
        (service_unit_key, tenant_key, source_key, agent_key, user_key, deleted_at, updated_at, external_chat_key),
    KEY ix_conv_chat_channel_recent
        (service_unit_key, tenant_key, source_key, agent_key, user_key, channel_key, deleted_at, updated_at, external_chat_key),
    KEY ix_conv_chat_session (session_id, updated_at, registry_order),
    KEY ix_conv_chat_registry
        (service_unit_key, tenant_key, source_key, agent_key, registry_order),
    KEY ix_conv_chat_session_first (session_id, deleted_at, registry_order)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COMMENT='Chat目录及专家绑定';

CREATE TABLE swe_conversation_metadata_sources (
    service_unit_key VARBINARY(256) NOT NULL,
    tenant_key VARBINARY(256) NOT NULL,
    source_key VARBINARY(256) NOT NULL,
    migration_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    manifest_digest BINARY(32) NOT NULL,
    published TINYINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (service_unit_key, tenant_key, source_key)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

CREATE TABLE swe_conversation_metadata_workspaces (
    service_unit_key VARBINARY(256) NOT NULL,
    tenant_key VARBINARY(256) NOT NULL,
    source_key VARBINARY(256) NOT NULL,
    agent_key VARBINARY(256) NOT NULL,
    workspace_root VARBINARY(1024) NOT NULL,
    imported_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    registry_digest BINARY(32) NOT NULL,
    PRIMARY KEY (service_unit_key, tenant_key, source_key, agent_key),
    UNIQUE KEY uq_conv_metadata_root (service_unit_key, workspace_root)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
