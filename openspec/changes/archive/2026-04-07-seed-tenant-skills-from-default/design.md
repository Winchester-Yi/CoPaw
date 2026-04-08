## Context

The codebase already separates tenant runtime bootstrap from full tenant initialization. Runtime bootstrap, reached through `TenantWorkspacePool.ensure_bootstrap()`, intentionally creates only tenant directory structure and the default agent declaration so request-path startup remains cheap. Full initialization, currently used by `swe init`, additionally creates the builtin QA workspace and initializes the tenant skill pool.

Skills are stored in two layers: tenant-shared `skill_pool/` and workspace-local `workspaces/<agent>/skills/`. Existing skill-pool initialization imports packaged builtin skills only. Existing workspace initialization copies skills from the pool only when explicit `skill_names` are provided, which today is used by the builtin QA agent but not by the normal default workspace.

The new requirement is to let a newly initialized tenant inherit the `default` tenant's skill baseline without changing the runtime lazy-bootstrap boundary. This is analogous to how provider configuration inheritance was introduced, but skills add an extra workspace-local layer and have manifest reconciliation logic that must remain tenant-scoped.

## Goals / Non-Goals

**Goals:**
- Seed a new tenant's `skill_pool` from `WORKING_DIR/default/skill_pool` during full tenant initialization when the target tenant has no skill pool state yet.
- Seed a new tenant's `workspaces/default/skills` and retained workspace skill manifest state from the `default` tenant's default workspace during full tenant initialization when the target workspace has no skills yet.
- Preserve runtime lazy bootstrap behavior so request-path bootstrap still avoids skill initialization and copying.
- Keep seeding idempotent: existing tenant-local skill state wins and is never overwritten by a later initialization call.
- Make pool manifest helpers tenant-aware so seed/reconcile logic always operates on the intended tenant directory.

**Non-Goals:**
- Changing how packaged builtin skills are imported into a tenant that has no default-tenant template available.
- Synchronizing later edits from the `default` tenant into already-initialized tenants.
- Changing QA agent skill seeding rules.
- Introducing runtime request-path skill auto-initialization.

## Decisions

### Decision: Restrict inheritance to full tenant initialization paths
Full tenant initialization will own skill inheritance. `TenantInitializer` will gain explicit full-initialization steps for skill-pool and default-workspace seeding, while `initialize_minimal()` remains unchanged.

**Why:** Existing tests and architecture define runtime bootstrap as a low-cost operation. Pulling skill copy/reconcile work into request-path bootstrap would violate that boundary and make tenant readiness imply more than workspace existence.

**Alternatives considered:**
- Seed during `TenantWorkspacePool.ensure_bootstrap()`: rejected because it breaks lazy-loading guarantees and increases first-request work.
- Seed on first skill access only: rejected for this change because the requirement is specifically about full tenant initialization behavior and predictable onboarding.

### Decision: Seed `skill_pool` before default workspace skills
Full initialization will initialize the tenant skill pool first, then seed the default workspace skills.

**Why:** The workspace layer conceptually depends on the tenant-local pool baseline. Seeding the pool first keeps initialization order aligned with the existing architecture and avoids a workspace referencing capabilities absent from the tenant-local pool.

**Alternatives considered:**
- Seed workspace directly without pool initialization ordering: rejected because it weakens the shared-pool-first model and makes future reasoning about workspace sources harder.

### Decision: Workspace skill inheritance copies files and rehydrates manifest state
For default workspace inheritance, the system will copy `skills/` directories from the default tenant's default workspace, run target workspace reconciliation, and then merge durable user-state fields such as `enabled`, `channels`, `config`, and `source` from the source manifest onto discovered target entries.

**Why:** Workspace manifests contain both filesystem-derived metadata and user-state. Blindly copying the full manifest risks stale metadata and path-derived drift. Reconcile-then-merge preserves real on-disk truth while carrying over the behaviorally relevant state.

**Alternatives considered:**
- Copy `skill.json` verbatim: rejected because metadata, timestamps, and discovered state should be regenerated from the target filesystem.
- Ignore manifest state and copy files only: rejected because it loses enabled/channel/config semantics that define the default workspace baseline.

### Decision: Add tenant-aware pool helper parameters instead of relying on global `WORKING_DIR`
Pool manifest read/reconcile helpers will accept an explicit `working_dir` so callers can operate on the correct tenant's pool during initialization and tests.

**Why:** Several skill-pool operations currently default to global `WORKING_DIR`, which is unsafe for tenant-specific seed flows and makes tests rely on implicit global context.

**Alternatives considered:**
- Temporarily mutate process/global context before reconcile: rejected because it is brittle and obscures which tenant is being operated on.

### Decision: Preserve idempotency by skipping seeded targets that already contain skill state
If the target tenant already has a pool manifest, pool skill directories, workspace skill directories, or an initialized workspace manifest with skill entries, seeding is skipped for that layer.

**Why:** Tenant-local customizations must not be overwritten by repeated init calls, partial retries, or operator reruns.

**Alternatives considered:**
- Re-copy from default tenant on every init: rejected because it destroys tenant isolation after first customization.

## Risks / Trade-offs

- **[Default tenant template is absent or incomplete]** → Fall back to existing builtin pool initialization and skip workspace seeding when the source workspace has no skills.
- **[Tenant-aware pool helpers are updated incompletely]** → Centralize `working_dir` plumbing in shared helper functions and add targeted tests for per-tenant reconcile/read behavior.
- **[Workspace manifest merge loses behaviorally important fields]** → Limit merges to explicit durable fields and validate via tests that enabled/channels/config/source survive seeding.
- **[Operators expect later default-tenant edits to propagate]** → Document that seeding is a one-time initialization action, not ongoing synchronization.
- **[Initialization ordering subtly changes existing CLI behavior]** → Keep QA agent initialization semantics unchanged and limit CLI flow changes to calling a new `initialize_full()` wrapper that preserves prior side effects.

## Migration Plan

1. Add tenant-aware helper parameters for pool manifest operations in the skills manager.
2. Add skill-pool and default-workspace seed helpers with idempotent copy/merge behavior.
3. Extend `TenantInitializer` with `ensure_skill_pool()`, `ensure_default_workspace_skills()`, and `initialize_full()`.
4. Update `swe init` to use `initialize_full()` while keeping QA agent initialization explicit.
5. Add tests covering seeded initialization, fallback behavior, idempotency, and unchanged lazy bootstrap boundaries.

Rollback is straightforward: revert the initialization path changes and helper additions. Existing tenants seeded by the new flow remain valid because the resulting on-disk structure matches normal tenant-local skill storage.

## Open Questions

- Should full initialization seed only the tenant `default` workspace, or should future agent creation also optionally inherit from the default tenant's default workspace template? This design scopes inheritance to tenant initialization only.
- Should pool seeding preserve pool manifest `config` entries exactly as stored by the default tenant, or should some fields be normalized on import? Current design preserves them through copy plus reconcile.
