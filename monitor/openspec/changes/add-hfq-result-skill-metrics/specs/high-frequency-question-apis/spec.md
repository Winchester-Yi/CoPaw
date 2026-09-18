# high-frequency-question-apis Specification

## MODIFIED Requirements

### Requirement: Result Batch Save API

The monitor service SHALL provide an idempotent batch save API for
AI-generated high-frequency question analysis results, including batch-level and
per-topic skill coverage metrics.

#### Scenario: Save a complete result batch with skill metrics

- **GIVEN** a request to `POST /api/monitor/high-frequency-question/results`
- **AND** the request body contains `batch_id`, `stat_start_time`,
  `stat_end_time`, and non-empty `results`
- **AND** every result row contains `valid_message_count`, `user_count`,
  `total_skill_used_count`, `message_count`, `skill_used_count`, and optional
  `top_skill`
- **WHEN** all result rows pass validation
- **THEN** the service opens one database transaction
- **AND** deletes existing rows from `swe_high_frequency_question_result` for
  the same `source_id + batch_id`
- **AND** batch inserts the new rows with `user_count`,
  `total_skill_used_count`, `skill_used_count`, and `top_skill`
- **AND** commits the transaction.

#### Scenario: Reject invalid skill metrics

- **GIVEN** a result save request
- **WHEN** `user_count` is negative
- **OR** `total_skill_used_count` is negative
- **OR** `total_skill_used_count` exceeds `valid_message_count`
- **OR** `skill_used_count` is negative
- **OR** `skill_used_count` exceeds `message_count`
- **THEN** the service rejects the request before writing to the database.

#### Scenario: Reject inconsistent batch-level metrics

- **GIVEN** a result save request with multiple topic rows
- **WHEN** rows in the same request disagree on `valid_message_count`,
  `user_count`, or `total_skill_used_count`
- **THEN** the service rejects the request before writing to the database.

#### Scenario: Save array-shaped branch distribution

- **GIVEN** a valid result save request
- **AND** a result row contains `bbk_dis` as an array of branch count objects
- **WHEN** the service saves the batch
- **THEN** it stores `bbk_dis` as JSON text without rejecting the array shape.
