## 1. Add durable chat repositories

- [ ] 1.1 Introduce MySQL-backed repositories for chat metadata
- [ ] 1.2 Introduce durable interactive run record storage
- [ ] 1.3 Add schema and repository wiring for tenant-scoped chat reads and writes

## 2. Migrate chat control paths

- [ ] 2.1 Update chat creation, update, delete, and list paths to use the MySQL repositories
- [ ] 2.2 Add backfill or import support for existing `chats.json` data
- [ ] 2.3 Add dual-read or parity checks during rollout until cutover is complete

## 3. Verify durable behavior

- [ ] 3.1 Add tests for concurrent multi-instance chat mutations
- [ ] 3.2 Add tests for consistent chat reads across instances after mutation
- [ ] 3.3 Add tests for durable run fact persistence after runtime coordination expires
