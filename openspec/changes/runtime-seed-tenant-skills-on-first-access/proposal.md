## Why

New tenants currently inherit the default tenant's skills only through
explicit full initialization paths such as `swe init --tenant-id ...`.
That does not match the operational requirement that a newly accessed
tenant should immediately receive the default tenant's skill baseline
on first access.

The current seeding implementation also has two functional gaps:
- source template discovery incorrectly depends on manifest presence, so
  default-tenant skill directories on disk are ignored when the
  manifest is absent or stale
- pool-level durable config is not preserved when seeding from the
  default tenant

## What Changes

- Extend runtime tenant bootstrap so first access performs one-time
  skill seeding in addition to minimal directory bootstrap.
- Add a runtime-safe tenant initialization path that:
  - ensures tenant directory structure
  - ensures the default agent declaration
  - seeds `skill_pool` from the default tenant, with builtin fallback
    when no default template exists
  - seeds `workspaces/default/skills` from the default tenant's default
    workspace
- Keep runtime bootstrap lightweight in scope:
  - do seed files and manifests
  - do not start workspace runtime
  - do not create the builtin QA agent
- Treat source filesystem skill directories as the source of truth for
  template availability; manifests are used only for durable state
  merge.
- Preserve durable state during seeding:
  - pool: `config`
  - workspace: `enabled`, `channels`, `config`, `source`
- Preserve idempotency: once a tenant has local skill state, later
  bootstrap calls must not overwrite it.
- Reuse the same seeding logic in CLI full initialization, with QA agent
  creation remaining full-init only.

## Capabilities

### New Capabilities
- `runtime-tenant-skill-template-bootstrap`: Seed a new tenant's skill
  pool and default workspace skills from the `default` tenant during the
  first successful runtime bootstrap.

### Modified Capabilities
- `tenant-provider-init-boundary`: Runtime bootstrap semantics change
  from minimal directory-only bootstrap to tenant-readiness bootstrap
  that may seed skills, while still not starting runtime or creating the
  QA agent.

## Impact

- Affected code:
  - `src/swe/app/workspace/tenant_pool.py`
  - `src/swe/app/workspace/tenant_initializer.py`
  - `src/swe/agents/skills_manager.py`
  - `src/swe/cli/init_cmd.py`
- Affected behavior:
  - first request bootstrap for new tenants
  - `swe init --tenant-id ...`
- Unchanged behavior:
  - runtime bootstrap still does not start workspace runtime
  - runtime bootstrap still does not create QA agent
- Testing impact:
  - tenant bootstrap tests
  - lazy-loading boundary tests
  - tenant skill seeding tests
  - concurrency and idempotency coverage
