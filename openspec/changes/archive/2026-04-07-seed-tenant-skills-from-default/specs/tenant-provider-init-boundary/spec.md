## ADDED Requirements

### Requirement: Tenant runtime bootstrap does not initialize tenant skills
The backend SHALL keep tenant runtime bootstrap focused on minimal workspace readiness and MUST NOT initialize or copy tenant skill state as part of request-path bootstrap.

#### Scenario: Runtime bootstrap succeeds without skill pool initialization
- **WHEN** tenant runtime bootstrap completes for a tenant that has never undergone full initialization
- **THEN** the tenant SHALL be considered bootstrapped even if its `skill_pool` has not been initialized or copied from the default tenant

#### Scenario: Runtime bootstrap succeeds without workspace skill initialization
- **WHEN** tenant runtime bootstrap completes for a tenant whose default workspace has no initialized skills
- **THEN** the tenant SHALL be considered bootstrapped even if `workspaces/default/skills` is empty and no workspace skill manifest seeding has occurred
