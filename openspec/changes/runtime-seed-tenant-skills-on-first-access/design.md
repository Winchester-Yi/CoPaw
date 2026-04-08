## Context

The system currently separates runtime tenant bootstrap from full tenant
initialization. Runtime bootstrap, used on first request, ensures only
directory structure and default agent presence. Full initialization,
used by `swe init`, additionally initializes skill state and the
builtin QA agent.

That split no longer matches the operational requirement: a newly
accessed tenant must immediately inherit the default tenant's skill
baseline without requiring a prior explicit CLI initialization step.

Skills exist in two layers:
- shared tenant `skill_pool/`
- workspace-local `workspaces/<agent>/skills/`

The existing seeding work introduced tenant-aware manifest helpers and
full-init seeding, but it still assumes:
- runtime bootstrap must never seed skills
- source manifests are required to detect seedable defaults
- pool durable config does not need to be copied

Those assumptions conflict with the actual requirement.

## Goals / Non-Goals

**Goals:**
- Seed skill state during first runtime access for previously
  uninitialized tenants.
- Keep runtime bootstrap limited to bootstrap concerns only:
  - no runtime start
  - no QA agent creation
- Use on-disk source skill directories as template truth.
- Preserve durable state from the default tenant.
- Keep seeding idempotent and safe under concurrent first access.
- Reuse the same seeding behavior from CLI full initialization.

**Non-Goals:**
- Ongoing synchronization from the default tenant after first
  initialization.
- Runtime creation of the builtin QA agent.
- Starting workspace runtime during bootstrap.
- Generalizing inheritance to arbitrary future workspace creation beyond
  tenant bootstrap.

## Decisions

### Decision: first-access bootstrap performs one-time skill seeding

`TenantWorkspacePool.ensure_bootstrap()` will call a new
`TenantInitializer.ensure_seeded_bootstrap()` instead of only
`initialize_minimal()`.

Why:
- This is the actual entrypoint for newly accessed tenants.
- It keeps tenant readiness logic centralized.

Alternatives considered:
- Keep seeding CLI-only: rejected because it does not satisfy first
  access requirements.
- Seed in middleware after bootstrap: rejected because it splits tenant
  readiness across multiple request-path layers.

### Decision: runtime seeding excludes QA agent creation

Runtime bootstrap will seed only the tenant skill pool and the tenant
default workspace skill set.

Why:
- QA agent creation is not required for ordinary tenant readiness.
- It would unnecessarily increase first-request work and side effects.

Alternatives considered:
- Reuse full initialization unchanged: rejected because it would create
  the QA agent during request-path bootstrap.

### Decision: source filesystem content determines whether a template exists

Before copying, the default tenant source pool and source default
workspace will be reconciled from disk. A missing manifest alone must
not disable seeding.

Why:
- Actual templates are directories containing `SKILL.md`.
- Manifests may be absent, stale, or not yet generated.

Alternatives considered:
- Require source manifests: rejected because it misses valid on-disk
  templates and recreates the current bug.

### Decision: seed by copy, then reconcile, then merge durable state

For both pool and workspace seeding:
1. reconcile source from disk
2. copy discovered skill directories
3. reconcile target from disk
4. merge durable manifest state from source

Why:
- Copy preserves content.
- Reconcile regenerates metadata for the target tenant.
- Merge preserves behaviorally relevant state without copying stale
  metadata wholesale.

Alternatives considered:
- Copy source manifests verbatim: rejected because paths, timestamps,
  and derived metadata should be regenerated for the target tenant.

### Decision: preserve durable state for both layers

- Pool layer preserves `config`
- Workspace layer preserves `enabled`, `channels`, `config`, `source`

Why:
- These fields define effective behavior and tenant baseline semantics.

Alternatives considered:
- Preserve workspace state only: rejected because pool-level config is
  part of the expected inherited baseline.

### Decision: pool seeding failure falls back to builtin initialization

If the default tenant has no usable pool template, runtime bootstrap
initializes builtin pool skills instead. If source copy or reconcile
fails, bootstrap logs a warning and falls back to builtin
initialization.

Why:
- A new tenant must still be functional without a prepared default
  tenant template.

Alternatives considered:
- Fail the request when default seeding fails: rejected because it turns
  optional template inheritance into a hard availability dependency.

### Decision: workspace seeding failure is non-fatal but logged

If workspace seeding fails due to IO or manifest issues, tenant
bootstrap still completes, but a warning is logged.

Why:
- Tenant accessibility should not be blocked by non-critical default
  workspace template failure.
- Operators still need observability.

Alternatives considered:
- Fail bootstrap when workspace seeding fails: rejected because it is
  too expensive for request-path resilience.

### Decision: existing tenant bootstrap lock remains the once-only guard

The existing per-tenant lock in `TenantWorkspacePool.ensure_bootstrap()`
remains the concurrency boundary for first-access seeding.

Why:
- It already serializes bootstrap for a tenant.
- It prevents duplicate copy and reconcile work under concurrent first
  requests.

Alternatives considered:
- Add a second seed-only lock: rejected because it adds complexity
  without reducing contention.

## Risks / Trade-offs

- [First request becomes heavier than pure directory bootstrap] →
  Exclude QA-agent creation and runtime startup from this path, and keep
  seeding strictly one-time and idempotent.
- [Partial copy may leave mixed state after an exception] → Reconcile the
  target on retry and use non-overwrite checks based on actual disk and
  manifest state.
- [Operators may expect later default-tenant edits to propagate] →
  Document this as one-time initialization, not ongoing synchronization.
- [Broad exception swallowing could hide broken initialization] →
  Replace silent catches with warning-level logging and explicit
  fallback behavior.

## Migration Plan

1. Add `ensure_seeded_bootstrap()` in `TenantInitializer`.
2. Update runtime bootstrap to call the new path.
3. Fix source discovery to reconcile source from disk before deciding
   whether to seed.
4. Add pool durable-state merge for `config`.
5. Reuse the new runtime-safe seeding path from `initialize_full()`.
6. Add tests for first-access seeding, manifestless source templates,
   config preservation, idempotency, and concurrency.

Rollback is straightforward: restore runtime bootstrap to
`initialize_minimal()` and keep seeding only in explicit full-init
paths. Any tenants seeded by the new flow remain valid because the
resulting on-disk state matches normal tenant-local skill storage.

## Open Questions

- Should runtime bootstrap write an explicit tenant seed marker for
  observability, or should state remain fully inferred from disk and
  manifest content?
  Recommended answer: an optional marker is acceptable for observability,
  but correctness must not depend on it.
