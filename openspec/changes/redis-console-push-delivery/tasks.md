## 1. Add Redis-backed push storage

- [ ] 1.1 Introduce a Redis-backed console push store keyed by tenant and session
- [ ] 1.2 Preserve bounded retention and expiration behavior in the Redis implementation
- [ ] 1.3 Keep a compatible append/take API so existing callers can migrate cleanly

## 2. Wire push writers and readers

- [ ] 2.1 Update console channel send paths to publish push messages through the shared store
- [ ] 2.2 Update cron push/error paths to publish through the shared store
- [ ] 2.3 Update `/api/console/push-messages` to consume shared push delivery state

## 3. Verify cross-instance delivery

- [ ] 3.1 Add tests for write-on-one-instance/read-on-another behavior
- [ ] 3.2 Add tests for tenant and session isolation during push polling
- [ ] 3.3 Add tests for expiry and bounded queue trimming behavior
