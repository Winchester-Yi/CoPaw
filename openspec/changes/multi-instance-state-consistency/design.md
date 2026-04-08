## Context

The current backend contains two different classes of multi-instance inconsistency.

First, interactive runtime state is process-local. `TaskTracker`, `console_push_store`, `UnifiedQueueManager`, and workspace runtime caches all live inside a single pod's memory. As a result, `POST /api/console/chat`, reconnect flows, `POST /api/console/chat/stop`, `GET /api/console/push-messages`, and chat running-status reads only behave correctly when the user's follow-up requests land on the same pod that owns the work.

Second, several authoritative control records are still stored as shared JSON files on NFS. `jobs.json`, `chats.json`, and session JSON state are visible to every pod, but their repositories explicitly use single-file read-modify-write semantics without cross-process locking or transactional conflict control. Shared visibility therefore exists without correct concurrent mutation semantics.

The deployment context for this design is fixed: Kubernetes, multiple stateless backend pods, sticky session as a best-effort routing optimization, shared Redis, shared MySQL, and shared NFS. The design must therefore treat pod memory as ephemeral, NFS as a file asset store, Redis as the cross-instance coordination plane, and MySQL as the durable fact store.

The repository already contains a separate `redis-coordinated-cron-leadership` change that addresses single-active cron scheduling. This design is broader: it defines the rest of the state model required so interactive traffic and durable control state are also correct in a multi-instance deployment.

## Goals / Non-Goals

**Goals:**
- Make interactive chat behavior instance-agnostic for shared status, stop, reconnect discovery, and console push delivery.
- Eliminate shared JSON files on NFS as the authoritative source of high-churn control state.
- Define a storage contract that is simple to reason about: Redis for runtime coordination, MySQL for durable control state, NFS for files.
- Provide a phased migration path that can be rolled out incrementally without requiring a single risky cutover.
- Preserve tenant and session isolation while externalizing state.

**Non-Goals:**
- Delivering full in-flight token streaming continuity across arbitrary pod failover in the first migration step.
- Re-specifying Redis cron lease behavior already covered by the existing cron leadership change.
- Moving workspace documents, uploads, screenshots, or other file assets out of NFS.
- Rewriting every local cache into a distributed data structure; local caches may remain as execution-local helpers as long as they are not authoritative.
- Solving unrelated provider, approval, or frontend design concerns in this change.

## Decisions

### Decision 1: State placement follows semantics, not current module boundaries

**Choice:** Partition state into three authoritative classes:
- Redis for short-lived shared runtime coordination
- MySQL for durable control facts
- NFS for file assets and workspace documents only

**Rationale:**
- The current problems come from mixing these semantics: pod memory holds cross-instance runtime truth, and NFS files hold transactional control truth.
- The new boundary is easy to reason about operationally and aligns with the actual deployment topology.
- It also prevents the current anti-pattern where a module's internal storage detail accidentally becomes a correctness boundary.

**Alternatives considered:**
- Keep JSON/NFS as control-state storage and add locks: reduces some races, but still leaves poor queryability, weak migration semantics, and complex distributed write correctness.
- Put everything in Redis: fast for coordination, but weak as the durable source of record for chats, jobs, and historical run facts.
- Put everything in MySQL: durable, but a poor fit for high-frequency transient coordination like ownership heartbeats, cancel signals, and short-lived event buffers.

### Decision 2: Interactive run ownership is externalized through Redis

**Choice:** Redis becomes the authoritative coordination plane for interactive runs, including owner identity, run heartbeat, cancel signaling, and reconnect-visible runtime status.

**Rationale:**
- Stop, reconnect, and running-status APIs currently fail because they only inspect local process state.
- A shared run registry lets any pod answer "is this run active?" and "which pod currently owns execution?"
- Redis fits the lifecycle and write frequency of runtime leases and shared cancellation better than MySQL.

**Alternatives considered:**
- Keep ownership in local memory and rely on sticky sessions: rejected because it fails during reconnect, rollout, scale events, and non-browser follow-up requests.
- Store ownership only in MySQL: possible, but unnecessarily heavy for high-frequency lease renewal and cancellation signaling.

### Decision 3: Console push delivery moves from pod memory to Redis

**Choice:** Replace the in-memory `console_push_store` with Redis-backed per-tenant, per-session message delivery.

**Rationale:**
- Push delivery is the most obvious user-visible break in a multi-instance deployment.
- Any pod should be able to write a push message, and any pod serving the polling request should be able to read it.
- Redis list/stream semantics are a natural match for short-lived queued delivery.

**Alternatives considered:**
- Continue using local memory plus sticky session: not correct when requests drift.
- Persist push messages in MySQL only: durable but heavier than needed for short-lived queue-like delivery.

### Decision 4: Chat, cron, and run control records move to MySQL

