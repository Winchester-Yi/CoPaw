## ADDED Requirements

### Requirement: Chat metadata SHALL use durable transactional storage
The backend SHALL store authoritative chat metadata in MySQL rather than shared `chats.json` files so chat operations remain consistent across backend instances.

#### Scenario: Concurrent chat mutations do not overwrite one another
- **WHEN** multiple backend instances concurrently create, update, or delete chat metadata
- **THEN** the authoritative chat store SHALL preserve a deterministic result without whole-file overwrite corruption

#### Scenario: Chat reads remain consistent across instances
- **WHEN** one backend instance successfully mutates chat metadata
- **THEN** another backend instance SHALL read the same resulting chat state from the authoritative durable store

### Requirement: Interactive run facts SHALL remain durably queryable
The backend SHALL persist durable interactive run records independently from ephemeral runtime coordination.

#### Scenario: Completed run remains queryable after runtime ownership expires
- **WHEN** an interactive run completes and its ephemeral coordination state is later removed
- **THEN** the backend SHALL retain a durable run record describing the execution outcome

#### Scenario: Failed run preserves durable failure result
- **WHEN** an interactive run fails
- **THEN** the backend SHALL retain a durable failure record that is readable across backend instances

### Requirement: Migration from shared chat JSON SHALL preserve existing chat visibility
The backend SHALL provide a migration path from existing `chats.json` data before removing JSON as the authoritative chat writer.

#### Scenario: Existing chat metadata is preserved during cutover
- **WHEN** a tenant workspace already contains chat metadata in `chats.json`
- **THEN** the backend SHALL migrate or import that metadata so it remains visible after the authoritative writer switches to MySQL
