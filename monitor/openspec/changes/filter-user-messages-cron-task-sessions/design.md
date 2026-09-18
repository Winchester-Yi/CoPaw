# Filter User Messages Cron Task Sessions - Design

## API Compatibility

The existing user-message APIs keep their current default behavior. A new
boolean query parameter, `exclude_cron_task_sessions`, controls the additional
filter:

- `false` or omitted: keep existing behavior.
- `true`: exclude rows whose `session_id` starts with `cron-task`.

The Console user-messages page passes `true` for both the paginated list and
export request so the table, pagination total, and exported file share the same
scope.

## Query Behavior

When the flag is true, `TracingQueryService.get_user_messages()` appends this
condition to the shared WHERE clause before both the count query and row query:

```sql
(session_id IS NULL OR session_id NOT LIKE 'cron-task%')
```

This keeps rows without a session ID visible while excluding cron task sessions.

## Console Behavior

The user-messages table hides conversation ID (`trace_id`) and session ID
(`session_id`) columns by default. A header action button toggles both columns.
The table still uses `trace_id` as `rowKey`; hiding the column only affects
visible cells.
