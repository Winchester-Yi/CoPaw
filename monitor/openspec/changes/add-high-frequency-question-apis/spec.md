# High Frequency Question APIs - Specification

## Endpoints

### POST /monitor/high-frequency-question/messages

Returns source user messages for offline high-frequency question analysis.

**Request Body**

```json
{
  "source_id": "RMASSIST",
  "start_time": "2026-07-23 00:00:00",
  "end_time": "2026-07-30 00:00:00",
  "bbk_id": null
}
```

**Behavior**

- `source_id`, `start_time`, and `end_time` are required.
- `start_time` must be earlier than `end_time`.
- Time span must not exceed 31 days.
- The database query MUST use parameter binding.
- The query MUST read from `swe_tracing_traces`.
- The query MUST use these fields:
  - `source_id`
  - `user_id`
  - `bbk_id`
  - `user_message` as `content`
  - `skills_used`
  - `status`
- The query MUST filter:
  - `source_id = request.source_id`
  - `start_time >= request.start_time`
  - `start_time < request.end_time`
  - `status = 'completed'`
  - `session_id NOT LIKE 'cron-task%'`
  - `user_message IS NOT NULL`
  - `TRIM(user_message) != ''`
  - `TRIM(user_message)` not in the configured meaningless-text blacklist
  - optional `bbk_id = request.bbk_id`
- Results MUST be ordered by `start_time ASC, trace_id ASC`.
- The response MUST include `message_count` as the returned message row count.
- The response MUST include `user_count` as the count of distinct non-empty
  `user_id` values in the returned rows.
- The endpoint MUST return an explicit error if more than 10000 messages match.
- Logs MUST NOT include full `user_message` content.

**Response Body**

```json
{
  "total": 4000,
  "message_count": 4000,
  "user_count": 210,
  "data": [
    {
      "user_id": "136807",
      "bbk_id": "110",
      "content": "帮我查询这个客户目前有哪些保险产品",
      "skills_used": ["客户分析"]
    }
  ]
}
```

### POST /monitor/high-frequency-question/results

Saves a complete AI-generated high-frequency question result batch.

**Request Body**

```json
{
  "batch_id": "HFQ_20260730_030000",
  "stat_start_time": "2026-07-23 00:00:00",
  "stat_end_time": "2026-07-30 00:00:00",
  "results": [
    {
      "scope_type": "ALL",
      "bbk_id": "ALL",
      "rank_no": 1,
      "topic_name": "查询客户保险持仓",
      "message_count": 520,
      "valid_message_count": 4000,
      "user_count": 1200,
      "total_skill_used_count": 2600,
      "skill_used_count": 380,
      "top_skill": "保险助手",
      "bbk_dis": [
        {
          "bbk_id": 110,
          "count": 300
        },
        {
          "bbk_id": 121,
          "count": 220
        }
      ],
      "sample_questions": [
        "查询客户目前有哪些保险产品"
      ]
    }
  ]
}
```

**Behavior**

- `batch_id`, `stat_start_time`, `stat_end_time`, and non-empty `results` are required.
- `stat_start_time` must be earlier than `stat_end_time`.
- `scope_type` MUST be `ALL` or `ORG`.
- For `scope_type = ALL`, `bbk_id` MUST be `ALL`.
- For `scope_type = ORG`, `bbk_id` MUST be non-empty and MUST NOT be `ALL`.
- `rank_no` MUST be 1 through 10.
- `topic_name` MUST be non-empty after trimming.
- Counts MUST be non-negative.
- `message_count` MUST NOT exceed `valid_message_count`.
- `total_skill_used_count` MUST NOT exceed `valid_message_count`.
- `skill_used_count` MUST NOT exceed `message_count`.
- `valid_message_count`, `user_count`, and `total_skill_used_count` MUST be
  consistent across all rows in the same request batch.
- `sample_questions` MUST contain at most 4 items.
- Each `sample_questions` item MUST be at most 1000 characters.
- A single request MUST NOT contain duplicate `batch_id + scope_type + bbk_id + rank_no`.
- Save MUST happen in one database transaction:
  - delete existing rows for the same `batch_id`
  - batch insert the full new result set
  - roll back the delete and insert if any step fails
- Logs MUST NOT include full `sample_questions`.

**Response Body**

```json
{
  "batch_id": "HFQ_20260730_030000",
  "saved_count": 120
}
```
