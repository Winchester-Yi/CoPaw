## 1. Add authoritative session checkpoint metadata

- [ ] 1.1 Introduce MySQL-backed session checkpoint metadata with tenant, session, user, and version identity
- [ ] 1.2 Define how raw checkpoint payloads are stored and referenced from authoritative metadata
- [ ] 1.3 Add version-aware write semantics so stale writers cannot silently overwrite newer checkpoints

## 2. Migrate session save and load paths

- [ ] 2.1 Update session save paths to create coordinated checkpoints
- [ ] 2.2 Update session load paths to resolve the latest checkpoint through authoritative metadata
- [ ] 2.3 Add compatibility or migration handling for existing legacy session JSON files

## 3. Verify conflict-safe persistence

- [ ] 3.1 Add tests for concurrent multi-instance session updates
- [ ] 3.2 Add tests for durable latest-checkpoint resolution across instances
- [ ] 3.3 Add tests for conflict and cleanup behavior when checkpoint writes partially fail
