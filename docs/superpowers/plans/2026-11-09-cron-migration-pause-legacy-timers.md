# Pause Legacy Timers During Cron Migration

> **For agentic workers:** Follow the repository's sequential execution convention. Use test-driven development and verify the real migration store with a simulated external scheduler.

**Goal:** Prevent new platform-triggered runs from both generations after a selected group is migrated.

**Architecture:** Use the old ordinary and batch IDs from the authorized, frozen snapshot. Prepare the whole new group with callbacks gated; pause its old timers; then activate and publish the new group.

**Spec:** Existing selected migration with the requested legacy platform pause. Do not delete timers, change old task data, or cancel old executions. On failure keep the new group gated and do not re-enable old timers automatically.

### Task: Extend selected migration cutover

- [ ] In `tests/unit/scheduler/test_selected_migration.py`, verify preview makes no platform calls, old ordinary and batch IDs are paused before the first new enable, a failed pause blocks publication, and retry reuses registered new IDs.
- [ ] Run `pytest -q tests/unit/scheduler/test_selected_migration.py -k legacy_timer`; expect the new behavior assertions to fail before implementation.
- [ ] In `scheduler/src/scheduler/app/services/cron/managed/selected_migration.py`, call `set_enabled(old_id, False)` for known legacy timers after preparation and before activation. Renew the lease before each call and surface a retryable group error on failure.
- [ ] Update the confirmation copy and `docs/deployment/cron_selected_migration.md`. Re-run selected migration, platform client and Console migration modal tests. Leave changes uncommitted for review.
