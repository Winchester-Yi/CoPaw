# High Frequency Question Console Modal Metrics - Proposal

## Summary

Update the Console high-frequency question analysis modal to render the result
summary metrics and per-topic skill coverage fields returned by
`GET /api/monitor/high-frequency-question/results`.

## Motivation

The backend result lookup API now exposes batch message count, user count,
skill-covered message count, skill-gap topic count, and each topic's skill
coverage and top skill. The Console modal still shows only topic counts,
examples, and branch distribution, so it cannot match the target UI.

## Scope

- Extend Console API typings for the new result fields.
- Render top summary cards for message count, user count, skill coverage, and
  skill-gap topics.
- Render per-topic skill coverage progress and top skill labels.
- Preserve existing result lookup, task submission, running-task polling,
  branch-scope behavior, and `bbk_dis` object handling.

## Out of Scope

- Backend API changes.
- Console route changes outside the messages analytics page.
- Changing high-frequency task submission, prewarm, cache, or workflow logic.
