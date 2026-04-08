## Context

The repository already contains a `redis-coordinated-cron-leadership` change that addresses single-active scheduling ownership. That removes duplicate scheduling, but it does not solve the fact that job definitions still live in `jobs.json`, which uses shared file replacement semantics without transactional mutation guarantees.

This change isolates the durable-definition problem: job definitions and related heartbeat configuration should be stored in MySQL so cron mutations and scheduler reloads operate on an authoritative transactional source.

## Goals / Non-Goals

**Goals:**
- Make cron definition reads and writes transactional across backend instances.
- Remove `jobs.json` as the authoritative long-term cron definition store.
- Align leader reload behavior with database-backed job definitions.
- Preserve existing scheduler-ownership responsibilities from the separate cron leadership change.

**Non-Goals:**
- Replacing Redis-based leadership or execution locks from the existing cron leadership design.
- Solving interactive chat runtime coordination in this change.
- Moving unrelated workspace documents or file assets out of NFS.

## Decisions

### Decision 1: MySQL becomes the authoritative cron definition store

**Choice:** Persist cron definitions in MySQL rather than `jobs.json`.

**Rationale:**
- Cron definitions are durable control state and need transactional mutation guarantees.
- Shared files are the wrong long-term authority even if only one leader schedules jobs.
- MySQL provides indexed reads and consistent mutation semantics across pods.

**Alternatives considered:**
- Keep `jobs.json` with Redis reload signals only: still leaves overwrite and durability risks.
- Store definitions in Redis: less suitable than MySQL for durable authoritative configuration.

### Decision 2: Cron leadership remains a companion change

**Choice:** Reuse the existing cron leadership change for active scheduler ownership while moving only durable definitions here.

**Rationale:**
- It keeps concerns separated and avoids conflicting lease semantics.
- Leadership and durable storage are complementary but distinct concerns.

**Alternatives considered:**
- Merge durable storage into the leadership change: would overload the scheduler-focused change.

### Decision 3: Migrate JSON definitions with controlled cutover

**Choice:** Support import/backfill from existing `jobs.json` before removing file authority.

**Rationale:**
- Existing deployments already contain live job definitions in JSON files.
- Safe cutover requires preserving current jobs.

**Alternatives considered:**
- Start with an empty database and require manual recreation: too risky and operationally expensive.

## Risks / Trade-offs

- [Cutover mismatch] -> Use parity checks between JSON and database-backed reads during rollout.
- [Scheduler/repository drift] -> Ensure the active leader always rebuilds from the authoritative database-backed repository after successful mutation.
- [Operational complexity] -> Accept additional migration complexity because cron definitions are durable control state.

## Migration Plan

1. Introduce MySQL-backed cron definition repositories.
2. Import existing `jobs.json` definitions into MySQL.
3. Switch cron read/write APIs to the new repository.
4. Update leader reload behavior to reload from the MySQL repository.
5. Remove `jobs.json` from the authoritative write path once parity verification passes.

## Open Questions

- Should heartbeat definitions live in the same table model as scheduled jobs or in a related auxiliary table?
- Do we need explicit versioning on job definitions for operator-visible conflict reporting?
