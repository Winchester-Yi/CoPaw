# high-frequency-question-apis Specification

## MODIFIED Requirements

### Requirement: High-Frequency Question Console Result Modal

The Console SHALL display available high-frequency question analysis results
with batch summary metrics and per-topic skill coverage metrics.

#### Scenario: Display available result summary metrics

- **GIVEN** the Console modal has queried
  `GET /api/monitor/high-frequency-question/results`
- **AND** the response state is `AVAILABLE` or `AVAILABLE_STALE`
- **WHEN** the modal renders the result content
- **THEN** it displays `message_count`, `user_count`,
  `total_skill_used_count / message_count` as skill coverage, and
  `skill_gap_topic_count / topic_count` in summary cards.
- **AND** `skill_gap_topic_count` represents topics whose skill coverage is
  below 50%.

#### Scenario: Display topic skill coverage

- **GIVEN** a returned topic includes `message_count`, `skill_used_count`, and
  nullable `top_skill`
- **WHEN** the topic row renders
- **THEN** the row displays skill coverage as
  `skill_used_count / message_count`
- **AND** displays `top_skill` when present
- **AND** displays an empty-skill state when `top_skill` is null or empty.

#### Scenario: Preserve existing modal behavior

- **GIVEN** the Console modal is opened, refreshed, or used to submit a new
  high-frequency question analysis task
- **WHEN** this change is applied
- **THEN** existing filters, branch-scope behavior, result lookup, task
  submission, running-task polling, and `bbk_dis` object-to-branch-name mapping
  continue to work without API route changes.
