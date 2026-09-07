# Query skill snapshots bind in-flight selection

Each query captures an immutable **Query Skill Snapshot** at start, including its effective skill set, trusted metadata, resolved package locations, and content signatures. Skill lifecycle changes therefore affect subsequent queries only; if a package is missing or its signature changes before registration or reading, the current query fails closed for that skill rather than using stale metadata. This preserves a stable in-flight selection without copying complete skill directories, while keeping content changes from bypassing the trust boundary.

When a new query detects an invalid snapshot, it waits for one deduplicated worker-based reconciliation before capturing its snapshot; reconciliation failure fails closed for affected skills while the query continues without unconfirmed Workspace Skills. Background reconciliation with the old snapshot is reserved for queries that were already admitted before the change.

Agents, Hooks, and Background SubAgent Runs derived from one query inherit its snapshot; a launched child keeps that launch snapshot, subject to the same content-signature recheck before its skill registration.

Snapshot caches are process-local. Across Kubernetes instances, invalidation relies on atomically replaced manifest files and shared-storage stat/signature checks; this decision does not introduce a Redis or pub/sub invalidation protocol.

Persistent Workspace Skills use this query snapshot. Chat-private temporary Scenario Skills remain governed by the existing Session Marketplace Resource Snapshot and session-root validation; they do not enter Workspace manifest/channel resolution.

Snapshot `generation` is process-local and diagnostic only; it is not persisted in `skill.json` or used as the security decision, which remains based on manifest and content signatures.

The snapshot/cache also carries the validated runtime profile derived from each skill's content and hook configuration, so Agent construction does not repeat synchronous `SKILL.md`, `hooks.json`, or feature extraction work on the event loop; profile entries are invalidated with their content signature.
