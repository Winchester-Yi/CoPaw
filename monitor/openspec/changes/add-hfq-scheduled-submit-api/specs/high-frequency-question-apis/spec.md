# high-frequency-question-apis Specification

## ADDED Requirements

### Requirement: High-Frequency Question Scheduled Submission

The monitor service SHALL provide a scheduler-facing high-frequency question
submission API that uses request-body source information and creates a new
workflow task for the latest seven calendar days.

#### Scenario: Submit scheduled all-organization task

- **GIVEN** a request to
  `POST /api/monitor/high-frequency-question/scheduled-tasks`
- **AND** the JSON body contains `source_id`
- **AND** the JSON body omits `bbk_id`
- **WHEN** the service handles the request
- **THEN** it computes `end_time` as today `23:59:59` by server date
- **AND** computes `start_time` as six days before today `00:00:00`
- **AND** submits through the existing task flow with `force = true`
- **AND** returns `state = RUNNING` with a generated `task_id` and `batch_id`
- **AND** normalizes the scope to `scope_type = ALL` and `bbk_id = ALL`.

#### Scenario: Submit scheduled organization task

- **GIVEN** a request to
  `POST /api/monitor/high-frequency-question/scheduled-tasks`
- **AND** the JSON body contains `source_id` and `bbk_id`
- **WHEN** the service handles the request
- **THEN** it submits through the existing task flow with `force = true`
- **AND** normalizes the scope to `scope_type = ORG`
- **AND** uses the provided `bbk_id`.

#### Scenario: Do not depend on headers

- **GIVEN** a scheduler request without `X-Source-Id`, `X-User-Id`, or
  `X-User-Name`
- **WHEN** the scheduled submission API is called
- **THEN** the service uses the body `source_id`
- **AND** records actor fields as the system scheduler identity.
