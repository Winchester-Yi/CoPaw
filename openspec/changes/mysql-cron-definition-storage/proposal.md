## Why

Cron definitions are still stored in shared `jobs.json` files on NFS, which means multi-instance reads share visibility but not safe mutation semantics. Even with Redis-coordinated leader election, job definitions remain vulnerable to concurrent overwrite and weak durability if the authoritative store stays file-based.

## What Changes

- Replace `jobs.json` as the authoritative cron definition store with MySQL-backed repositories.
- Align cron mutation and reload flows with durable database-backed job definitions.
- Keep the existing Redis cron leadership change focused on scheduler ownership while this change addresses durable cron definition storage.
- Add migration and rollout safeguards for existing JSON-backed job definitions.

## Capabilities

### New Capabilities
- `mysql-cron-definition-storage`: Store authoritative cron and heartbeat job definitions in MySQL so multi-instance mutation and scheduling read paths are consistent.

### Modified Capabilities
- None.

## Impact

- Affected modules: `src/swe/app/crons/repo/json_repo.py`, cron API mutation/read paths, cron manager reload wiring
- New dependency usage: MySQL becomes authoritative for cron definition storage
- Companion relationship with `redis-coordinated-cron-leadership`: lease ownership remains there, durable definitions move here
