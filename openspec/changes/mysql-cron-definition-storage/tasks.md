## 1. Add durable cron repositories

- [ ] 1.1 Introduce MySQL-backed repositories for cron and heartbeat definitions
- [ ] 1.2 Add schema and repository wiring for tenant-and-agent scoped cron reads and writes
- [ ] 1.3 Define import support for existing `jobs.json` data

## 2. Migrate cron definition paths

- [ ] 2.1 Update cron create, update, pause, resume, delete, and list paths to use the MySQL repositories
- [ ] 2.2 Update leader reload paths to rebuild schedules from MySQL-backed definitions
- [ ] 2.3 Remove `jobs.json` from the authoritative write path after cutover verification

## 3. Verify durable cron definition behavior

- [ ] 3.1 Add tests for concurrent multi-instance cron mutations
- [ ] 3.2 Add tests for consistent cron reads across instances after mutation
- [ ] 3.3 Add tests for migration/import from existing `jobs.json` definitions
