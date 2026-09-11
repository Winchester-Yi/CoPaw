# Cron notification concurrency implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; repository AGENTS.md requires sequential execution in the main task.

**Goal:** Send claimed cron notifications concurrently with a per-worker limit and immediately claim the next batch after successful processing.

**Architecture:** Keep Monitor claiming and per-execution status updates intact. A worker-owned asyncio semaphore covers workspace lookup, sending, and status writeback. Await all batch tasks, isolate individual processing errors, and propagate shutdown cancellation. Continue claiming after a nonempty batch with no reported errors; wait the configured interval after empty scans or processing/claim exceptions.

**Tech Stack:** Python 3.10+, asyncio, pytest, pytest-asyncio.

**Spec:** User requests to parallelize notification sending and then immediately claim the next batch; existing contract in `wiki/cron/cron-monitor-notification.md`.

## Global constraints

- `SWE_CRON_NOTIFICATION_CONCURRENCY` defaults to 5, bounded to 1–20 per worker; 1 restores serial processing.
- Keep batch size, configured idle/error interval, source filtering, 600-second claim expiry, and database schema unchanged. Successful nonempty batches now bypass that interval.
- The limit includes status writeback, not only the outgoing channel request.
- A failed failure-writeback is logged for its execution and must not abort other notifications.
- Cancelling the worker must settle active and queued batch tasks before stop returns.
- Completion order within a batch is not guaranteed. Existing expired-lock redelivery behavior remains.
- No commit or push in this task. Preserve unrelated `output/` files.

## Task 1: Add behavior tests, then bounded sending

**Files:**
- Modify: `src/swe/app/crons/notification_worker.py`
- Test: `tests/unit/app/test_cron_notification_worker.py`
- Document: `wiki/cron/cron-monitor-notification.md`
- Document: `analysis/playbook/cron-notification-source-scope.md`

**Interfaces at this step:** `scan_once()` keeps returning None; `_send_one()` keeps existing claim-row and status callback contracts. Add a private limited-send wrapper and one environment variable. Task 2 below adds result flags for immediate refill.

- [x] Write tests that block outbound sends with asyncio Events. Assert multiple sends start before release, active sends never exceed the configured cap, a freed slot progresses without waiting for slower peers, and every successful execution is acknowledged exactly once.
- [x] Exercise default/invalid/zero/high concurrency settings through the active-send count, including the serial fallback.
- [x] Block status writeback and assert that it continues to occupy a concurrency slot.
- [x] Simulate a send failure followed by a failure-writeback error; assert later records still send and the execution error is logged.
- [x] Stop a running worker with active and queued notifications; assert active work is cancelled, queued work never sends, and no cancellation is recorded as delivery failure.
- [x] Run `.venv/Scripts/python.exe -m pytest tests/unit/app/test_cron_notification_worker.py -q`. Observed before implementation: 6 failed, 4 passed; concurrency barriers timed out and the failure-writeback case aborted the scan.
- [x] Implement `_send_semaphore = asyncio.Semaphore(_get_int_env(CONCURRENCY_ENV, default=5, minimum=1, maximum=20))` in the constructor. Replace the serial loop with awaited asyncio.gather over limited sends. In each wrapper use `async with self._send_semaphore`, await `_send_one(row)`, and catch/log Exception while allowing CancelledError to propagate.
- [x] Rerun the focused worker tests, then `tests/unit/app/test_monitor_sync_client.py` for request compatibility.
- [x] Update operational documentation with per-worker concurrency, serial fallback, unchanged scan pacing, and the existing lock-expiry limitation.
- [x] Review cancellation, error isolation, configuration bounds, and scope sequentially in the main task. These are self-reviews, not independent agent reviews.
- [x] Run targeted formatting/lint checks and `git diff --check`; report test results and local-only delivery.

## Task 2: Refill immediately after a successful batch

**Files:** Continue editing the same worker, test, wiki, and playbook files.

**Interfaces:** `_send_one()` and `_send_one_with_limit()` return whether processing reported no exception. `scan_once()` returns true only for a nonempty batch whose processing results are all true. The result is consumed only by `_run_loop()`; this is not an HTTP API change.

- [x] Add a loop test with a blocked first-batch acknowledgement, a second partial batch, then an empty result. Assert no new claim precedes completion of all first-batch work, both batches complete before the first idle wait, and the partial batch also triggers a follow-up claim.
- [x] Add empty-scan and claim-error tests that observe entry into the existing stop-aware wait and assert a single claim attempt.
- [x] Add delivery-error and failure-writeback-error tests that assert the batch is processed but the loop enters backoff instead of immediately retrying.
- [x] Run worker tests before production edits. Observed 1 failed, 14 passed: the refill test expected 3 claims before idle, but the old loop made only 1.
- [x] Implement boolean processing results and `if await self.scan_once(): continue` in `_run_loop()`. Preserve CancelledError propagation and source-scoped SQL claiming.
- [x] Update the wiki and playbook: successful batches immediately refill, empty/error rounds wait, and claim selection uses `ORDER BY notification_due_at, id` among eligible records, without promising concurrent completion order.
- [x] Run worker and Monitor sync-client tests, Black, Flake8, and `git diff --check`; review failure backoff, full-batch settlement, shutdown, and SQL ordering in the main task.

## Verification record

- Baseline: existing worker test passed (1 test) in the project Windows virtual environment.
- GitNexus impact: `scan_once` LOW, one direct caller (`_run_loop`), upstream `start` and application background-service startup; constructor LOW with no indexed direct callers.
- Green: worker tests passed (10 tests); worker and Monitor sync-client tests passed together (54 tests).
- Sequential self-review: checked configuration bounds and unchanged public constructor; checked semaphore lifetime and slot reuse; checked exception isolation and shutdown cancellation; checked operational documentation and untouched Monitor schema/claim predicates. No independent agent review was performed under the repository execution rule.
- Final verification: 54 tests passed, including cancellation with asynchronous cleanup; Black (79 columns, Python 3.10 target), Flake8 with repository exclusions, and `git diff --check` passed. Only local edits; no commit, push, or production deployment.
- Immediate-refill follow-up: worker tests passed (15 tests); worker plus Monitor sync-client tests passed (59 tests). The idle/error interval is retained; normal nonempty batches refill immediately after every row settles. Self-review checked empty results, partial batches, per-row error backoff, stop cancellation, and existing SQL priority. GitNexus reports LOW for the indexed affected methods; the newly added limited-send wrapper is not indexed yet, and its sole source caller is `scan_once`. No SQL change, commit, push, or deployment.
