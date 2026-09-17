# High Frequency Question Scheduled Submit API - Design

## API

Add:

```http
POST /monitor/high-frequency-question/scheduled-tasks
Content-Type: application/json

{
  "source_id": "RMASSIST",
  "bbk_id": "110"
}
```

`bbk_id` is optional. When omitted, the existing normalization maps the request
to `scope_type = ALL` and `bbk_id = ALL`.

## Time Window

The service calculates the time window at request handling time:

- `end_time`: today `23:59:59` by server date.
- `start_time`: six days before today `00:00:00`.

This gives the scheduler the latest seven calendar days while avoiding
scheduler-side date math.

## Task Creation

The scheduled endpoint calls the existing `submit_task()` path with
`force = true`. This keeps batch/task creation, async task row shape, workflow
payload, workflow completion handling, and result polling identical to the
interactive task flow. Because `force = true`, every scheduled invocation starts
a new task and generates a new `task_id`; Monitor uses that same value as the
workflow `batch_id`.

The external workflow payload receives `batch_id`, not the internal `task_id`.

Actor fields use the existing system scheduler identity.

## Compatibility

The endpoint does not read `X-Source-Id`, `X-User-Id`, or `X-User-Name`.
Existing endpoints keep their current contracts.
