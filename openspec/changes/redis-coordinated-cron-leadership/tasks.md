## 1. Add Redis cron coordination primitives

- [ ] 1.1 Introduce a Redis-backed coordination module for tenant-agent cron lease acquisition, renewal, release, and reload pub/sub
- [ ] 1.2 Add timed job execution lock support for scheduler-originated cron triggers with TTL derived from cron timeout configuration
- [ ] 1.3 Add configuration plumbing and dependency wiring for Redis cron coordination without changing non-cron runtime ownership

## 2. Refactor cron lifecycle around active leadership

- [ ] 2.1 Refactor `CronManager` to support passive initialization plus explicit activate, deactivate, and reload-from-repo transitions
- [ ] 2.2 Add a workspace-scoped cron leadership controller that elects leadership per `tenant + agent` and drives `CronManager` activation state
- [ ] 2.3 Ensure heartbeat scheduling is owned by the active leader only and stops promptly when lease ownership is lost

## 3. Wire cron mutation and execution semantics

- [ ] 3.1 Update cron mutation paths to publish Redis reload signals only after successful `jobs.json` writes
- [ ] 3.2 Route timed scheduler callbacks through the execution lock while preserving manual `run_job` as an explicit extra execution
- [ ] 3.3 Add logging and state handling for lease loss, follower passivity, skipped duplicate timed runs, and leader-triggered reloads

## 4. Verify multi-instance cron behavior

- [ ] 4.1 Add unit tests for lease election, renewal failure, activation/deactivation, and reload debounce behavior
- [ ] 4.2 Add unit tests for timed execution lock semantics and manual `run_job` bypass behavior
- [ ] 4.3 Add integration tests covering two-instance leadership, failover, timed de-duplication, and reload propagation from a follower mutation
