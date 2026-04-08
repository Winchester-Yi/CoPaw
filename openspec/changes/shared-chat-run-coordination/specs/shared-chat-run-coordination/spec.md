## ADDED Requirements

### Requirement: Interactive run ownership SHALL be shared across backend instances
The backend SHALL maintain interactive chat run ownership in shared coordination state so any backend instance can determine whether a run is active and which instance owns execution.

#### Scenario: Non-owner instance can observe a running chat
- **WHEN** one backend instance starts an interactive chat run
- **THEN** another backend instance SHALL be able to determine from shared coordination state that the run is active and identify its owner

### Requirement: Stop requests SHALL work from any backend instance
The backend SHALL route interactive stop requests through shared coordination so stop semantics do not depend on reaching the owning backend instance directly.

#### Scenario: Stop request reaches a non-owner instance
- **WHEN** an active chat run is owned by one backend instance and a stop request is served by another
- **THEN** the backend SHALL signal cancellation through shared coordination and the owner SHALL stop the run

#### Scenario: Stop request for a non-running chat returns a deterministic result
- **WHEN** a stop request targets a chat run that is no longer active in shared coordination state
- **THEN** the backend SHALL return a deterministic non-running result rather than attempting a best-effort local cancellation

### Requirement: Chat running-status APIs SHALL use shared run liveness
The backend SHALL answer interactive running-status queries from shared coordination state rather than only from local in-memory trackers.

#### Scenario: Chat list status remains consistent across instances
- **WHEN** different backend instances serve chat list or chat detail requests for the same active run
- **THEN** they SHALL report the same running state while shared coordination indicates the run is active

### Requirement: Reconnect discovery SHALL use shared run coordination
The backend SHALL allow reconnect requests to discover an active interactive run from shared coordination state.

#### Scenario: Reconnect lands on a non-owner instance
- **WHEN** a reconnect request for an active chat run is handled by a backend instance that did not start the run
- **THEN** that instance SHALL be able to determine from shared coordination that the run is active
