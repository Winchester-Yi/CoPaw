## Why

Console push delivery is currently stored in per-process memory, so messages written by one pod are invisible to another pod that serves the user's polling request. This makes cron feedback, proactive console sends, and error pushes the fastest user-visible multi-instance inconsistency.

## What Changes

- Replace the in-memory console push store with Redis-backed tenant-and-session scoped delivery.
- Update console push writers so cron, console channel sends, and other producers publish into shared delivery state.
- Update the polling API to consume shared push messages regardless of which backend instance handles the read.
- Preserve tenant and session scoping, bounded retention, and expiring short-lived messages.

## Capabilities

### New Capabilities
- `redis-console-push-delivery`: Deliver console push messages through Redis so writes and reads remain consistent across backend instances.

### Modified Capabilities
- None.

## Impact

- Affected modules: `src/swe/app/console_push_store.py`, `src/swe/app/routers/console.py`, `src/swe/app/channels/console/channel.py`, `src/swe/app/crons/manager.py`
- New dependency usage: Redis is required for authoritative console push delivery
- Removes pod-local memory as the source of truth for `/api/console/push-messages`
