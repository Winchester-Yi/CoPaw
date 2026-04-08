## Context

`console_push_store` currently uses an in-memory dictionary guarded by a local asyncio lock. Writers such as the console channel and cron manager append messages locally, and `/api/console/push-messages` consumes messages from the local process only. In a multi-pod deployment, a write on pod A and a poll on pod B are disconnected even with shared Redis already available in the environment.

This change is intentionally narrow. It fixes cross-instance delivery of push messages without redesigning the rest of interactive run ownership or chat persistence. It is the smallest change that removes one of the most visible user-facing inconsistencies.

## Goals / Non-Goals

**Goals:**
- Make console push delivery instance-agnostic across reads and writes.
- Preserve tenant and session isolation for push retrieval.
- Preserve short-lived retention and bounded queue behavior.
- Keep the API contract of `/api/console/push-messages` stable for callers.

**Non-Goals:**
- Solving chat run ownership, stop routing, or reconnect semantics in this change.
- Introducing durable historical storage for push messages beyond the short-lived queue window.
- Reworking frontend polling behavior beyond what is needed to preserve current semantics.

## Decisions

### Decision 1: Use Redis as the authoritative push queue

**Choice:** Store push messages in Redis keyed by tenant and session rather than in local memory.

**Rationale:**
- Any pod can write and any pod can read.
- Push messages are short-lived queue-like data, which is a better fit for Redis than MySQL.
- It directly removes the current pod-local visibility problem with minimal architectural blast radius.

**Alternatives considered:**
- Keep local memory and rely on sticky session: rejected because it fails when requests drift or a pod is replaced.
- Store push messages in MySQL: durable, but heavier than needed for ephemeral delivery semantics.

### Decision 2: Scope keys by tenant and session

**Choice:** Use Redis keys derived from `tenant_id + session_id` for push buckets.

**Rationale:**
- Existing behavior is already tenant and session scoped.
- This keeps read paths simple and avoids cross-session scanning.

**Alternatives considered:**
- Tenant-wide shared queue: would require additional filtering and increases leakage risk across sessions.
- One global queue with metadata filtering: operationally noisier and less isolated.

### Decision 3: Preserve consume-on-read semantics

**Choice:** The polling API continues to drain messages for the requested tenant session rather than only peeking.

**Rationale:**
- This matches current `take(session_id)` behavior.
- It avoids changing frontend expectations in the first migration.

**Alternatives considered:**
- Peek-only retrieval: changes API semantics and would require new acknowledgment logic.

## Risks / Trade-offs

- [Redis unavailable] -> Push delivery cannot remain correct across pods; fail explicitly rather than silently falling back to local memory.
- [Multiple pollers on the same session] -> Consumers may race to drain the same queue; mitigate by keeping current single-consumer expectation and documenting behavior.
- [Unbounded growth] -> Apply TTL and bounded queue trimming per tenant session.

## Migration Plan

1. Introduce a Redis-backed push store implementation with the same append/take interface.
2. Update writers and `/api/console/push-messages` to use the shared store.
3. Add multi-instance tests for write-on-one-pod/read-on-another behavior.
4. Remove the in-memory store as the authoritative implementation once verification passes.

## Open Questions

- Should the Redis implementation use list semantics or stream semantics for the first cut?
- Do we need push queue metrics such as depth, drops, and expired messages in this change?
