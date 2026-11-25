-- Read-only verification after import and before reopening writes.
-- Set these two values from the migration manifest before running this file.
SET @migration_id = 'REPLACE_WITH_MANIFEST_MIGRATION_ID';
SET @service_unit_id = 'REPLACE_WITH_CMB_CAAS_SERVICEUNITID';

SELECT DATABASE() AS application_schema, @migration_id AS migration_id,
       @service_unit_id AS service_unit_id;

SELECT published, COUNT(*) AS source_count
FROM swe_conversation_metadata_sources
WHERE migration_id = @migration_id
  AND service_unit_key = CAST(@service_unit_id AS BINARY)
GROUP BY published
ORDER BY published;

SELECT CONVERT(p.tenant_key USING utf8mb4) AS tenant_id,
       CONVERT(p.source_key USING utf8mb4) AS source_id,
       p.published,
       CONVERT(w.agent_key USING utf8mb4) AS agent_id,
       CONVERT(w.workspace_root USING utf8mb4) AS workspace_root,
       w.imported_count,
       (SELECT COUNT(*)
        FROM swe_conversation_chats AS c
        WHERE c.service_unit_key = w.service_unit_key
          AND c.tenant_key = w.tenant_key AND c.source_key = w.source_key
          AND c.agent_key = w.agent_key AND c.deleted_at IS NULL) AS live_chat_count,
       (SELECT COUNT(*)
        FROM swe_conversation_sessions AS s
        WHERE s.service_unit_key = w.service_unit_key
          AND s.tenant_key = w.tenant_key AND s.source_key = w.source_key
          AND s.agent_key = w.agent_key) AS session_count
FROM swe_conversation_metadata_sources AS p
LEFT JOIN swe_conversation_metadata_workspaces AS w
  ON w.service_unit_key = p.service_unit_key AND w.tenant_key = p.tenant_key
  AND w.source_key = p.source_key
WHERE p.migration_id = @migration_id
  AND p.service_unit_key = CAST(@service_unit_id AS BINARY)
ORDER BY p.tenant_key, p.source_key, w.agent_key;
