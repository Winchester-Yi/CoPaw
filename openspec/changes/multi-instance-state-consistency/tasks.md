## 1. Externalize interactive runtime coordination

- [ ] 1.1 Add Redis-backed coordination primitives for interactive run ownership, heartbeats, cancellation signals, and reconnect-visible shared status
- [ ] 1.2 Replace the in-memory `console_push_store` with Redis-backed tenant-and-session scoped push delivery
- [ ] 1.3 Update console/chat APIs and runtime services so stop, status, reconnect discovery, and push polling use shared runtime state instead of pod-local truth

## 2. Migrate durable control state off shared JSON files

- [ ] 2.1 Introduce MySQL-backed repositories for chat metadata, cron/job definitions, and durable run records
- [ ] 2.2 Refactor service wiring and API paths to write authoritative control state through the new repositories instead of `chats.json` and `jobs.json`
- [ ] 2.3 Add migration and dual-read safeguards for existing JSON-backed data until cutover validation is complete

## 3. Align storage boundaries and operational behavior

- [ ] 3.1 Integrate the new control-state model with the existing cron leadership work so scheduler ownership and job definitions follow the same multi-instance contract
- [ ] 3.2 Restrict NFS-backed workspace files to assets and documents, and remove authoritative high-churn control-state writes from shared files
- [ ] 3.3 Add logs, metrics, and health indicators for owner lease state, cancel latency, reconnect outcomes, push delivery, and migration drift

## 4. Verify multi-instance correctness

- [ ] 4.1 Add tests covering cross-pod chat status, reconnect discovery, stop requests, and push delivery
- [ ] 4.2 Add tests covering concurrent chat/job mutations so no shared-state overwrite or lost update remains
- [ ] 4.3 Document deployment expectations, rollout sequencing, and rollback procedures for Redis/MySQL/NFS-backed multi-instance operation
