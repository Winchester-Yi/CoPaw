# High Frequency Question Console Modal Metrics - Design

## Data Contract

The modal continues to call `GET /api/monitor/high-frequency-question/results`
through the existing Console monitor API module. The response is treated as the
source of truth for:

- Batch summary: `message_count`, `user_count`, `total_skill_used_count`,
  `topic_count`, `skill_gap_topic_count`.
- Topic metrics: `skill_used_count`, nullable `top_skill`.
- Existing topic fields: `message_count`, `valid_message_count`, `bbk_dis`,
  and `sample_questions`.

`bbk_dis` remains a `Record<string, number>` and Console keeps mapping branch
IDs to display names locally.

## UI Behavior

When a result is available, the modal renders:

- The existing status bar and result update time.
- A summary grid with participating messages, participating users, overall
  skill coverage, and skill-gap topic count.
- The TOP10 list with existing topic title, sample questions, branch
  distribution, and total topic share.
- A new skill coverage column per topic showing covered message count, coverage
  percentage, and the top skill tag. Missing `top_skill` is displayed as no
  skill.

The empty, loading, and running-task states remain unchanged.

## Compatibility

The frontend assumes the new fields are present on available results. Display
helpers still guard division by zero and nullable top skill values so older or
empty payloads do not break rendering.
