# High Frequency Question Result Skill Metrics - Proposal

## Summary

Extend the high-frequency question result-save payload and persistence path with
batch-level user and skill coverage counts, plus per-topic skill coverage
metrics.

## Motivation

The current result table stores topic counts and sample questions, but it cannot
support UI displays for participating users, overall skill coverage, or
per-topic skill coverage. The database table has already been extended in the
deployment environment, so Monitor needs to accept, validate, and persist the new
fields from the external analysis workflow.

## Scope

- Update `POST /api/monitor/high-frequency-question/results` request validation.
- Persist `user_count`, `total_skill_used_count`, `skill_used_count`, and
  `top_skill` into `swe_high_frequency_question_result`.
- Accept the newer array-shaped `bbk_dis` payload and store it as JSON text.
- Add focused backend tests for validation and insert parameters.

## Out of Scope

- `GET /api/monitor/high-frequency-question/results` response shape.
- Console frontend display.
- Source message query behavior.
- External workflow dispatch behavior.
- Task submission, prewarm, running-task deduplication, and cache policy.
