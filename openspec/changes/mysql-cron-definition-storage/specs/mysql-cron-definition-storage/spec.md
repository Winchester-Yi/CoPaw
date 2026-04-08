## ADDED Requirements

### Requirement: Cron definitions SHALL use durable transactional storage
The backend SHALL store authoritative cron and heartbeat definitions in MySQL rather than shared `jobs.json` files.

#### Scenario: Concurrent cron mutations do not overwrite one another
- **WHEN** multiple backend instances concurrently create, update, pause, resume, or delete cron definitions
- **THEN** the authoritative cron store SHALL preserve a deterministic result without whole-file overwrite corruption

#### Scenario: Cron definition reads remain consistent across instances
- **WHEN** one backend instance successfully mutates a cron definition
- **THEN** another backend instance SHALL read the same resulting definition state from the authoritative durable store

### Requirement: Scheduler reloads SHALL use durable cron definitions
The backend SHALL rebuild cron schedules from the authoritative durable cron repository rather than from `jobs.json`.

#### Scenario: Active leader reloads from MySQL-backed definitions
- **WHEN** the active scheduler leader reloads cron definitions after a successful mutation
- **THEN** it SHALL rebuild local schedules from the authoritative MySQL-backed repository

### Requirement: Migration from shared jobs JSON SHALL preserve existing definitions
The backend SHALL provide a migration path from existing `jobs.json` definitions before removing JSON as the authoritative cron writer.

#### Scenario: Existing job definitions are preserved during cutover
- **WHEN** a tenant-agent workspace already contains cron definitions in `jobs.json`
- **THEN** the backend SHALL migrate or import those definitions so they remain visible after the authoritative writer switches to MySQL
