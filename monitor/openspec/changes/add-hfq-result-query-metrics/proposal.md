# High Frequency Question Result Query Metrics - Proposal

## Summary

Expose persisted batch and topic skill metrics through
`GET /api/monitor/high-frequency-question/results` so the Console can render
the final high-frequency question analysis modal.

## Motivation

The result-save path now persists batch user counts, batch skill coverage, topic
skill coverage, and top skill names. The result lookup API still returns only
topic counts and examples, so the frontend cannot render the summary cards or
per-topic skill coverage shown in the target UI.

## Scope

- Add top-level result summary metrics to `GET /results`.
- Add per-topic `skill_used_count` and `top_skill` to result topics.
- Keep `bbk_dis` as the existing object shape because the Console already maps
  branch IDs to names.
- Add focused backend tests for result lookup response fields.

## Out of Scope

- Console frontend changes.
- `POST /api/monitor/high-frequency-question/results`.
- Source message query behavior.
- External workflow dispatch behavior.
- Task submission, prewarm, running-task deduplication, and cache policy.
