-- Read-only inventory for choosing the installation or upgrade path.
-- Select the target application database before running this file.
SELECT DATABASE() AS application_schema;

SELECT TABLE_NAME, ENGINE, ROW_FORMAT
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('swe_conversation_sessions', 'swe_conversation_chats',
                     'swe_conversation_metadata_sources',
                     'swe_conversation_metadata_workspaces')
ORDER BY TABLE_NAME;

SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('swe_conversation_sessions', 'swe_conversation_chats',
                     'swe_conversation_metadata_sources',
                     'swe_conversation_metadata_workspaces')
ORDER BY TABLE_NAME, ORDINAL_POSITION;

SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('swe_conversation_sessions', 'swe_conversation_chats',
                     'swe_conversation_metadata_sources',
                     'swe_conversation_metadata_workspaces')
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
