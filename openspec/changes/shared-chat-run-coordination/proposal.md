## Why

Interactive chat runs are currently owned only by the pod-local `TaskTracker`, which makes stop requests, reconnect discovery, and running-status reads incorrect whenever follow-up requests land on a different pod. This is the highest-impact runtime inconsistency after push delivery.

## What Changes

- Add Redis-backed shared coordination for interactive chat run ownership and liveness.
- Route stop requests through shared cancellation signaling so they work from any backend instance.
- Make chat running-status queries and reconnect discovery read shared run state instead of only local memory.
- Keep local task execution on the owner pod while removing local memory as the cross-instance source of truth.

## Capabilities

### New Capabilities
- `shared-chat-run-coordination`: Coordinate interactive chat run ownership, liveness, stop semantics, and reconnect discovery through Redis-backed shared runtime state.

### Modified Capabilities
- None.

## Impact

- Affected modules: `src/swe/app/runner/task_tracker.py`, `src/swe/app/routers/console.py`, `src/swe/app/runner/api.py`, `src/swe/app/channels/base.py`
- New dependency usage: Redis becomes authoritative for shared run ownership and cancellation signaling
- Leaves streaming execution local, but removes local-only ownership as the source of truth for status and control APIs
