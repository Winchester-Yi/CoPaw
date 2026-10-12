-- Token diagnostics own all records; no foreign keys to Trace tables.
CREATE TABLE IF NOT EXISTS swe_token_runs (
  branch_run_id VARCHAR(36) PRIMARY KEY,
  task_run_id VARCHAR(36) NOT NULL,
  parent_branch_run_id VARCHAR(36) NULL,
  source_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  trace_id VARCHAR(64) NULL,
  started_at DATETIME(6) NOT NULL,
  ended_at DATETIME(6) NULL,
  revision BIGINT NOT NULL DEFAULT 0,
  data_revision BIGINT NOT NULL DEFAULT 0,
  analyzed_revision BIGINT NOT NULL DEFAULT 0,
  execution_state VARCHAR(24) NOT NULL DEFAULT 'running',
  expected_attempt_count BIGINT NULL,
  gap_count BIGINT NOT NULL DEFAULT 0,
  analysis_state VARCHAR(32) NOT NULL DEFAULT 'pending',
  dirty TINYINT NOT NULL DEFAULT 1,
  analysis_error VARCHAR(64) NULL,
  analysis_summary JSON NULL,
  claim_token VARCHAR(36) NULL,
  lease_until DATETIME(6) NULL,
  payload JSON NOT NULL,
  INDEX token_run_scope_time(source_id,tenant_id,started_at),
  INDEX token_run_task(task_run_id),
  INDEX token_run_trace(source_id,trace_id),
  INDEX token_run_analysis(dirty,lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS swe_token_model_calls (
  call_id VARCHAR(36) PRIMARY KEY,
  task_run_id VARCHAR(36) NOT NULL,
  branch_run_id VARCHAR(36) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  logical_call_id VARCHAR(36) NOT NULL,
  logical_seq BIGINT NOT NULL,
  attempt_no INT NOT NULL,
  revision BIGINT NOT NULL,
  execution_state VARCHAR(24) NOT NULL,
  usage_status VARCHAR(24) NOT NULL,
  capture_state VARCHAR(24) NOT NULL,
  input_tokens BIGINT NULL,
  output_tokens BIGINT NULL,
  model_name VARCHAR(128) NULL,
  provider_id VARCHAR(128) NULL,
  trace_id VARCHAR(64) NULL,
  started_at DATETIME(6) NOT NULL,
  ended_at DATETIME(6) NULL,
  payload JSON NOT NULL,
  INDEX token_call_branch(branch_run_id,logical_seq,attempt_no),
  INDEX token_call_scope_task(source_id,tenant_id,task_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS swe_token_input_items (
  call_id VARCHAR(36) NOT NULL,
  occurrence_index INT NOT NULL,
  branch_run_id VARCHAR(36) NOT NULL,
  task_run_id VARCHAR(36) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  category VARCHAR(32) NOT NULL,
  fingerprint CHAR(64) NULL,
  estimated_tokens BIGINT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY(call_id,occurrence_index),
  INDEX token_item_scope_call(source_id,tenant_id,call_id),
  INDEX token_item_branch(branch_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS swe_token_execution_events (
  event_id VARCHAR(36) PRIMARY KEY,
  task_run_id VARCHAR(36) NOT NULL,
  branch_run_id VARCHAR(36) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  event_seq BIGINT NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  revision BIGINT NOT NULL DEFAULT 1,
  created_at DATETIME(6) NOT NULL,
  payload JSON NOT NULL,
  INDEX token_event_branch(branch_run_id,event_seq),
  INDEX token_event_scope_task(source_id,tenant_id,task_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS swe_token_findings (
  finding_id CHAR(64) PRIMARY KEY,
  task_run_id VARCHAR(36) NOT NULL,
  branch_run_id VARCHAR(36) NOT NULL,
  source_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  rule_id VARCHAR(64) NOT NULL,
  rule_version INT NOT NULL,
  finding_key CHAR(64) NOT NULL,
  observed_revision BIGINT NOT NULL,
  state VARCHAR(16) NOT NULL DEFAULT 'active',
  updated_at DATETIME(6) NOT NULL,
  payload JSON NOT NULL,
  UNIQUE KEY token_finding_identity(branch_run_id,rule_id,rule_version,finding_key),
  INDEX token_finding_scope_task(source_id,tenant_id,task_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
