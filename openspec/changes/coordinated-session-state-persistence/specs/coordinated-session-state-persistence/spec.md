## ADDED Requirements

### Requirement: Session checkpoint identity SHALL use authoritative shared metadata
The backend SHALL store authoritative session checkpoint identity and version metadata in durable shared storage rather than relying on shared JSON files alone as the source of truth.

#### Scenario: Latest checkpoint is resolved through authoritative metadata
- **WHEN** a backend instance loads session state for a tenant session
- **THEN** it SHALL determine the latest valid checkpoint through authoritative shared metadata rather than by assuming a local shared file is current

### Requirement: Session writes SHALL prevent silent stale-writer overwrite
The backend SHALL use coordinated version-aware session persistence so a stale backend instance cannot silently overwrite a newer session checkpoint.

#### Scenario: Stale writer does not replace a newer checkpoint
- **WHEN** two backend instances attempt to persist competing updates for the same session and one writer is operating on stale checkpoint state
- **THEN** the backend SHALL prevent the stale write from silently replacing the newer checkpoint

### Requirement: Raw checkpoint payload storage SHALL be separate from checkpoint authority
The backend SHALL treat raw session checkpoint payloads as file or blob assets referenced by authoritative checkpoint metadata.

#### Scenario: Raw checkpoint payload is not the sole source of truth
- **WHEN** a raw checkpoint payload exists on NFS or equivalent blob storage
- **THEN** the backend SHALL use authoritative shared metadata to determine whether that payload is the current valid checkpoint

### Requirement: Legacy session files SHALL remain migratable during transition
The backend SHALL preserve visibility of existing legacy session files while authoritative checkpoint metadata is introduced.

#### Scenario: Existing legacy session state remains accessible during migration
- **WHEN** a tenant session already has legacy JSON-backed persisted state before coordinated checkpoint metadata is enabled
- **THEN** the backend SHALL provide a migration or compatibility path so that session remains loadable during transition
