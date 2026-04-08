## ADDED Requirements

### Requirement: Cron scheduling ownership is single-active per tenant-agent workspace
The backend SHALL coordinate cron scheduling through Redis so that at most one instance at a time actively schedules cron and heartbeat jobs for a given tenant-agent workspace.

#### Scenario: Only the leader activates scheduling
- **WHEN** multiple backend instances load the same tenant-agent workspace
- **THEN** the backend SHALL allow only the instance holding the Redis agent lease to activate local cron scheduling for that workspace

#### Scenario: Followers stay passive
- **WHEN** an instance does not hold the Redis agent lease for a tenant-agent workspace
- **THEN** the backend SHALL keep that workspace's cron subsystem passive and MUST NOT run scheduled cron or heartbeat callbacks for that workspace

#### Scenario: Leadership failover re-establishes scheduling
- **WHEN** the current leader loses its Redis lease or exits and another instance acquires the lease
- **THEN** the new leader SHALL rebuild local scheduling state from the shared cron definition store and resume future scheduled execution for that workspace

### Requirement: Timed cron execution is de-duplicated across instances
The backend SHALL use a Redis execution lock for scheduler-originated cron triggers so transient leadership overlap does not result in duplicate timed execution of the same job.

#### Scenario: Overlapping leaders do not duplicate a timed run
- **WHEN** two instances transiently reach the same timed cron trigger during a lease handoff window
- **THEN** the backend SHALL allow only one instance to execute that timed job

#### Scenario: Timed lock expiry covers job timeout
- **WHEN** the backend acquires a timed execution lock for a scheduled cron job
- **THEN** the lock lifetime SHALL be long enough to cover the configured cron execution timeout plus a safety margin

### Requirement: Manual job execution remains an explicit extra run
The backend SHALL preserve manual `run_job` behavior as an explicit one-shot execution that is independent from timed scheduler de-duplication.

#### Scenario: Manual run bypasses timed execution lock
- **WHEN** an operator manually triggers `run_job` for a cron job
- **THEN** the backend SHALL execute that manual run without requiring the timed scheduler execution lock

#### Scenario: Manual run may overlap with a scheduled run
- **WHEN** a manual `run_job` request occurs while the same job is already executing from a scheduled trigger
- **THEN** the backend SHALL treat the manual run as a separate explicit execution rather than suppressing it as a duplicate timed run

### Requirement: Cron mutations notify the active leader to reload
The backend SHALL publish a Redis reload signal after successful cron definition mutations so the active leader refreshes its local schedule from the shared cron definition store.

#### Scenario: Successful cron mutation triggers leader reload
- **WHEN** any backend instance successfully creates, updates, pauses, resumes, or deletes a cron definition in the shared cron definition store for a tenant-agent workspace
- **THEN** the backend SHALL publish a reload signal for that tenant-agent workspace so the active leader refreshes its local schedule

#### Scenario: Failed cron mutation does not publish reload
- **WHEN** a cron definition mutation fails before it is durably written to the shared cron definition store
- **THEN** the backend SHALL NOT publish a reload signal for that failed mutation

### Requirement: Redis coordination failures fail safe for scheduling
The backend SHALL stop active scheduling when leadership can no longer be renewed safely rather than continuing unsafely without confirmed lease ownership.

#### Scenario: Lease renewal failure deactivates scheduling
- **WHEN** the active leader cannot renew its Redis agent lease within the configured failure threshold
- **THEN** the backend SHALL deactivate local cron scheduling for that tenant-agent workspace

#### Scenario: Redis outage does not promote a follower without a lease
- **WHEN** a follower instance cannot reach Redis to establish confirmed lease ownership
- **THEN** the backend SHALL keep cron scheduling inactive for that tenant-agent workspace
