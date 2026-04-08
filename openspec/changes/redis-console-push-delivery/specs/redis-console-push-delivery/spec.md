## ADDED Requirements

### Requirement: Console push delivery SHALL be shared across backend instances
The backend SHALL store console push messages in Redis so messages written by one backend instance are readable by another backend instance for the same tenant session.

#### Scenario: Push written on one instance is read on another
- **WHEN** one backend instance appends a push message for a tenant session
- **THEN** another backend instance serving `/api/console/push-messages` for that tenant session SHALL be able to retrieve that message

### Requirement: Console push reads SHALL remain tenant-and-session scoped
The backend SHALL isolate console push delivery by both tenant and session.

#### Scenario: Polling does not leak messages across sessions
- **WHEN** push messages exist for multiple sessions in the same tenant
- **THEN** a polling request for one session SHALL return only the messages for that session

#### Scenario: Polling does not leak messages across tenants
- **WHEN** push messages exist for different tenants
- **THEN** a polling request for one tenant session SHALL NOT return messages from another tenant

### Requirement: Console push delivery SHALL remain bounded and short-lived
The backend SHALL preserve bounded retention and expiration behavior for console push messages stored in Redis.

#### Scenario: Expired messages are not returned
- **WHEN** a push message exceeds the configured maximum age before it is polled
- **THEN** the backend SHALL omit that message from retrieval

#### Scenario: Message count remains bounded
- **WHEN** the number of queued push messages for a tenant session exceeds the configured maximum
- **THEN** the backend SHALL trim older messages so the queue remains bounded
