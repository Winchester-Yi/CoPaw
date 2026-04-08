## Why

New tenants currently bootstrap only the minimal workspace structure and do not inherit the default tenant's skill pool or default workspace skill set. This makes tenant onboarding inconsistent with provider initialization and forces manual skill setup even when the default tenant already represents the desired baseline.

## What Changes

- Add tenant full-initialization behavior that seeds a new tenant's `skill_pool` from the `default` tenant when the target tenant has not initialized its own skill pool yet.
- Add tenant full-initialization behavior that seeds a new tenant's `workspaces/default/skills` and workspace skill manifest state from the `default` tenant's default workspace when the target workspace has no skills yet.
- Keep runtime minimal bootstrap unchanged: request-path bootstrap remains limited to directory structure and default agent declaration and MUST NOT initialize or copy skills.
- Make shared skill-pool manifest operations tenant-aware so full initialization and later reconciliation operate on the intended tenant directory.
- Preserve idempotency: if a tenant already has a skill pool or workspace skills, initialization skips seeding and does not overwrite tenant-local customizations.

## Capabilities

### New Capabilities
- `tenant-skill-template-initialization`: Initialize a new tenant's skill pool and default workspace skills from the `default` tenant during full tenant initialization.

### Modified Capabilities
- `tenant-provider-init-boundary`: Clarify that tenant runtime bootstrap remains minimal and does not imply skill initialization, mirroring the existing provider-boundary rule.

## Impact

- Affected code: `src/swe/app/workspace/tenant_initializer.py`, `src/swe/cli/init_cmd.py`, `src/swe/agents/skills_manager.py`
- Affected behavior: `swe init --tenant-id ...` and any future full tenant initialization path
- Unchanged behavior: `TenantWorkspacePool.ensure_bootstrap()` request-path lazy bootstrap
- Testing impact: tenant initializer tests, skills manager tests, and lazy-loading boundary tests