**Choice:** Replace `chats.json`, `jobs.json`, and equivalent high-churn control records with transactional database-backed repositories. NFS-backed JSON files become transitional import/migration sources, not long-term authority.

**Rationale:**
- These records require concurrency-safe mutation, durable history, and consistent reads across pods.
- MySQL already exists in the deployment environment and is the right place for queryable, durable control facts.
- Once these records are in MySQL, list/detail APIs stop depending on unsafe whole-file overwrites.

**Alternatives considered:**
- Add file locks around JSON repositories: helps only partially and remains brittle on shared filesystems while preserving poor operational ergonomics.
- Keep chats in files but move only jobs first: valid as a rollout step, but insufficient as the target architecture.

### Decision 5: Existing cron leadership remains a companion capability

**Choice:** This change defines the broader multi-instance state architecture but does not duplicate the detailed scheduler lease behavior already described in `redis-coordinated-cron-leadership`.

**Rationale:**
- Cron scheduler single-ownership is already being designed separately.
- Repeating that design here would create competing specs.
- The broader architecture still needs to specify that cron definitions and run records ultimately belong in the same durable/shared state model as other control records.

**Alternatives considered:**
- Merge all cron leadership details into this change: would overload this change and duplicate existing design work.
- Ignore cron entirely: rejected because cron definitions and cron-visible results are part of the same multi-instance consistency problem.

### Decision 6: Rollout is phased by user-visible risk

**Choice:** Deliver the migration in phases:
1. Shared runtime coordination and push delivery
2. Durable control-state migration
3. Integration and cleanup across cron, observability, and residual NFS boundaries

**Rationale:**
- The first user-visible failures come from pod-local runtime state, so that is the fastest path to meaningful improvement.
- Durable control-state migration is essential, but it is safer once shared runtime observability exists.
- A phased plan reduces rollout risk and enables mixed-mode transitional reads where needed.

**Alternatives considered:**
- One-shot migration of all state: highest conceptual simplicity, but too risky operationally.
- Only fix runtime coordination and leave JSON repositories in place: still leaves hidden data-loss and overwrite failures.

## Execution Roadmap

The top-level change is intentionally broader than a single implementation branch. It now serves as the umbrella roadmap for five smaller, independently reviewable changes:

1. `redis-console-push-delivery`
2. `shared-chat-run-coordination`
3. `mysql-chat-control-storage`
4. `mysql-cron-definition-storage`
5. `coordinated-session-state-persistence`

The recommended rollout sequence is driven by user-visible impact first, then durable control-state correction, then the most invasive persistence cleanup.

### Phase 1: Remove the fastest visible inconsistency

**Change:** `redis-console-push-delivery`

**Why first:**
- It fixes the simplest and most obvious cross-pod failure: writes on one pod and polls on another pod cannot currently see the same push messages.
- It has the smallest blast radius and establishes the Redis-backed delivery pattern used by later runtime coordination work.

**Depends on:**
- Shared Redis availability and connection wiring only.

**Can run in parallel with:**
- Early design or schema work for `shared-chat-run-coordination`, but it should land first in production rollout order.

**Exit criteria:**
- `/api/console/push-messages` returns messages regardless of which pod handled the write.
- Tenant/session scoping, expiry, and bounded retention remain correct.

### Phase 2: Make interactive run control cross-instance aware

**Change:** `shared-chat-run-coordination`

**Why second:**
- After push delivery, the next highest-impact user failures are stop requests, running-status drift, and reconnect discovery.
- This phase externalizes shared run ownership without forcing chat metadata or session payload migration in the same step.

**Depends on:**
- Shared Redis availability.
- No hard code dependency on `redis-console-push-delivery`, but both changes benefit from a common Redis operational foundation and observability baseline.

**Can run in parallel with:**
- Early repository design for `mysql-chat-control-storage`.

**Exit criteria:**
- Stop requests work when served by a non-owner pod.
- Chat status reads are consistent across pods while a run is active.
- Reconnect can at least discover an active run from shared state.

### Phase 3: Move chat control facts to durable storage

**Change:** `mysql-chat-control-storage`

**Why third:**
- Once interactive run ownership is externally visible, chat metadata becomes the next major control-state inconsistency.
- This phase removes `chats.json` overwrite risk and adds durable run facts for completed or failed interactive work.

**Depends on:**
- Shared MySQL availability.
- It is strongly recommended after `shared-chat-run-coordination` so the system already distinguishes ephemeral run ownership from durable run facts.

**Can run in parallel with:**
- Early database modeling for `mysql-cron-definition-storage`.

**Exit criteria:**
- Chat create/update/delete/list behavior is authoritative in MySQL.
- Completed and failed interactive runs remain queryable after ephemeral coordination expires.
- `chats.json` is no longer the authoritative chat writer.

