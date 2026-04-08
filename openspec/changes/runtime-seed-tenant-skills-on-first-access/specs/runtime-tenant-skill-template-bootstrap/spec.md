## ADDED Requirements

### Requirement: First-access bootstrap seeds tenant skill pool from default tenant
The system SHALL seed a new tenant's `skill_pool` from the `default`
tenant during the first successful runtime bootstrap when the target
tenant has no existing skill-pool state.

#### Scenario: First access copies default tenant skill pool
- **WHEN** runtime bootstrap runs for tenant `tenant-a`
- **AND** the `default` tenant has one or more valid skill directories
  under `skill_pool`
- **AND** `tenant-a` has no initialized tenant-local skill pool state
- **THEN** the system SHALL copy the default tenant's skill-pool content
  into `tenant-a/skill_pool`
- **AND** the target tenant's pool manifest SHALL be reconciled against
  the copied filesystem state

#### Scenario: Source skill directories are sufficient for pool seeding
- **WHEN** the `default` tenant has one or more valid skill directories
  under `skill_pool`
- **AND** the source pool manifest is absent or stale
- **THEN** the system SHALL still treat the source pool as seedable
  after reconciling source state from disk

#### Scenario: Pool config is preserved from source manifest
- **WHEN** a copied source pool skill has durable `config` state in the
  source manifest
- **THEN** the target tenant's seeded pool manifest SHALL preserve that
  `config` for the copied skill

#### Scenario: Existing target pool state is not overwritten
- **WHEN** runtime bootstrap runs for a tenant that already has
  initialized pool state
- **THEN** the system SHALL NOT overwrite or recopy that tenant-local
  pool state

#### Scenario: Runtime bootstrap falls back when no default pool template exists
- **WHEN** runtime bootstrap runs for a tenant with no initialized
  pool state
- **AND** the `default` tenant has no usable skill-pool template content
- **THEN** the system SHALL initialize the tenant pool using builtin
  skill initialization behavior

### Requirement: First-access bootstrap seeds default workspace skills from default tenant
The system SHALL seed a new tenant's default workspace skills from the
`default` tenant's default workspace during the first successful runtime
bootstrap when the target default workspace has no existing workspace
skill state.

#### Scenario: First access copies default workspace skills
- **WHEN** runtime bootstrap runs for tenant `tenant-a`
- **AND** the `default` tenant default workspace has one or more valid
  skill directories under `skills`
- **AND** `tenant-a` has no initialized skill state in
  `workspaces/default`
- **THEN** the system SHALL copy the source workspace skill directories
  into `tenant-a/workspaces/default/skills`
- **AND** the target workspace skill manifest SHALL be reconciled from
  the copied filesystem state

#### Scenario: Source workspace skill directories are sufficient for workspace seeding
- **WHEN** the `default` tenant default workspace has one or more valid
  skill directories under `skills`
- **AND** the source workspace manifest is absent or stale
- **THEN** the system SHALL still treat the source workspace as seedable
  after reconciling source state from disk

#### Scenario: Workspace durable fields are preserved
- **WHEN** a copied source workspace skill has durable state fields
  `enabled`, `channels`, `config`, or `source`
- **THEN** the target workspace manifest SHALL preserve those fields
  after reconciliation

#### Scenario: Existing target workspace skill state is not overwritten
- **WHEN** runtime bootstrap runs for a tenant whose default workspace
  already has initialized workspace skill state
- **THEN** the system SHALL NOT overwrite or recopy that workspace state

### Requirement: Runtime bootstrap keeps skill seeding bounded to tenant readiness
The system SHALL use first-access skill seeding to make a tenant ready
for use without expanding request-path bootstrap into full runtime
initialization.

#### Scenario: Runtime bootstrap does not create QA agent
- **WHEN** runtime bootstrap runs for a newly accessed tenant
- **THEN** the system SHALL NOT create the builtin QA agent as part of
  that bootstrap

#### Scenario: Runtime bootstrap does not start workspace runtime
- **WHEN** runtime bootstrap runs for a newly accessed tenant
- **THEN** the system SHALL NOT start the workspace runtime as part of
  that bootstrap

#### Scenario: Concurrent first access seeds once per tenant
- **WHEN** multiple requests concurrently trigger first-access bootstrap
  for the same tenant
- **THEN** the system SHALL serialize bootstrap for that tenant so
  seeding happens once without conflicting writes
