-- Final post-import cleanup and DDL for share service ownership.
-- Validated by isolated integration tests on MySQL 8.0.46; verify compatibility
-- against the actual target MySQL/TDSQL service before production execution.
-- Spec: docs/superpowers/specs/2026-11-02-conversation-service-unit-isolation-design.md
-- DBA runs once after import, with share writers stopped and a pre-DDL backup.
-- The v3 importer writes to precreated application tables; this runs only after
-- import verification and share ownership/locator backfill.
-- Source ownership must come from the coordinated migration inventory, never a DB default.
-- This guard verifies share ownership/locator completeness, not the full NAS/semantic closure.
-- Full importer validation must also reject invalid service identities and check all extents.
-- Rows with all four new locator fields NULL are expired legacy shares and are deleted.
-- Partially populated or mismatched rows are retained and cause this script to fail.
-- This intentionally invalidates historical share links; inspect the deletion count first.
-- DDL implicitly commits. Run with a fail-fast client; do not continue on errors.
SELECT COUNT(*) AS legacy_shares_to_expire
FROM swe_chat_shares
WHERE publication_id IS NULL
  AND byte_offset IS NULL
  AND byte_length IS NULL
  AND service_unit_key IS NULL;

DELETE FROM swe_chat_shares
WHERE publication_id IS NULL
  AND byte_offset IS NULL
  AND byte_length IS NULL
  AND service_unit_key IS NULL;

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
