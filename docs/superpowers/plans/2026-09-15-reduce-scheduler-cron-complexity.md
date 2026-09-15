# Reduce Scheduler Cron Complexity

## Scope

Reduce the Sonar cognitive complexity of these functions to at most 15 without
changing observable behavior:

- `CronDispatchIntentService._build_ordered_execution_rows`
- `SweCronCallbackClient.dispatch_job`

## Plan

1. Confirm existing behavior coverage for the extracted response and
   row-building branches; add focused tests only if a material gap is found.
   - Verify: the existing tests cover the observable contracts affected by the
     extraction.
2. Extract single-purpose private helpers in the same modules and keep the two
   public/internal call signatures unchanged.
   - Verify: targeted Scheduler unit tests pass.
3. Run complexity, formatting, diff, and GitNexus change-scope checks.
   - Verify: both reported functions are at or below 15 and only the expected
     Cron scheduling paths are affected.

## Commands

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/scheduler/test_batch_priority.py tests/unit/scheduler/test_cron_scheduling_service.py tests/unit/scheduler/test_batch_callback_skip.py -q
.venv\Scripts\python.exe -m flake8 scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py scheduler/src/scheduler/app/services/cron/scheduling_service.py --select C901 --max-complexity 15
git diff --check
npx gitnexus detect-changes -r CoPaw
```

No commit or push is included.
