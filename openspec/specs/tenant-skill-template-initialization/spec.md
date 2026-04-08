# tenant-skill-template-initialization Specification

## Purpose

This specification defines how tenant skill state is initialized from the default tenant's templates during full tenant initialization, ensuring new tenants inherit configured skills without requiring manual setup.

## Requirements

### Requirement: Full tenant initialization seeds skill pool from default tenant
The system SHALL seed a new tenant's `skill_pool` from the `default` tenant during full tenant initialization when the target tenant has no existing skill-pool state.

#### Scenario: Full initialization copies default tenant skill pool
- **WHEN** full tenant initialization runs for tenant `tenant-a`
- **AND** `WORKING_DIR/default/skill_pool` contains initialized skill-pool content
- **AND** `WORKING_DIR/tenant-a/skill_pool` does not yet exist or contain initialized skill-pool state
- **THEN** the system SHALL copy the default tenant's skill-pool content into `WORKING_DIR/tenant-a/skill_pool`
- **AND** the target tenant's skill-pool manifest SHALL be reconciled against the copied filesystem state

#### Scenario: Full initialization falls back when default tenant has no skill pool
- **WHEN** full tenant initialization runs for tenant `tenant-a`
- **AND** the `default` tenant has no initialized skill-pool content to copy
- **THEN** the system SHALL initialize `tenant-a`'s skill pool using the existing builtin-skill initialization behavior

#### Scenario: Existing tenant skill pool is not overwritten
- **WHEN** full tenant initialization runs for tenant `tenant-a`
- **AND** `tenant-a` already has initialized skill-pool state
- **THEN** the system SHALL NOT overwrite or recopy the tenant's existing skill-pool content

### Requirement: Full tenant initialization seeds default workspace skills from default tenant
The system SHALL seed a new tenant's default workspace skill set from the `default` tenant's default workspace during full tenant initialization when the target default workspace has no existing skill state.

#### Scenario: Full initialization copies default workspace skills
- **WHEN** full tenant initialization runs for tenant `tenant-a`
- **AND** `WORKING_DIR/default/workspaces/default/skills` contains one or more skills
- **AND** `WORKING_DIR/tenant-a/workspaces/default` has no initialized workspace skill state
- **THEN** the system SHALL copy the source skill directories into `WORKING_DIR/tenant-a/workspaces/default/skills`
- **AND** the target workspace skill manifest SHALL be reconciled from the copied filesystem state

#### Scenario: Workspace manifest behavior is preserved from source defaults
- **WHEN** full tenant initialization seeds default workspace skills from the `default` tenant
- **AND** the source workspace manifest declares skill state such as `enabled`, `channels`, `config`, or `source`
- **THEN** the target workspace manifest SHALL preserve those behaviorally relevant fields for corresponding copied skills after reconciliation

#### Scenario: Existing workspace skills are not overwritten
- **WHEN** full tenant initialization runs for tenant `tenant-a`
- **AND** `tenant-a`'s default workspace already contains initialized workspace skill state
- **THEN** the system SHALL NOT overwrite or recopy the workspace skill content or manifest state

### Requirement: Tenant runtime bootstrap does not initialize tenant skills
The backend SHALL keep tenant runtime bootstrap focused on minimal workspace readiness and MUST NOT initialize or copy tenant skill state as part of request-path bootstrap.

#### Scenario: Runtime bootstrap succeeds without skill pool initialization
- **WHEN** tenant runtime bootstrap completes for a tenant that has never undergone full initialization
- **THEN** the tenant SHALL be considered bootstrapped even if its `skill_pool` has not been initialized or copied from the default tenant

#### Scenario: Runtime bootstrap succeeds without workspace skill initialization
- **WHEN** tenant runtime bootstrap completes for a tenant whose default workspace has no initialized skills
- **THEN** the tenant SHALL be considered bootstrapped even if `workspaces/default/skills` is empty and no workspace skill manifest seeding has occurred
