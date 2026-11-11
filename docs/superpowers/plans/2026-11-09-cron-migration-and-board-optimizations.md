# Cron Migration and Board Optimizations

**Goal:** Deliver the seven approved changes without including the separate retirement draft.

**Constraints:** No changes to the old definition table for migration, no new chat IDs, no production changes, no overwriting unrelated changes. Preserve existing API authorization and batch scheduling semantics.

1. In SWE selected migration source/API, compare the old projections and files, return field-level warnings, retain file binding and selected parent. Keep Scheduler publish collision checks. Test read-only snapshots and conflicting existing new records.
2. After new timer preparation, stop old file-based recovery and known old timers; preserve original snapshot and wait for old in-flight/queued work before disabling old definitions. Use the migration journal to hide only completed jobs. Test restart, partial failure and retry.
3. Add channel-manager list/submit/progress entrypoints, authorizing the source server-side and keeping operation targets/actor separate. Add the Console administration page and test source isolation and mixed tenant progress.
4. In managed list, batch-read parent identities from the existing parent index for the page, including logically deleted child history. Render no parent tag when absent. Test a parent, child and never-distributed task.
5. Extend existing failure-summary aggregation with the current execution history error type only for child failures. Keep existing detail counts and add the breakdown in the hover. Test missing type and superseded attempts.
6. In the shared model pool, distinguish idle-reset and initial capacity from tuned capacity and use the new base only on the next start from baseline. Test both lease-protected state transitions, active work and below-base tuning.
7. Add an on-demand member attempt query in Monitor constrained by source, batch and member; follow trusted retry lineage and join the matching execution histories. Test no request at zero, history on click, and rejection of unrelated attempts.
