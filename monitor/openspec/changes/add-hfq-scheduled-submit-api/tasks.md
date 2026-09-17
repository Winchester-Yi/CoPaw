## 1. OpenSpec

- [x] 1.1 Add proposal, design, and delta spec.

## 2. Backend

- [x] 2.1 Add scheduler request model with `source_id` and optional `bbk_id`.
- [x] 2.2 Add service method that builds the latest seven-calendar-day forced task request.
- [x] 2.3 Add route that does not require headers and reuses the service method.

## 3. Verification

- [x] 3.1 Add focused tests for scheduled request construction.
- [x] 3.2 Run OpenSpec validation.
- [x] 3.3 Run backend focused checks.
