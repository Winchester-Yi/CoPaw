-- Apply to the independent observability database before enabling recovery.
CREATE TABLE IF NOT EXISTS swe_agentobs_outbox_scopes (
    scope_key BINARY(32) NOT NULL PRIMARY KEY,
    service_unit_key VARBINARY(256) NOT NULL,
    tenant_key VARBINARY(256) NOT NULL,
    source_key VARBINARY(256) NOT NULL,
    agent_key VARBINARY(256) NOT NULL,
    workspace_root VARBINARY(2048) NOT NULL,
    storage_authority VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    registered_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY ix_agentobs_outbox_scopes_service (service_unit_key, scope_key)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS swe_agentobs_outbox_recovery (
    scope_key BINARY(32) NOT NULL PRIMARY KEY,
    lease_owner CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
    lease_until DATETIME(6) NULL,
    scan_cursor TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL
) ENGINE=InnoDB;

CREATE INDEX ix_agentobs_inbox_event_ack
    ON swe_agentobs_inbox (event_id, processing_state);
