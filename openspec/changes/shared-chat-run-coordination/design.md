## Context

Interactive chat execution currently depends on `TaskTracker`, which lives entirely in process memory. This makes three user-facing behaviors inconsistent across pods:
- `/api/console/chat/stop` only cancels tasks on the current pod
- `/api/chats` and `/api/chats/{chat_id}` report running state from the current pod only
- reconnect behavior can only find an existing run if the reconnect request lands on the original pod

This change addresses ownership and control semantics for interactive runs. It does not promise perfect token-stream continuation after owner pod loss; it makes shared state authoritative so the system can answer whether a run exists, who owns it, and how to stop it.

## Goals / Non-Goals

**Goals:**
- Make interactive run ownership visible across backend instances.
- Make stop requests work regardless of which instance receives them.
- Make chat status APIs read shared liveness rather than only local task state.
- Let reconnect requests discover an active run from shared state.

**Non-Goals:**
- Full replay of every streamed token after arbitrary pod failure.
- Moving long-lived chat history or metadata persistence to MySQL in this change.
- Reworking the queueing model for every channel beyond what is needed for shared run coordination.

## Decisions

### Decision 1: Use Redis as the authoritative run registry

**Choice:** Store shared interactive run ownership and liveness in Redis.

**Rationale:**
- Ownership heartbeats and cancellation are transient high-frequency coordination concerns.
- Any pod needs to answer whether a run is active and which pod owns it.
- Redis is a better fit for leases and cancel signaling than MySQL.

**Alternatives considered:**
- Keep ownership only in local memory: rejected because it is the current failure mode.
- Store ownership only in MySQL: workable but unnecessarily heavy for liveness and lease updates.

### Decision 2: Separate shared coordination from local execution

**Choice:** Continue executing the actual task locally on the owner pod, but treat Redis as the authoritative cross-instance coordination plane.

**Rationale:**
- This minimizes the change surface and avoids redesigning stream execution itself in the first step.
- It gives the system a shared control plane without forcing immediate distributed execution.

**Alternatives considered:**
- Fully distributed execution handoff: too large for this step.
- Keep all semantics local: fails the multi-instance requirement.

### Decision 3: Stop uses shared cancel signaling

**Choice:** A stop request writes shared cancel intent that the owner pod must observe and honor.

**Rationale:**
- Stop must not depend on reaching the owner pod directly.
- Shared cancel signaling decouples the control request from the execution location.

**Alternatives considered:**
- Proxy stop to the owner pod over an internal RPC path: adds more moving parts than needed for the first coordination step.

## Risks / Trade-offs

- [Redis unavailable] -> Shared stop/status semantics cannot remain correct; fail explicitly rather than silently reporting local-only truth.
- [Owner pod dies mid-run] -> Shared state can show the run as lost or expired, but full continuation is not guaranteed in this change.
- [Lease expiration tuning] -> Heartbeat TTLs that are too short flap ownership; TTLs that are too long delay stale-owner cleanup.

## Migration Plan

1. Introduce Redis-backed run ownership, heartbeat, and cancel primitives.
2. Update task startup and teardown paths to register shared ownership.
3. Update stop, status, and reconnect discovery paths to read shared coordination state.
4. Add tests for cross-instance status and stop behavior.

## Open Questions

- Should reconnect in the first phase expose only active-run discovery, or also a shared event replay window?
- Should owner identity be stored as pod name, process ID, or an abstract runtime instance ID?
