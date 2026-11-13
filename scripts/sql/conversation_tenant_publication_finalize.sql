-- Partial tenant migration only. Run in a DBA maintenance/MDL window after
-- every registered tenant is published. Never delete or expire legacy shares.
-- A fail-fast client is required; DDL implicitly commits.
SELECT CASE WHEN
    EXISTS (SELECT 1 FROM swe_conversation_migration_tenants)
    AND NOT EXISTS (
        SELECT 1 FROM swe_conversation_migration_tenants WHERE state <> 'published'
    )
    AND NOT EXISTS (
        SELECT 1 FROM swe_chat_shares s
        LEFT JOIN swe_conversation_publications p ON p.id=s.publication_id
        WHERE s.service_unit_key IS NULL OR OCTET_LENGTH(s.service_unit_key)=0
           OR p.id IS NULL OR s.service_unit_key<>p.service_unit_key
           OR s.byte_offset IS NULL OR s.byte_length IS NULL
    )
THEN
    'ALTER TABLE swe_chat_shares MODIFY COLUMN service_unit_key VARBINARY(256) NOT NULL COMMENT ''分享所属NAS服务单元原值，与publication归属一致，由可信部署上下文维护'''
ELSE
    'ALL_TENANTS_AND_SHARES_MUST_BE_PUBLISHED_BEFORE_FINALIZE'
END INTO @conv_tenant_finalize_ddl;
PREPARE conv_tenant_finalize_stmt FROM @conv_tenant_finalize_ddl;
EXECUTE conv_tenant_finalize_stmt;
DEALLOCATE PREPARE conv_tenant_finalize_stmt;
