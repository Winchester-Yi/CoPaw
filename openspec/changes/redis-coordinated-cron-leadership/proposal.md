## Why

SWE currently persists cron definitions in shared `jobs.json` files, but each instance that loads a workspace starts its own local scheduler. In a multi-instance deployment with shared NAS this causes duplicate cron and heartbeat execution, which is the first backend conflict that must be resolved before broader multi-instance support can be trusted.

## What Changes

- Add Redis-coordinated leader election for cron ownership at the `tenant + agent` level.
- Ensure only the current leader instance activates `APScheduler` and runs scheduled cron jobs or heartbeat jobs for that agent workspace.
- Add a Redis-backed execution lock for timed cron triggers so transient leader overlap does not produce duplicate scheduled executions.
- Keep `jobs.json` as the stage-one shared cron configuration store on NAS, but require cron mutations to publish a Redis reload signal so the current leader rebuilds its local schedule.
- Preserve existing manual `run_job` behavior as an extra one-shot execution that does not participate in the timed execution lock.
- Avoid frontend, session-state, approval, and non-cron connector changes in this iteration.

## Capabilities

### New Capabilities
- `redis-coordinated-cron-leadership`: Coordinate cron and heartbeat scheduling through Redis so a multi-instance deployment has a single active scheduler per tenant-agent workspace and de-duplicates timed cron execution during failover edges.

### Modified Capabilities
- None.

## Impact

- Affected backend cron lifecycle: `src/swe/app/crons/manager.py`, `src/swe/app/crons/executor.py`
- Affected workspace startup ownership: `src/swe/app/workspace/workspace.py`
- Affected cron persistence/write paths: `src/swe/app/crons/repo/json_repo.py`, cron API/mutation entrypoints
- New external dependency and runtime requirement: Redis for cron coordination
- Shared NAS `jobs.json` remains in use for this stage; no frontend or general request-routing behavior changes are required