### Phase 4: Move cron definitions to durable storage

**Change:** `mysql-cron-definition-storage`

**Why fourth:**
- Cron scheduler ownership is already being handled separately by `redis-coordinated-cron-leadership`, but durable cron definitions still need the same transactional treatment as chat metadata.
- This phase removes `jobs.json` overwrite risk and aligns leader reloads with a durable repository.

**Depends on:**
- Shared MySQL availability.
- Operational alignment with the companion change `redis-coordinated-cron-leadership`.

**Can run in parallel with:**
- Some portions of `mysql-chat-control-storage`, if repository and migration work are kept isolated.

**Exit criteria:**
- Cron definitions and heartbeat configuration are authoritative in MySQL.
- Scheduler reloads rebuild from MySQL rather than `jobs.json`.
- `jobs.json` is no longer the authoritative cron writer.

### Phase 5: Remove silent session checkpoint overwrite

**Change:** `coordinated-session-state-persistence`

**Why last:**
- It fixes a serious but less immediately visible problem than push or stop failures.
- It is also the most invasive persistence change because session payloads are large, nested, and operationally sensitive.

**Depends on:**
- Shared MySQL availability.
- It benefits from the architectural patterns established in `mysql-chat-control-storage`, especially the separation between durable metadata and ephemeral/runtime state.

**Can run in parallel with:**
- Final cleanup after chat and cron durable storage migrations, but should not precede them in rollout priority.

**Exit criteria:**
- Session persistence has authoritative checkpoint metadata.
- Stale writers cannot silently overwrite a newer checkpoint.
- Legacy session files remain migratable during transition.

### Dependency Summary

The practical dependency graph is:

- `redis-console-push-delivery`: no application-level dependency on the other four changes
- `shared-chat-run-coordination`: Redis-only dependency; recommended after push delivery, but not blocked by chat or cron storage migration
- `mysql-chat-control-storage`: recommended after shared run coordination so ephemeral and durable concerns stay separated
- `mysql-cron-definition-storage`: companion to `redis-coordinated-cron-leadership`; independent from chat storage except for shared persistence patterns
- `coordinated-session-state-persistence`: recommended last because it builds on the same durable-state boundary but carries the highest migration sensitivity

The resulting rollout order is therefore:

1. `redis-console-push-delivery`
2. `shared-chat-run-coordination`
3. `mysql-chat-control-storage`
4. `mysql-cron-definition-storage` together with or immediately after `redis-coordinated-cron-leadership`
5. `coordinated-session-state-persistence`

## Risks / Trade-offs

- [Redis coordination outage] -> Interactive stop/reconnect/push semantics may degrade until Redis is available; mitigate by failing shared coordination paths explicitly and exposing ownership/health metrics rather than silently falling back to incorrect local behavior.
- [Database migration complexity] -> Introduce dual-read and targeted backfill during rollout, and keep JSON repositories as import-only fallbacks until cutover verification passes.
- [Mixed-mode transition ambiguity] -> Define one authoritative writer per phase and instrument mismatches between old and new reads during rollout.
- [Streaming continuity expectations] -> The first phase guarantees shared status/stop/push correctness, not perfect token-level continuation after arbitrary owner-pod loss; document this clearly.
- [Broader scope than a single module change] -> Keep concerns separated with dedicated coordination/repository abstractions so implementation remains composable.

## Migration Plan

1. Introduce Redis-backed interactive coordination primitives for run ownership, heartbeat, cancellation, and push delivery.
2. Update console/chat APIs and runtime services to read shared runtime state for status, stop, reconnect discovery, and push retrieval while still executing local tasks on the owning pod.
3. Introduce MySQL-backed repositories for chats, jobs, and run records, plus migration utilities from existing JSON files.
4. Switch chat/job creation and mutation paths to database-backed writes, with temporary migration safeguards and cutover verification.
5. Integrate the durable control-state model with cron persistence and the existing cron leadership change so scheduling authority and job definitions follow the same storage contract.
6. Remove authoritative JSON control-state writes from NFS once cross-instance verification passes.
7. Roll back by keeping the old repositories readable, disabling new writers by feature flag or wiring switch, and restoring local-only behavior only as an emergency measure with explicit acknowledgement that cross-instance semantics degrade.

## Open Questions

- How much shared event buffering is required for reconnect support in the first phase: status-only plus replay window, or full partial-output replay?
- Should job definitions move directly to MySQL in the first durable-state migration step, or should chat records move first because they affect user-facing APIs more frequently?
- What observability is required at launch: owner pod metrics, cancel latency, reconnect attach outcomes, queue depth, migration drift counters?
- Do we want one combined run table for both interactive chat runs and cron runs, or separate durable records with a shared abstraction?
