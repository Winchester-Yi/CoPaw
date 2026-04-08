## MODIFIED Requirements

### Requirement: Tenant workspace bootstrap remains workspace-scoped
The backend SHALL keep tenant workspace bootstrap focused on workspace
concerns and MUST NOT treat workspace bootstrap as evidence that
provider storage has already been initialized. Runtime bootstrap MAY
seed tenant skill state as part of workspace readiness, but MUST NOT
expand that bootstrap into provider initialization, runtime startup, or
builtin QA agent creation.

#### Scenario: Workspace bootstrap succeeds without provider readiness
- **WHEN** tenant workspace bootstrap completes for a tenant that has
  never used provider features
- **THEN** the tenant workspace SHALL be considered bootstrapped even if
  tenant provider storage does not exist yet

#### Scenario: Provider readiness is established independently from workspace bootstrap
- **WHEN** a tenant has already been workspace-bootstrapped but later
  accesses a provider feature for the first time
- **THEN** the backend SHALL initialize tenant provider storage at that
  provider feature boundary rather than requiring a new workspace
  bootstrap step

#### Scenario: Workspace bootstrap may seed skills without changing provider boundary
- **WHEN** runtime workspace bootstrap seeds tenant-local skill state for
  a newly accessed tenant
- **THEN** that seeding SHALL NOT imply tenant provider storage has been
  initialized
- **AND** provider storage SHALL still be initialized only at provider
  feature boundaries
