# Cron Callback Missing Job Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, following the repository's sequential task mapping. Use test-driven-development and verification-before-completion.

**Goal:** Return HTTP 200 with `skipped=job_not_found` when a cron callback references a missing job.

**Architecture:** Handle only the exact missing-job `KeyError` raised by `CronManager.run_job` at the callback boundary. Keep Scheduler's existing definite-rejection/retry behavior for this outcome, so an HTTP 200 skip is never treated as accepted execution or an ambiguous transport result.

**Tech Stack:** Python, FastAPI, pytest, httpx.

**Spec:** User-confirmed HTTP 200 skip response; existing callback ownership in `docs/adr/0010-independent-cron-scheduling-service-owns-batch-dispatch.md`.

## Global Constraints

- Preserve unrelated working-tree changes; no commit or push in this task.
- Preserve missing-parameter validation, disabled-job skips, and real execution errors.
- No changes to CronManager's public exception contract or Scheduler persistence schema.
- Use the project Linux virtual environment through WSL.
- GitNexus impact: `_run_job_callback` LOW, one direct production caller (`_run_agent_callback`), two upstream symbols, no indexed processes. `SweCronCallbackClient.dispatch_job` LOW, no indexed callers/processes; source confirms `_dispatch_execution_intent` consumes it.

## Task 1: Missing-job callback outcome

**Files:**
- Modify: `src/swe/app/routers/internal.py`
- Modify: `scheduler/src/scheduler/app/services/cron/scheduling_service.py`
- Test: `tests/unit/routers/test_internal_tenant_scope.py`
- Test: `tests/unit/scheduler/test_batch_callback_skip.py`
- Document: `analysis/playbook/common-errors.md`

- [x] Add HTTP regression cases for flat and encoded callback bodies, legacy and dispatch-service callers, and deletion between lookup and execution. Use real `CronManager.run_job` with a repository returning no job. Assert:

  ```python
  assert response.status_code == 200
  assert response.json() == {
      "status": "ok", "skipped": "job_not_found", "job_id": "job-1",
  }
  ```

- [x] Add guards proving unrelated KeyError and RuntimeError remain HTTP 500. Add a Scheduler test using its real HTTP client and dispatch method with a mocked HTTP 200 skip response; assert it records definite rejection without marking accepted or unknown execution.
- [x] Run the new tests before implementation. Expect HTTP 500 instead of 200, and Scheduler to incorrectly classify the skip as unknown.

  ```bash
  venv/bin/python -m pytest tests/unit/routers/test_internal_tenant_scope.py -q -k 'missing_job or job_lookup_race or execution_errors'
  venv/bin/python -m pytest tests/unit/scheduler/test_batch_callback_skip.py -q -k missing_job
  ```

- [x] Catch only `KeyError` with `exc.args == (f"Job not found: {job_id}",)` immediately around `mgr.run_job`; log tenant/source/agent/job and return the HTTP 200 skip body. Re-raise other KeyErrors.
- [x] In `SweCronCallbackClient.dispatch_job`, raise a definite rejection `RuntimeError` for `skipped == "job_not_found"` before the unknown-skip branch. Retain the existing retry policy and disabled-job behavior.
- [x] Add a short playbook entry with the symptom, response contract, and scope checks for external scheduler leftovers.
- [x] Run related suites separately to avoid cross-package test-collection interference:

  ```bash
  venv/bin/python -m pytest tests/unit/routers/test_internal_tenant_scope.py tests/unit/app/test_cron_disabled_dispatch.py tests/unit/app/test_external_cron_scope_refresh.py -q
  venv/bin/python -m pytest tests/unit/scheduler/test_batch_callback_skip.py tests/unit/scheduler/test_cron_scheduling_service.py -q
  git diff --check
  ```

- [x] Review exact matching, deletion race, parameter validation, and Scheduler outcome semantics; report fresh test results and leave changes uncommitted.

## Verification results

- Before the fix: 8 missing-job HTTP cases failed with HTTP 500; 3 unrelated-error guards passed. The new Scheduler test failed because the skip was classified as an unknown callback outcome.
- After the fix: SWE router/disabled-job/external-scope suites had 75 passed and 1 failure. All 12 new regression cases passed across SWE and Scheduler.
- Scheduler callback/scheduling suites: 49 passed.
- `git diff --check`: passed for all changed target files.
- Remaining unrelated failure: `test_cron_manager_system_jobs_do_not_register_cleanup` rejects every `/job-admin/v2/add-job` request, including the existing dream registration. Its test, `CronManager._register_system_jobs`, and scheduler adapter have no diff from HEAD. This path does not invoke the changed callback handler/client. No unrelated production or test behavior was changed.
- Review was performed inline under the repository's sequential task mapping; no independent subagent review was performed.
