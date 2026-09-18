# High Frequency Question Task Flow - Design

## Existing Shape

Monitor already exposes source-message and result-save endpoints through
FastAPI routers, Pydantic models, and direct SQL through the existing database
wrapper. Monitor also has read-only async task APIs for `swe_async_tasks`.

## Task Records

The high-frequency question task flow writes directly to `swe_async_tasks`
because Monitor does not currently have a write-side async task repository.
The write is intentionally narrow and uses the same shared table contract as
the existing async task center.

Created task fields:

- `task_id`: generated UUID, also used as workflow `batch_id`
- `service`: `monitor`
- `task_type`: `monitor.high.freq.question`
- `status`: `running`
- `title`: `用户高频问题分析`
- `summary`: readable date and organization summary
- `source_id`: raw request source id
- `actor_user_id` / `actor_user_name`: caller headers, or system values for
  prewarm
- `target_count`: `1`
- `done_count`: `0`
- `failed_count`: `0`
- `result_json`: JSON object with the normalized request criteria

Final updates set status to `succeeded` or `failed`, update counts, set
`finished_at`, and preserve the `request` object in `result_json`.

## Request Matching

Normalized request criteria are:

- `source_id`
- `start_date`
- `end_date`
- `scope_type`
- `bbk_id`

`bbk_id` is normalized to `ALL` with `scope_type = ALL` when missing, otherwise
`scope_type = ORG` and the trimmed input `bbk_id` is preserved.

Interactive requests resolve `source_id` from the `X-Source-Id` header, matching
the existing tracing query APIs. Body or query `source_id` remains a
compatibility fallback for workflow and scheduler callers, followed by the
existing default source behavior.

Date matching uses day boundaries calculated in Python so SQL does not need
`DATE(column)` on indexed datetime columns.

## Cache Reuse

Submission and query both look for the newest successful result rows with the
same normalized criteria. The 24-hour cache only matches rows whose grouped
`MAX(created_at)` is within the last 24 hours.

If submission finds a 24-hour hit, it returns the existing result and does not
create a task. If no hit exists, submission always creates a new task.

The Console modal separates cache lookup from task creation. Changing filters
puts the result area into a pending-query state. Clicking "查询结果" only calls
the result lookup API. If the API returns `EMPTY`, the modal shows a separate
"生成分析" action; clicking that action submits the task.

## Workflow Dispatch

The submission endpoint writes the `running` task in a short database operation
and schedules an `asyncio.create_task` to call the external workflow outside
that write. The workflow configuration is read from environment-backed monitor
config. Missing configuration fails the background task and marks the task
failed.

The workflow payload includes:

- `source_id`
- `batch_id`
- `start_time`
- `end_time`
- `bbk_id`

The internal async-task `task_id` is not sent to the external workflow. Monitor
uses the generated task id as the workflow `batch_id` and keeps `task_id` only
for internal task status updates.

## Failure Handling

Workflow success requires a JSON response with `message == "success"`. On
unexpected responses or exceptions, Monitor polls
`swe_high_frequency_question_result` for the configured wait window
(`MONITOR_HFQ_RESULT_WAIT_SECONDS`, default 420 seconds) at the configured
interval (`MONITOR_HFQ_RESULT_POLL_INTERVAL_SECONDS`, default 10 seconds). If
rows exist for the same `source_id + batch_id`, the task is marked `succeeded`;
otherwise it is marked `failed` with a truncated safe error message.

## Concurrency

The design intentionally does not prevent duplicate running tasks for the same
criteria. This accepts duplicate work for rare concurrent submissions and keeps
the implementation within the current table structure.
