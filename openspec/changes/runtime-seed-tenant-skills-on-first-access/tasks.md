## 1. Runtime bootstrap entrypoint

- [x] 1.1 Add `TenantInitializer.ensure_seeded_bootstrap()` to perform
  minimal tenant bootstrap plus one-time skill seeding without creating
  the QA agent.
- [x] 1.2 Update
  `src/swe/app/workspace/tenant_pool.py` to call
  `ensure_seeded_bootstrap()` from `ensure_bootstrap()` under the
  existing per-tenant lock.
- [x] 1.3 Add or update tests proving first access seeds tenant skills
  without starting runtime or creating the QA agent.

## 2. Skill-pool seeding semantics

- [x] 2.1 Change pool seeding to reconcile the default tenant source from
  disk before checking template availability.
- [x] 2.2 Seed from source skill directories even when the source pool
  manifest does not exist yet.
- [x] 2.3 Preserve pool durable `config` when copying skills from the
  default tenant.
- [x] 2.4 Fallback to builtin pool initialization only when no usable
  default template exists or when source copy or reconcile fails.
- [x] 2.5 Add tests for manifestless source templates, config
  preservation, builtin fallback, and non-overwrite idempotency.

## 3. Default-workspace seeding semantics

- [x] 3.1 Change workspace seeding to reconcile the default tenant source
  workspace from disk before checking template availability.
- [x] 3.2 Seed from source workspace skill directories even when the
  source workspace manifest does not exist yet.
- [x] 3.3 Preserve `enabled`, `channels`, `config`, and `source` after
  target reconciliation.
- [x] 3.4 Add tests for manifestless source templates, durable-state
  preservation, and non-overwrite idempotency.

## 4. Full initialization and observability

- [x] 4.1 Update `TenantInitializer.initialize_full()` to reuse
  `ensure_seeded_bootstrap()` and then create the QA agent.
- [x] 4.2 Keep `swe init` output aligned with the new seeding result
  semantics.
- [x] 4.3 Replace broad silent exception swallowing in seeding helpers
  with explicit warning or error logging.
- [x] 4.4 Add tests proving CLI-oriented full initialization still
  creates the QA agent while runtime bootstrap does not.

## 5. Concurrency and regression coverage

- [x] 5.1 Add concurrency tests showing simultaneous first access seeds
  once per tenant without conflicting writes.
- [x] 5.2 Re-run lazy-loading boundary tests to confirm runtime bootstrap
  still does not start workspace runtime.
- [x] 5.3 Re-run tenant skill seeding tests to confirm new first-access
  behavior and repaired edge cases.
