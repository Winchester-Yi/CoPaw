-- DESIGN DRAFT / NOT APPLIED TO PRODUCTION, updated 2026-11-02.
-- Spec: docs/superpowers/specs/2026-11-02-conversation-service-unit-isolation-design.md
-- DBA runs once ONLY after extension and verified ownership backfill, with writers stopped.
-- Pending v3 uses precreated application tables; current v2 uses staging.
-- Pending v3 spec: docs/superpowers/specs/2026-11-02-conversation-discovery-precreated-import-design.md
-- Source ownership must come from the coordinated migration inventory, never a DB default.
-- This guard verifies share ownership/locator completeness, not the full NAS/semantic closure.
-- Full importer validation must also reject invalid service identities and check all extents.
-- No backfill DML, no token/legacy-field changes, no production startup DDL.
-- DDL implicitly commits. Run with a fail-fast client; do not continue on errors.
SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM swe_chat_shares AS s
    LEFT JOIN swe_conversation_publications AS p ON p.id = s.publication_id
    WHERE s.service_unit_key IS NULL
       OR OCTET_LENGTH(s.service_unit_key) = 0
       OR p.id IS NULL
       OR s.service_unit_key <> p.service_unit_key
       OR s.byte_offset IS NULL
       OR s.byte_length IS NULL
) THEN
    'ALTER TABLE swe_chat_shares MODIFY COLUMN service_unit_key VARBINARY(256) NOT NULL COMMENT ''分享所属NAS服务单元原值，与publication归属一致，由可信部署上下文维护'''
ELSE
    'SERVICE_OWNERSHIP_BACKFILL_REQUIRED_FOR_SWE_CHAT_SHARES'
END INTO @conv_share_service_finalize_ddl;
PREPARE conv_share_service_finalize_stmt FROM @conv_share_service_finalize_ddl;
EXECUTE conv_share_service_finalize_stmt;
DEALLOCATE PREPARE conv_share_service_finalize_stmt;
