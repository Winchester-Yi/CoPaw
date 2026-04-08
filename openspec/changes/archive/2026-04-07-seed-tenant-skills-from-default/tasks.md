## 1. Tenant-aware skill pool foundations

- [x] 1.1 Add `working_dir` support to shared skill-pool manifest helpers so read and reconcile operations can target a specific tenant directory.
- [x] 1.2 Add tests proving tenant-aware pool helpers operate on the requested tenant rather than the global working directory.

## 2. Seed tenant skill state from the default tenant

- [x] 2.1 Implement an idempotent skill-pool seed helper that copies `default/skill_pool` into a new tenant when target pool state is absent and falls back to builtin initialization when no template exists.
- [x] 2.2 Implement an idempotent default-workspace skill seed helper that copies `default/workspaces/default/skills`, reconciles the target manifest, and preserves source manifest fields `enabled`, `channels`, `config`, and `source`.
- [x] 2.3 Add unit tests covering successful seeding, fallback behavior, and skip-without-overwrite behavior for both pool and workspace skill state.

## 3. Wire full tenant initialization to use skill templates

- [x] 3.1 Extend `TenantInitializer` with explicit full-initialization steps for skill-pool and default-workspace skill seeding while keeping `initialize_minimal()` unchanged.
- [x] 3.2 Update CLI tenant initialization to use the new full-initialization path without changing builtin QA agent setup semantics.
- [x] 3.3 Add tests confirming full initialization seeds from the default tenant and runtime lazy bootstrap still avoids skill initialization.
