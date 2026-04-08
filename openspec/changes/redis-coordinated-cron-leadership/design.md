## Context

The current backend cron design is single-instance by construction. Each `Workspace` creates a `CronManager`, and `CronManager.start()` loads `jobs.json` and starts a local `AsyncIOScheduler`. In a multi-instance deployment backed by shared NAS, multiple instances can load the same tenant-agent workspace and each will independently schedule the same jobs. This causes duplicate cron execution, duplicate heartbeat execution, and unclear ownership during instance failure.

For this stage, the team explicitly does not want to redesign the rest of the runtime for strict multi-instance continuation. Sticky requests remain acceptable for interactive traffic, and instance failure may still interrupt in-flight user session state. The scope here is narrower: make cron and heartbeat single-active across instances while preserving `jobs.json` on NAS as the stage-one configuration store.

## Goals / Non-Goals

**Goals:**
- Ensure exactly one instance at a time owns cron scheduling for a given `tenant + agent`.
- Prevent duplicate timed cron execution during leader overlap or failover boundaries.
- Keep `jobs.json` as the shared cron definition store in this stage.
- Allow any instance to serve cron read/write APIs while ensuring the active leader reloads schedule changes.
- Preserve current manual `run_job` semantics as an explicit extra execution.

**Non-Goals:**
- Migrating cron storage from NAS/JSON to a database in this change.
- Externalizing console push, approvals, chat runtime state, or other in-memory request state.
- Splitting API and worker roles into separate deployment units.
- Providing in-flight cron continuation after leader failure.
- Reworking non-cron polling connectors in this change.

## Decisions

### Decision 1: Cron ownership is elected at the `tenant + agent` level

**Choice:** Use a Redis lease keyed by `tenant + agent` so only one instance activates scheduling for that workspace.

**Rationale:**
- The current scheduler boundary is the workspace/agent, not an individual job.
- One leader per tenant-agent keeps the current `CronManager` ownership model largely intact.
- Leadership can cover both scheduled jobs and heartbeat with one coordination concept.

**Alternatives considered:**
- Per-job leader election: more granular, but it conflicts with the current scheduler shape and adds unnecessary coordination complexity.
- No lease, only execution locks: prevents some duplicates but still allows every instance to keep a hot scheduler and react unpredictably to reloads.

### Decision 2: Timed cron de-duplication uses a separate per-job execution lock

**Choice:** Timed scheduler callbacks must acquire a Redis job execution lock keyed by `tenant + agent + job_id` before executing.

**Rationale:**
- Lease edges can briefly overlap during renew failure, network jitter, or slow scheduler shutdown.
- A second guard at execution time prevents duplicate timed runs even if two instances momentarily believe they are active.
- This lock only protects timed execution; it does not determine scheduler ownership.

**Alternatives considered:**
- Rely only on the agent lease: simpler, but more exposed to duplicate execution at failover boundaries.
- Use the same lock for manual runs: rejected because manual `run_job` should remain an explicit extra execution.

### Decision 3: Manual `run_job` bypasses timed execution locking

**Choice:** Manual execution remains a one-shot administrative action and does not acquire the timed execution lock.

**Rationale:**
- The intended semantics are “run now in addition to the schedule”, not “simulate the next timed tick”.
- Locking manual runs against timed runs would suppress legitimate operator-triggered executions.
- This keeps the stage-one design focused on de-duplicating scheduler-originated work only.

**Alternatives considered:**
- Share the timed lock with manual runs: safer from accidental overlap, but changes operator-visible behavior and would silently drop requested manual executions.
- Add a separate manual-run dedupe lock: unnecessary for this stage.

### Decision 4: `jobs.json` remains the source of cron definitions, Redis carries reload signals

**Choice:** Keep NAS-backed `jobs.json` for stage one, but require successful cron mutations to publish a Redis reload event for the owning `tenant + agent`.

**Rationale:**
- This minimizes migration scope while still fixing duplicate scheduling.
- Any instance can continue serving cron APIs; the leader becomes responsible for rebuilding local schedule state after change notification.
- It preserves the existing file layout while making scheduler ownership explicit.

**Alternatives considered:**
- Move cron definitions to a database now: cleaner long-term, but outside the current requested scope.
- Poll `jobs.json` from every instance: simpler mechanically, but still wastes work and complicates ownership semantics.

### Decision 5: Redis failure must fail safe by stopping scheduling

**Choice:** If a leader cannot renew its lease reliably, it must deactivate local scheduling rather than continue optimistically.

**Rationale:**
- Duplicate cron execution is the main failure to avoid in this change.
- Short pauses in scheduling are acceptable under the current operational constraints; split-brain scheduling is not.
- This keeps the system biased toward safety during Redis instability.

**Alternatives considered:**
- Continue scheduling until another leader is observed: higher availability, but risks duplicate execution.

## Risks / Trade-offs

- [Redis unavailable] -> Active cron scheduling pauses until leadership can be safely re-established; this trades availability for duplicate-execution safety.
- [NAS-backed `jobs.json` remains eventually coordinated] -> Cron mutations must publish reload events only after successful file writes, and leader reload stays full-rebuild rather than incremental inference.
- [Leader handoff can interrupt in-flight scheduled work] -> Accept this in stage one; new leadership only guarantees future scheduling, not continuation of already-running jobs.
- [Manual runs can overlap with scheduled runs] -> Keep this as intentional behavior and document that timed de-duplication does not apply to explicit operator-triggered executions.
- [Additional moving parts in workspace startup] -> Isolate Redis concerns in a dedicated coordination layer so `CronManager` remains focused on local scheduling behavior.

## Migration Plan

1. Introduce Redis-backed coordination primitives for agent lease, timed job lock, and reload pub/sub.
2. Refactor cron lifecycle so `CronManager` supports passive startup plus explicit activation/deactivation under controller ownership.
3. Add a workspace-scoped cron leadership controller that elects leadership and drives `CronManager` transitions.
4. Update cron mutation paths to publish reload signals after successful `jobs.json` writes.
5. Add multi-instance tests for leadership, failover, reload, and manual-run semantics.
6. Roll back by disabling the coordination controller and restoring direct `CronManager.start()` ownership if regressions appear.

## Open Questions

- Should leadership be started eagerly for every loaded workspace, or only when cron/heartbeat is configured for that workspace?
- Do we want observability counters for current leader, lease loss, skipped timed executions, and reload latency in this stage or a follow-up change?
- Should future iterations move cron definitions from NAS to database storage once broader multi-instance state is addressed?
