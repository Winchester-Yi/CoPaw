# high-frequency-question-apis Specification

## MODIFIED Requirements

### Requirement: High-Frequency Question Result Lookup

The monitor service SHALL provide a result lookup API that returns successful
result rows for the caller's source with batch and topic skill metrics.

#### Scenario: Return available result metrics

- **GIVEN** a request to `GET /api/monitor/high-frequency-question/results`
- **AND** the request contains `X-Source-Id`, `start_time`, and `end_time`
- **WHEN** a successful result batch exists for the same normalized criteria
- **THEN** the service returns `message_count`, `user_count`,
  `total_skill_used_count`, `topic_count`, and `skill_gap_topic_count`
- **AND** each topic includes `skill_used_count` and nullable `top_skill`
- **AND** each topic keeps the existing `bbk_dis` object response shape.

#### Scenario: Count skill gap topics

- **GIVEN** a successful result batch with topic rows
- **WHEN** one or more returned topics have
  `skill_used_count / message_count < 50%`
- **THEN** `skill_gap_topic_count` equals the number of those returned topics.

#### Scenario: Return empty metric defaults

- **GIVEN** a valid result lookup request
- **WHEN** no successful result exists for the same normalized criteria
- **THEN** the service returns `state = EMPTY`
- **AND** returns zero for `message_count`, `user_count`,
  `total_skill_used_count`, `topic_count`, and `skill_gap_topic_count`.
