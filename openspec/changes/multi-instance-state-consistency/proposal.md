## Why

SWE now runs with shared Redis, shared NFS, and multiple stateless pods, but critical runtime state still lives in pod memory and critical control state still lives in shared JSON files. Sticky session can reduce how often users notice this, but it does not make cross-instance behavior correct for reconnect, stop, push delivery, or concurrent state mutation.

## What Changes

- Add Redis-backed coordination for interactive runtime state, including shared run ownership, shared cancellation signaling, shared push delivery, and shared reconnect-visible status.
- Move authoritative chat, job, and run control records from NFS JSON files to durable database-backed repositories so concurrent mutations do not overwrite one another.
- Define a strict storage boundary: Redis for coordination and short-lived runtime state, MySQL for durable fact state, and NFS for file assets and workspace documents only.
- Keep existing single-instance runtime objects as local execution caches only; they MUST NOT remain the source of truth for cross-instance behavior.
- Align future cron persistence with the broader multi-instance state model while treating the existing `redis-coordinated-cron-leadership` change as a companion change rather than re-specifying its scheduler lease behavior here.

## Capabilities

### New Capabilities
- `distributed-interactive-runtime-state`: Coordinate interactive chat runs, stop requests, reconnect visibility, and console push delivery through shared cross-instance runtime state.
- `durable-shared-control-state`: Persist chat, cron, and run control records in durable transactional storage instead of shared JSON files on NFS.

### Modified Capabilities
- None.

## Impact

- Affected runtime state paths: `src/swe/app/runner/task_tracker.py`, `src/swe/app/routers/console.py`, `src/swe/app/runner/api.py`, `src/swe/app/channels/base.py`, `src/swe/app/console_push_store.py`
- Affected durable state paths: `src/swe/app/runner/repo/json_repo.py`, `src/swe/app/crons/repo/json_repo.py`, session persistence and workspace service factories
- New or expanded infrastructure assumptions: Redis becomes required for shared runtime coordination; MySQL becomes the authoritative store for chat/job/run control records
- NFS remains in use for file assets, uploads, workspace documents, and other non-transactional file content, but no longer for authoritative high-churn control state

## Roadmap Summary

This umbrella change is intentionally split into smaller implementation changes so rollout can follow user-visible risk and data-safety priorities instead of attempting one unsafe migration.

Recommended rollout order:

1. `redis-console-push-delivery`
2. `shared-chat-run-coordination`
3. `mysql-chat-control-storage`
4. `mysql-cron-definition-storage` together with `redis-coordinated-cron-leadership`
5. `coordinated-session-state-persistence`

The sequencing logic is:

- First remove the most obvious runtime inconsistency users see immediately: cross-pod push delivery failure.
- Then fix interactive control semantics: stop, running-status visibility, and reconnect discovery.
- Next move durable chat and cron control facts out of shared JSON files and into MySQL.
- Finally remove the most invasive and subtle overwrite risk: shared session checkpoint persistence.

Each sub-change is independently reviewable and implementable, but together they converge on one storage contract:

- Redis for cross-instance runtime coordination
- MySQL for durable control facts
- NFS for file assets and workspace documents only
