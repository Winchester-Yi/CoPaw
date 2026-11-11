# Prompt Model Failure Retry and Worker Feedback

## Goal

For a completed managed Cron attempt, the existing main execution's retry-eligible errors continue to qualify as model failures; a failed async subtask qualifies as a model failure only when its correlated `swe_cron_executions` row has `async_status='error'` and `error_type contains 'Prompt'`. Retry and worker feedback share this rule. Settled non-model business failures remain visible as failed outcomes but count as capacity-success samples. The managed batch board and failure preview show the raw `error_type`.

The owning platform team adds `swe_cron_executions.error_type`. New table bootstrap definitions and schema checks will require it; no live migration or DDL execution is in scope. The producer must write `async_status` and `error_type` atomically before publishing the terminal async result. Substring matching is case-sensitive, using "Prompt" in (error_type or ""). Missing or other `error_type` values are non-model.

## Implementation units

1. **Shared classification**: add a small common managed-Cron policy for main retry exclusions plus the substring child `Prompt` rule. Keep the business status unchanged. Use it from automatic, daily, and manual retry decisions.
2. **Worker feedback**: use the correlated execution error type for the new shared capacity feedback; settle legacy dispatch intents as failed, without retry, for terminal non-model execution failures. Preserve retry limits, backoff, and auth/unknown-outcome safeguards.
3. **Error-field projection**: select `error_type` only from the execution row correlated by the existing execution identity, include it in managed member and retry-preview responses, and render the raw string in both Console tables (`-` for null).
4. **Schema contract**: add nullable `error_type` to fresh monitor/scheduler execution table definitions and to Scheduler's required-column check. Existing production databases must have the externally managed column before rolling out code.

## Primary files

- `scheduler/src/scheduler/app/services/cron/managed/auto_retry.py`
- `scheduler/src/scheduler/app/services/cron/managed/daily_retry_policy.py`
- `scheduler/src/scheduler/app/services/cron/managed/daily_retry_enqueue.py`
- `scheduler/src/scheduler/app/services/cron/managed/batch_retry.py`
- `scheduler/src/scheduler/app/services/cron/managed/settlement.py`
- `scheduler/src/scheduler/app/services/cron/managed/feedback.py`
- `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`
- `scheduler/src/scheduler/app/services/cron/managed/batch_failures.py`
- `scheduler/src/scheduler/app/services/cron/managed/schema.py`
- `scheduler/src/scheduler/app/database/schema.py`
- `monitor/src/monitor/app/services/cron/managed_board.py`
- `monitor/src/monitor/app/database/schema.py`
- `console/src/api/modules/managedBatchBoard.ts`
- `console/src/pages/Monitor/ManagedBatchDispatch/BatchDetail.tsx`
- `console/src/pages/Monitor/ManagedBatchDispatch/FailurePanel.tsx`

## Acceptance cases

- A correlated failed async execution with `error_type contains 'Prompt'` qualifies for retry within existing limits and contributes one worker failure.
- A correlated failed async execution with any other value, including null, is not automatically, daily, or manually retried; the business outcome remains failed and it contributes one worker-success sample.
- A main execution failure continues to use its existing non-retryable error exclusions; all other main failures retain current retry eligibility and count as worker failures.
- A terminal cancelled/skipped attempt is not a worker-success sample.
- A pending async status does not create a retry candidate or terminal worker sample.
- A legacy dispatch intent receives the same retry decision as its correlated execution and does not schedule a retry for non-model subtask failure.
- Raw error types, including `Prompt`, arbitrary other strings, long strings, and null, are returned for the correct member and shown in the member detail and retry preview without relabeling.
- Existing source, tenant, Agent, job, execution and batch identity checks remain in force; auth and unknown-outcome safeguards remain effective.

## Verification

Update the focused Scheduler retry/feedback and managed-board query tests, their SQLite schema fixtures, and the two Console page tests. Run the targeted pytest/Vitest checks plus Console typecheck, lint and format check; inspect the final diff and confirm unrelated pre-existing work remains untouched.
