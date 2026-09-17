# High Frequency Question Result Query Metrics - Design

## Current Shape

`GET /api/monitor/high-frequency-question/results` selects the newest matching
successful result batch and then reads topic rows from
`swe_high_frequency_question_result`. The response currently exposes topic
counts, branch distribution, and sample questions, but not the newer skill
metric columns.

## Response Contract

The result response gains these top-level summary fields:

- `message_count`: batch valid-message total, read from `valid_message_count`.
- `user_count`: batch distinct user count, read from `user_count`.
- `total_skill_used_count`: batch skill-covered message count, read from
  `total_skill_used_count`.
- `topic_count`: number of topic rows returned.
- `skill_gap_topic_count`: number of returned topics where skill coverage is
  below 50%, calculated as `skill_used_count / message_count < 50%`.

Each topic gains:

- `skill_used_count`: topic messages whose `skills_used` is non-empty.
- `top_skill`: skill name used by the most messages in the topic, nullable.

`bbk_dis` remains `Record<string, number>` in the GET response. The frontend
already maps branch IDs to display names and computes percentages from
`bbk_dis` counts and topic `message_count`.

## Calculation

The batch-level values are persisted identically on every topic row by the
save-path validation. The query response can therefore use the first returned
row for `message_count`, `user_count`, and `total_skill_used_count`. Empty
responses return zero summary values.

`topic_count` is `len(topics)`. `skill_gap_topic_count` is computed from the
returned rows so it reflects the same visible topic set. Use integer
comparison (`skill_used_count * 2 < message_count`) to avoid floating-point
rounding and to keep exactly 50% from counting as a gap.

## Compatibility

Existing fields remain in place. This change only adds response fields and does
not change cache matching, stale-result behavior, task submission, or prewarm.
