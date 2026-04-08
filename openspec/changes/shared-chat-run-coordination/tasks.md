## 1. Add shared run coordination primitives

- [ ] 1.1 Introduce Redis-backed interactive run ownership and heartbeat tracking
- [ ] 1.2 Introduce shared cancel signaling for active chat runs
- [ ] 1.3 Define owner identity and stale-run expiry behavior

## 2. Wire runtime control paths

- [ ] 2.1 Update task startup and teardown to register and clear shared run ownership
- [ ] 2.2 Update `/api/console/chat/stop` to use shared cancellation instead of local-only cancellation
- [ ] 2.3 Update chat status and reconnect-discovery paths to read shared run state

## 3. Verify cross-instance control semantics

- [ ] 3.1 Add tests for status consistency across instances
- [ ] 3.2 Add tests for stop requests served by non-owner instances
- [ ] 3.3 Add tests for stale ownership expiry and owner loss behavior
