# Filter User Messages Cron Task Sessions - Proposal

## Summary

Let the Console user-messages page exclude tracing messages whose `session_id`
starts with `cron-task`, without changing the default behavior of existing
Monitor tracing APIs.

## Motivation

The user-messages page is intended for manually initiated user conversations.
Cron task traces share the same storage table and currently appear in the
message list and total count, which makes the page noisy. Directly changing the
existing endpoint default could affect external callers, so the filter should be
opt-in.

## Scope

- Add an optional `exclude_cron_task_sessions` query parameter to
  `GET /monitor/tracing/user-messages`.
- Apply the same optional parameter to
  `GET /monitor/tracing/user-messages/export`.
- Make the Console user-messages page pass the parameter for both list and
  export.
- Hide conversation ID and session ID columns by default on the Console page,
  with a button to toggle them.

## Out of Scope

- Changing default behavior for callers that do not pass the new parameter.
- Changing other tracing pages, overview metrics, high-frequency question
  analysis, or cron analytics.
