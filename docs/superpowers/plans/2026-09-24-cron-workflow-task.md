# Workflow Scheduled Jobs

## Scope and decisions

Add `workflow` as a third Scheduled Job type. Its execution engine is independent
of AgentRunner and the existing agent/text executor. It invokes one configured
synchronous JSON HTTP endpoint once per Scheduled Run attempt, then uses the
existing Cron task Chat, trace, execution-history, unread-pause and notification
paths. Ordinary, manual, broadcast and batch-dispatched runs are supported.

Only workflow jobs carry `workflow_binding_id`. Unless explicitly selected, the
binding resolves from the first normalized `skill_ids` entry. One shared binding
applies to broadcast recipients; per-recipient identity and credentials come
from that recipient's runtime-scope `.secret/envs.json`. `sapId` must match the
recipient. Configured `provider_id/model_id` is a dispatch capacity identity,
not an execution model. One HTTP request occurs per attempt; Scheduler owns
whole-task retry. User-facing task UI shows the skill result, not HTTP details.

Published binding versions are immutable. Ordinary/manual runs select the latest
published version at execution start. Scheduler freezes binding version and
dispatch model when a batch is created, and retries retain that version.

## Repository constraints

- `src/swe/app/crons/models.py` currently accepts only agent/text.
- `src/swe/app/crons/executor.py` sends every non-text job to Agent; workflow
  must branch at CronManager and never enter that executor.
- `src/swe/app/crons/manager.py`, `task_view.py`, `api.py`,
  `monitor_sync_client.py` and the Console have explicit agent/text gates.
- Scheduler reads `swe_cron_jobs` and resolves model identity from its row/meta.
  Its workflow branch must read the shared binding version instead of trusting
  callback model fields.
- Monitor ordinary execution sync is best-effort; dispatch execution feedback
  follows the existing synchronous retry path. Preserve these semantics.
- Existing `skill_ids` is an association, not an instruction to load a Skill.

## Implementation units

### 1. Versioned configuration and task contract

Add versioned binding storage in the shared SWE/Monitor/Scheduler database,
including endpoint/method, declarative header/query/body mappings, business
success rule, result-field selectors, registered renderer, timeout, and dispatch
provider/model. Store credential references, never secret values. Add a nullable
`workflow_binding_id` to Cron job definitions and Monitor job projection;
validate required-only-for-workflow semantics. Resolve the first Skill ID during
create/update and copy the binding to broadcast children. Expose source-scoped
management APIs for publishing and reading configurations.

Likely files: `src/swe/app/crons/models.py`, `api.py`, new
`src/swe/app/crons/workflow/` config models/repository/router,
`src/swe/app/crons/monitor_sync_client.py`,
`monitor/src/monitor/app/models/cron.py`,
`monitor/src/monitor/app/services/cron/sync_service.py`,
`monitor/src/monitor/app/database/schema.py`, `scripts/sql/`.

Test first: `tests/unit/app/test_cron_workflow_config.py`,
`tests/unit/app/test_tenant_cron_api.py`, `monitor/tests/test_cron_workflow_sync.py`.
Cases: first Skill binding, absent/disabled binding, agent/text without binding,
type transitions, published-version immutability, recipient binding copy,
Monitor projection. Expect tests to fail before implementation and pass after.

### 2. Independent workflow engine and Cron adapter

Add a workflow engine that accepts resolved invocation configuration and returns
a typed outcome. It does not import or call AgentRunner. Declarative mappings
read fixed values, runtime identity values and tenant-scoped secret references.
Support synchronous JSON, bounded output, HTTP and business success checks,
field extraction, direct display and registered renderers. The Cron adapter
owns trace and task-session persistence, and returns a shared execution result.
Reuse CronManager's source-config boundary, concurrency, outcome finalization,
Monitor execution record and Scheduler feedback. Persist the rendered task
message before reporting success; deduplicate visible messages for a logical run.

Likely files: new `src/swe/app/crons/workflow/` engine, input resolver,
renderer and adapter; new shared Cron execution result/context modules;
`src/swe/app/crons/manager.py`; `src/swe/app/workspace/workspace.py`.

Test first: `tests/unit/app/test_cron_workflow_engine.py`,
`tests/unit/app/test_cron_workflow_manager.py`.
Cases: no AgentRunner call, per-recipient env scope, identity mismatch, mapping,
HTTP/business failure, timeout/cancel, renderer failure, trace completion,
session write before success, redacted snapshots, execution status and
same-attempt replay. Expect focused red-to-green validation.

### 3. Batch version/model ownership

Scheduler reads the workflow binding associated with each workflow job at batch
creation, freezes version/provider/model in intent payload, and passes version
to SWE on dispatch. A later config publish affects new batches only. Agent/text
retain current model resolution. Ensure a workflow job's Monitor definition is
visible before enabling its batch timer.

Likely files: `scheduler/src/scheduler/app/services/cron/scheduling_service.py`,
`scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`,
`scheduler/src/scheduler/app/database/schema.py`, and SWE callback handling.

Test first: `tests/unit/scheduler/test_cron_workflow_dispatch.py` and targeted
SWE callback tests. Cases: parent/child model pools, changed config after batch
creation, new batch selecting new version, retry preserving version, missing
binding/version failing closed, and agent/text callback compatibility.

### 4. User and operational surfaces

Expose workflow task creation/editing with Skill selection. Hide the HTTP call
mechanism from ordinary task and result views. Extend task Chat visibility,
unread-pause, success notification, Monitor schedule distribution and CLI task
creation/update. Keep config management restricted to authorized operators.

Likely files: `console/src/api/types/cronjob.ts`,
`console/src/pages/Control/CronJobs/`, `src/swe/cli/cron_cmd.py`,
`src/swe/app/crons/task_view.py`, `manager.py`, `monitor_sync_client.py`,
`monitor/src/monitor/app/models/cron.py`,
`monitor/src/monitor/app/services/cron/query_service.py`.

Test first: focused Console Vitest, CLI, Monitor distribution and notification
tests. Cases: workflow form validation, no HTTP internals in ordinary UI,
visible task result, unread auto-pause, notification delivery, and unchanged
agent/text behavior.

## Verification and delivery

Run focused tests after each unit, then affected SWE, Scheduler, Monitor and
Console suites; typecheck/lint changed files and run `git diff --check`.
Use GitNexus impact before editing each existing symbol and detect-changes before
any commit. Do not commit or push unless requested. Existing repository
instructions require sequential main-thread work, so the unavailable
Superpowers subagent/review commands are not counted as independent review.

## Remaining implementation choices

Use an allowlisted, published endpoint configuration. Treat missing required
result fields as failures. Keep endpoint credentials and raw HTTP payloads out
of task-visible records. Configuration/identity errors are terminal for one
attempt; Scheduler retains its existing whole-task retry policy unless its
existing terminal-error classification applies.
