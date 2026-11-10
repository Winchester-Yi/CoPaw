-- REVIEW DRAFT ONLY. Not executed or approved as a deployment migration.
-- Companion: ../2026-11-06-conversation-sql-contract-review.md
-- User precreates tables after final schema and legacy format validation.
-- Application code and exact schema readiness do not support this table yet.
-- This is NOT a patch for the currently inspected copaw_dev schema.
-- No body, summary, tool arguments/results or materialized recovery state here.

CREATE TABLE swe_conversation_legacy_sources (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    service_unit_key VARBINARY(256) NOT NULL,
    scope_kind VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    tenant_key VARBINARY(256) NOT NULL,
    source_key VARBINARY(256) NOT NULL,
    agent_key VARBINARY(256) NOT NULL,
    session_id BIGINT UNSIGNED NULL,
    chat_id BIGINT UNSIGNED NULL,
    legacy_key VARBINARY(64) NOT NULL,
    root_key VARBINARY(64) NOT NULL,
    root_binding_version BIGINT UNSIGNED NOT NULL,
    source_kind VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    resolver_key VARBINARY(64) NOT NULL,
    descriptor_version INT UNSIGNED NOT NULL,
    relative_path VARBINARY(1024) NULL,
    file_byte_length BIGINT UNSIGNED NULL,
    file_sha256 BINARY(32) NULL,
    manifest_text MEDIUMTEXT NULL,
    descriptor_sha256 BINARY(32) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_conv_legacy_source (
        service_unit_key, scope_kind, tenant_key, source_key, agent_key,
        legacy_key
    ),
    CONSTRAINT ck_conv_legacy_source_scope CHECK (
        scope_kind IN ('workspace', 'legacy_share')
        AND (
            scope_kind <> 'legacy_share'
            OR (session_id IS NULL AND chat_id IS NULL)
        )
    ),
    CONSTRAINT ck_conv_legacy_source_identity CHECK (
        OCTET_LENGTH(service_unit_key) > 0
        AND OCTET_LENGTH(legacy_key) > 0
        AND OCTET_LENGTH(root_key) > 0
        AND OCTET_LENGTH(resolver_key) > 0
        AND root_binding_version > 0
        AND descriptor_version > 0
    ),
    CONSTRAINT ck_conv_legacy_source_shape CHECK (
        (
            source_kind = 'extent_file'
            AND relative_path IS NOT NULL
            AND OCTET_LENGTH(relative_path) > 0
            AND file_byte_length IS NOT NULL
            AND file_sha256 IS NOT NULL
            AND manifest_text IS NULL
        )
        OR
        (
            source_kind = 'composite'
            AND relative_path IS NULL
            AND file_byte_length IS NULL
            AND file_sha256 IS NULL
            AND manifest_text IS NOT NULL
            AND OCTET_LENGTH(manifest_text) > 0
        )
    )
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4
COMMENT='不可变旧NAS内容来源；只读文件定位或有版本的无正文组合描述';

-- CHECKs describe local row shape only. Application validation must still
-- enforce canonical safe paths, trusted root binding, exact scope/owner,
-- resolver/version allowlists, strict JSON, byte budgets and file evidence.
-- Empty tenant/source/agent values remain valid distinct legacy identities.
-- Sources are immutable and not automatically reclaimed in this version.
-- Backup enumeration pages by full scope + legacy_key using the unique key;
-- no redundant scope/id, physical-path, hash, component or GC index is added.
