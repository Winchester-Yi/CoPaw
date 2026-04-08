## ADDED Requirements

### Requirement: Chat control records SHALL use durable transactional storage
The backend SHALL store chat metadata in durable transactional storage rather than shared JSON files so concurrent multi-instance mutations do not overwrite one another.

#### Scenario: Concurrent chat mutations do not lose updates
- **WHEN** two backend instances concurrently create, update, or delete chat records for the same tenant workspace
- **THEN** the authoritative chat store SHALL preserve a deterministic result without last-writer-wins file overwrite corruption

#### Scenario: Chat reads are consistent across instances
- **WHEN** different backend instances read chat metadata after a successful mutation
- **THEN** they SHALL observe the same authoritative chat state from durable storage

### Requirement: Cron and job control records SHALL use durable transactional storage
The backend SHALL store cron and job definitions in durable transactional storage rather than shared `jobs.json` files so multi-instance mutation and scheduling state remain consistent.

#### Scenario: Job definition mutations are durable across instances
- **WHEN** one backend instance successfully creates, updates, pauses, resumes, or deletes a job definition
- **THEN** another backend instance SHALL read the same resulting job definition state from durable storage

#### Scenario: Concurrent job mutations do not overwrite via shared file replacement
- **WHEN** multiple backend instances mutate job definitions concurrently
- **THEN** the authoritative job store SHALL prevent silent lost updates caused by whole-file shared JSON replacement

### Requirement: Durable run records SHALL capture execution facts independently of ephemeral coordination
The backend SHALL record durable run facts separately from ephemeral runtime coordination so completed or failed interactive and scheduled executions remain queryable after the runtime lease expires.

#### Scenario: Completed run remains queryable after runtime coordination expires
- **WHEN** an interactive or scheduled run completes and its ephemeral coordination state is later removed
- **THEN** the backend SHALL retain durable run metadata describing the execution outcome

#### Scenario: Failed run preserves durable error result
- **WHEN** an interactive or scheduled run fails
- **THEN** the backend SHALL persist a durable failure record that remains readable across backend instances

### Requirement: NFS SHALL not remain the authoritative store for high-churn control state
The backend SHALL restrict NFS-backed shared files to file assets and workspace documents rather than authoritative chat, job, or run control records.

#### Scenario: Workspace files remain file-oriented
- **WHEN** the backend stores uploads, screenshots, exports, or workspace documents such as `HEARTBEAT.md`
- **THEN** it MAY continue using NFS-backed files for those assets

#### Scenario: Control-state writes do not depend on shared JSON authority
- **WHEN** the backend persists authoritative chat, job, or run control state
- **THEN** it SHALL use durable shared storage rather than treating NFS JSON files as the source of truth
