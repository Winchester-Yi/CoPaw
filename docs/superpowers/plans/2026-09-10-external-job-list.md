# External Job List Implementation Plan

> **Execution:** Use the established writing-plans, TDD and verification workflow, sequentially in the main task per repository AGENTS.md. No independent-agent review is claimed.

**Goal:** Add `GET /api/external/cron/jobs`, preserving the input/header/response contract of `GET /api/cron/jobs` while avoiding tenant initialization and runtime startup. Retire the mistakenly requested external Chat list.

**Architecture:** A separate router reads existing tenant/Agent `jobs.json` through `JsonJobRepository`. It resolves config strictly without repair, copies state from an already-loaded CronManager when present, and uses shared pure task rendering plus the existing cron display-time calculation. Exact middleware path exemptions apply only to the new list. The old jobs route and Console remain as they are.

**Tech Stack:** FastAPI, Pydantic, existing Cron models/repository, pytest in the project WSL virtual environment.

**Spec:** User correction in this task: jobs, not chats; retain the earlier requirement for identical parameters/headers/response fields and no initialization.

## Contract

- No query parameters or request body, matching the old jobs list. Extra query parameters are ignored as on the old route.
- Existing identity/auth policy: tenant and source headers required; user header optional; Agent header optional, selecting the persisted active Agent by default.
- Return `list[CronJobListItem]`, including the existing `state` and `task` structures. Do not add Chat-style filters, pagination, mandatory user identity or a new null-status contract.
- User identity affects `task.visible_in_my_tasks`, not which jobs are listed.
- Missing jobs file returns `[]`, even without initialized tenant directories. Existing default jobs remain readable if config is absent.
- No bootstrap, Workspace/CronManager construction or startup, schedule registration, Monitor sync, automatic config repair, or task-chat binding creation.
- Missing task bindings stay missing; response fields remain compatible, but the old route's write-on-read repair is intentionally excluded. No synthetic Chat IDs.
- Existing runtime state is copied, not mutated. Next display run times use the same calculation as the old list. Without a runtime, default `CronJobState` is used; no execution history is invented.
- Preserve tenant/source/Agent path boundaries and all unrelated staged/unstaged work.

## Task 1: Retire the mistaken Chat endpoint

- [x] Verify the four task-owned Chat source/test/plan/playbook files are absent from HEAD, remove only their index entries and files, and remove their router/middleware/README additions. Keep every unrelated change.

## Task 2: Add the read-only jobs endpoint and shared task projection

Files:
- Create `src/swe/app/routers/external_jobs.py`.
- Create `src/swe/app/crons/task_view.py` with the pure existing task-view rules; `CronManager.build_task_view` delegates to it without behavior changes.
- Modify only the matching import/method in `src/swe/app/crons/manager.py`, router registration and exact workspace exemptions.
- Create `tests/unit/app/test_external_job_list.py`.

- [x] Write HTTP tests: missing tenant stays empty; current jobs and optional headers match old response JSON; user identity controls task visibility only; missing bindings/files cause no writes; known state and next-run times match old rendering; source/tenant/Agent boundaries and auth remain enforced; corrupt data is not repaired; original jobs still pass through bootstrap.
- [x] Run `venv/bin/python -m pytest tests/unit/app/test_external_job_list.py -q` under WSL; observed module-not-found before implementation.
- [x] Implement strict path/config reads and job projection. Do not call the old handler, because its `_ensure_task_binding_for_read` writes and may register jobs.
- [x] Run the new suite plus `test_cron_task_view.py`, `test_cron_utils.py`, `test_cron_json_repo.py`, and middleware regression tests. Run isolated Cron API tests separately to keep their module stubs separate from the ordinary import graph.

## Task 3: Document and verify

- [x] Create `analysis/playbook/external-job-list.md` with request/response examples and the read-only binding/state boundary; add its README entry.
- [x] Review contract, no-write behavior, isolation, and rollback scope in the main task. Run formatting/static checks and `git diff --check`.
- [x] Confirm no external-chat files, registrations or exemptions remain. Report verification and any limitations. No commit or push.

## Impact analysis

GitNexus reports LOW for the workspace exemption (one direct caller, no affected processes) and `CronManager.build_task_view` (two direct callers: jobs list/detail; ten upstream symbols in total). The shared extraction preserves its signature and behavior. Chat rollback symbols also report LOW.

## Verification results

- New HTTP list, task projection, cron display-time and JSON repository suites: **35 passed**.
- Existing isolated Cron API suite: **50 passed**.
- Tenant workspace/identity middleware suites: **30 passed, 35 skipped** (existing skipped cases).
- Total across these focused commands: **115 passed, 35 skipped**, all commands exited successfully using the project WSL virtual environment.
- Black, Flake8, Mypy and Pylint checks for the new Python files pass; `git diff --check` passes.
- Six HTTP comparisons against the original jobs handler cover optional user identity and loaded/unloaded runtime state, asserting the full response JSON for already-bound jobs. File snapshots and forbidden constructors verify no new directories/bindings, and copied-state assertions verify no runtime-state mutations.
- The old Chat artifacts and their index entries are absent. Pre-existing Cron/batch/frontend changes remain in place. The new ignored playbook file is marked intent-to-add for review; no unrelated index entries were modified.
