---
status: accepted
---

# Conversation Migration Publishes Tenants Independently

On 2026-11-13 the user selected independent rollout for successful tenants while failed tenants remain unavailable and can be repaired and migrated later. A publication unit contains every source, Agent, workspace and permanent share belonging to one `(service_unit_id, tenant_id)`; partial import is never sufficient to publish that unit. One tenant's data failure must not prevent unrelated, provably independent tenants from completing.

This supersedes ADR 0044's all-production-scopes-at-once decision and ADR 0045's requirement to keep all new-version traffic stopped until every tenant completes. Legacy writers must still drain and exit before the immutable migration source is captured. There is no fallback to legacy, dual write or incremental catch-up. Failed or unknown tenants remain closed; already published tenants must retain new committed writes during subsequent retries and rollback. ADR 0045's consistency and complete rollback export requirements continue to apply.

The decision is accepted; implementation is pending. Existing CLI and runtime still use the whole-job gate. See the [implementation design](../superpowers/specs/2026-11-13-conversation-tenant-cutover-design.md); this ADR does not authorize bypassing current readiness checks.
