## ADDED Requirements

### Requirement: Interactive run ownership SHALL be shared across backend instances
The backend SHALL maintain interactive run ownership in shared coordination state so any instance can determine whether a chat run is active and which instance currently owns execution.

#### Scenario: A non-owner instance can observe a running chat
- **WHEN** one backend instance starts an interactive chat run for a tenant session
- **THEN** another backend instance SHALL be able to read shared run state and determine that the chat is running without relying on its own local in-memory tracker

#### Scenario: Shared ownership expires when the owner disappears
- **WHEN** the owning backend instance stops renewing the run ownership heartbeat
- **THEN** the shared run state SHALL expire or transition so other instances do not continue reporting stale active ownership indefinitely

### Requirement: Stop requests SHALL work from any backend instance
The backend SHALL route interactive stop semantics through shared coordination state so a stop request does not depend on reaching the instance that received the original chat request.

#### Scenario: Stop request reaches a non-owner instance
- **WHEN** an interactive chat run is owned by one backend instance and a stop request is served by a different backend instance
- **THEN** the backend SHALL signal cancellation through shared coordination and the owning instance SHALL stop the run

#### Scenario: Stop request for a non-running chat is handled deterministically
- **WHEN** a stop request targets a chat run that is no longer active in shared coordination state
- **THEN** the backend SHALL return a deterministic non-running result rather than attempting a local best-effort cancellation

### Requirement: Console push delivery SHALL be instance-agnostic
The backend SHALL store console push messages in shared runtime delivery state so messages written by one backend instance are retrievable by another backend instance for the same tenant session.

#### Scenario: Push written on one instance is read on another
- **WHEN** a backend instance writes a console push message for a tenant session
- **THEN** a different backend instance serving the session's push polling request SHALL be able to retrieve that message

#### Scenario: Push reads remain tenant-and-session scoped
- **WHEN** push messages exist for multiple tenants or sessions
- **THEN** shared push retrieval SHALL return only the messages for the requested tenant and session

### Requirement: Interactive status APIs SHALL derive running state from shared runtime coordination
The backend SHALL answer interactive running-status queries from shared coordination state rather than only from per-process in-memory trackers.

#### Scenario: Chat list status is consistent across instances
- **WHEN** different backend instances serve repeated chat list or chat detail requests for the same active chat
- **THEN** they SHALL report the same running state while the shared run coordination remains active

#### Scenario: Reconnect can discover active run state from another instance
- **WHEN** a reconnect request is served by a backend instance that did not start the original interactive run
- **THEN** that instance SHALL be able to discover that the run is active from shared coordination state
