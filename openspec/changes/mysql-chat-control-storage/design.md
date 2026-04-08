## Context

`chats.json` is a shared single-file repository used for chat metadata. It provides shared visibility across pods, but not transactional mutation safety. This means concurrent creates, updates, or deletes can overwrite one another. Separately, interactive run facts currently disappear once local runtime state is gone, which makes completed execution history weakly represented.

This change moves chat metadata and durable interactive run records into MySQL. It complements shared runtime coordination by giving the system a durable fact store for chat objects and finished run outcomes.

## Goals / Non-Goals

**Goals:**
- Make chat metadata reads and writes transactional across backend instances.
- Persist durable interactive run facts independently from ephemeral run coordination.
- Provide a safe migration path from `chats.json`.
- Keep chat APIs backed by one authoritative durable store after cutover.

**Non-Goals:**
- Moving cron definitions in this change.
- Replacing every raw session payload with relational storage.
- Solving interactive runtime ownership, which is handled in a separate shared-run change.

## Decisions

### Decision 1: MySQL becomes the authoritative chat repository

**Choice:** Chat metadata is persisted in MySQL rather than shared JSON files.

**Rationale:**
- Chat metadata requires consistent multi-instance mutation semantics.
- MySQL is already available in the target deployment and supports transactional updates and indexed reads.
- This removes whole-file overwrite failure modes from chat operations.

**Alternatives considered:**
- Add file locks to `chats.json`: still operationally brittle and poor for querying.
- Keep chats in Redis only: insufficient as the durable source of truth.

### Decision 2: Durable run facts are stored separately from ephemeral coordination

**Choice:** Add durable run records for interactive executions rather than relying only on ephemeral runtime ownership state.

**Rationale:**
- Completed and failed runs need to remain queryable after Redis ownership entries expire.
- This cleanly separates "is it running now?" from "what happened previously?"

**Alternatives considered:**
- Infer durable run history only from chat state: too lossy for operational debugging and correctness.

### Decision 3: Migration uses dual-read safeguards

**Choice:** Introduce controlled migration from existing JSON data with a temporary compatibility window.

**Rationale:**
- Existing tenant workspaces already contain `chats.json`.
- A cutover without migration would drop visible state.

**Alternatives considered:**
- Hard cutover with no backfill: too risky.

## Risks / Trade-offs

- [Schema migration complexity] -> Keep chat and run tables narrowly scoped and migrate in explicit steps.
- [Drift during transition] -> Use one authoritative writer and dual-read verification during rollout.
- [Operational overhead] -> Accept database dependency because chat metadata is durable control state, not a file asset.

## Migration Plan

1. Introduce MySQL-backed repositories for chat metadata and durable run records.
2. Backfill existing `chats.json` data into the new store.
3. Switch chat API writes to MySQL-backed repositories.
4. Keep read compatibility during transition until parity checks pass.
5. Remove `chats.json` as the authoritative writer after verification.

## Open Questions

- Should run records be created only for terminal outcomes, or also for active-run lifecycle checkpoints?
- Do we want tenant-specific backfill tooling or a generic startup migration path?
