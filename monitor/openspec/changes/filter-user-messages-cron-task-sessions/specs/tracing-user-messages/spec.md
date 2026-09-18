# tracing-user-messages Specification

## MODIFIED Requirements

### Requirement: User Message List Filtering

The Monitor tracing user-message APIs SHALL support an opt-in filter that
excludes cron task sessions from the user-messages page.

#### Scenario: Exclude cron task sessions when requested

- **GIVEN** a request to `GET /monitor/tracing/user-messages`
- **AND** the request includes `exclude_cron_task_sessions=true`
- **WHEN** the service queries `swe_tracing_traces`
- **THEN** rows whose `session_id` starts with `cron-task` are excluded
- **AND** the returned `total` uses the same filtered scope.

#### Scenario: Preserve default user-message behavior

- **GIVEN** a request to `GET /monitor/tracing/user-messages`
- **AND** the request omits `exclude_cron_task_sessions`
- **WHEN** the service queries `swe_tracing_traces`
- **THEN** cron task session rows are not excluded by this change.

#### Scenario: Export uses the same opt-in filter

- **GIVEN** a request to `GET /monitor/tracing/user-messages/export`
- **AND** the request includes `exclude_cron_task_sessions=true`
- **WHEN** the service exports user messages
- **THEN** rows whose `session_id` starts with `cron-task` are excluded from
  the exported data.

### Requirement: User Message ID Column Visibility

The Console user-messages page SHALL hide conversation and session identifiers
by default while allowing users to show them on demand.

#### Scenario: Hide ID columns by default

- **GIVEN** the user-messages page is loaded
- **WHEN** the table renders
- **THEN** the conversation ID column and session ID column are hidden.

#### Scenario: Toggle ID columns

- **GIVEN** the user-messages page is loaded
- **WHEN** the user activates the ID column visibility control
- **THEN** the conversation ID column and session ID column become visible
- **AND** activating the control again hides both columns.
