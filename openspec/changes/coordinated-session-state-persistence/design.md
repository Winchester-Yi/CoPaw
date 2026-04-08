## Context

`SafeJSONSession` currently persists session state by directly reading and rewriting JSON files in a shared workspace directory. In a single-process deployment this is simple and sufficient, but in a multi-instance deployment it creates silent lost updates because multiple pods can read the same old file state and each write a different replacement.

This change does not require every session payload to become relational data. Instead, it introduces an authoritative shared checkpoint boundary: durable metadata says which checkpoint version is current, while raw checkpoint payloads may remain file-based artifacts on NFS if needed.

## Goals / Non-Goals

**Goals:**
- Prevent silent cross-instance overwrite of session state.
- Introduce authoritative versioned checkpoint metadata in durable storage.
- Preserve the ability to store raw session payloads as files if that remains operationally useful.
- Allow migration from existing session files without dropping visible state.

**Non-Goals:**
- Fully normalizing all session contents into relational tables.
- Reworking chat metadata or cron definition storage, which are handled in separate changes.
- Guaranteeing token-stream continuation after owner pod failure.

## Decisions

### Decision 1: Session checkpoint metadata becomes authoritative

**Choice:** Store session checkpoint identity and version metadata in MySQL, even if raw checkpoint payloads remain file-backed.

**Rationale:**
- This gives the system a transactional cross-instance boundary for "what is the latest valid checkpoint?"
- It avoids forcing every large or nested session payload into relational columns immediately.

**Alternatives considered:**
- Keep raw JSON files as the sole authority with file locks: still brittle and difficult to reason about across pods.
- Store every session payload directly in MySQL: possible, but larger in scope than needed for the first safe migration.

### Decision 2: Writes use version-aware coordination

**Choice:** Session updates must use coordinated version or compare-and-set semantics so a stale writer cannot silently replace a newer checkpoint.

**Rationale:**
- The current failure mode is silent last-writer-wins overwrite.
- Version-aware writes make conflicts explicit and testable.

**Alternatives considered:**
- Best-effort local retries on file overwrite: still leaves ambiguous correctness.

### Decision 3: Raw checkpoint blobs may remain on NFS

**Choice:** Keep raw checkpoint payload files on NFS if desired, but treat them as blobs referenced by authoritative metadata rather than as the source of truth.

**Rationale:**
- This preserves the parent architecture boundary: NFS is for files, MySQL is for facts.
- It also limits migration scope for potentially large session payloads.

**Alternatives considered:**
- Move all session payload bytes into MySQL immediately: more invasive and not required for correctness in the first step.

## Risks / Trade-offs

- [Conflict handling complexity] -> Surface explicit version conflicts rather than silently overwriting, and add retry behavior only where semantics are safe.
- [Blob/metadata drift] -> Write checkpoint blobs before committing authoritative metadata, and clean up abandoned blobs on failure paths.
- [Migration complexity] -> Import existing session files into versioned metadata lazily or via backfill.

## Migration Plan

1. Introduce MySQL-backed session checkpoint metadata.
2. Update session save paths to write versioned checkpoints and commit metadata atomically from the application's point of view.
3. Update session load paths to resolve the latest checkpoint through metadata rather than by guessing the latest file.
4. Add compatibility for existing legacy session files during transition.
5. Remove direct authoritative read-modify-write of shared session JSON files once parity validation passes.

## Open Questions

- Should version conflicts fail the request immediately or trigger bounded automatic retry for some update paths?
- Do we want lazy migration on session access, background backfill, or both?
