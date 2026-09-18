# High Frequency Question Result Skill Metrics - Design

## Current Shape

`POST /api/monitor/high-frequency-question/results` validates a full result
batch with Pydantic models and saves it transactionally by deleting existing
rows for the same `source_id + batch_id`, then batch inserting all replacement
rows into `swe_high_frequency_question_result`.

## Data Contract

Each topic result now carries three batch-level counters and two per-topic skill
fields:

- `user_count`: distinct users participating in the batch. This is batch-level
  and must be the same on every topic row in the request.
- `total_skill_used_count`: number of valid batch messages whose `skills_used`
  is non-empty. This counts messages, not individual skill usages, and must be
  the same on every topic row in the request.
- `skill_used_count`: number of messages in the current topic whose
  `skills_used` is non-empty. This counts messages, not individual skill usages.
- `top_skill`: skill name used by the most messages in the current topic. It may
  be empty or null when the topic has no skill coverage.

`valid_message_count` remains the valid-message total for the batch and must
also stay consistent on every topic row.

## Validation

Validation remains all-or-nothing before any database write. The item-level
checks ensure:

- `user_count >= 0`
- `total_skill_used_count >= 0`
- `total_skill_used_count <= valid_message_count`
- `skill_used_count >= 0`
- `skill_used_count <= message_count`

The request-level check rejects a batch if any topic row disagrees on
`valid_message_count`, `user_count`, or `total_skill_used_count`.

## Persistence

The existing transaction and replacement strategy stays unchanged. The INSERT
column list gains:

- `user_count`
- `total_skill_used_count`
- `skill_used_count`
- `top_skill`

`bbk_dis` continues to be stored as JSON text. The save payload accepts both
the prior object form and the newer array form so the workflow can send branch
distribution rows such as `{ "bbk_id": 110, "count": 300 }`.

## Compatibility

This change intentionally does not expose the new columns through result lookup.
The new fields are persisted first so a later UI and GET-response change can be
implemented separately.
