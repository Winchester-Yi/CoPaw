## Why

Interactive session state is still persisted through shared JSON file reads and writes, which means cross-instance updates can overwrite one another even when the underlying workspace is shared on NFS. This causes the most subtle multi-instance inconsistency: lost or reverted conversation state.

## What Changes

- Introduce coordinated session checkpoint metadata so session persistence has an authoritative cross-instance version boundary.
- Prevent silent last-writer-wins overwrite of session state across backend instances.
- Separate durable session checkpoint facts from raw checkpoint blob storage so NFS remains a file store, not the source of truth for the latest checkpoint version.
- Add migration behavior that preserves existing session files while introducing coordinated writes.

## Capabilities

### New Capabilities
- `coordinated-session-state-persistence`: Persist session checkpoints through authoritative shared version metadata so multi-instance writes do not silently overwrite one another.

### Modified Capabilities
- None.

## Impact

- Affected modules: `src/swe/app/runner/session.py`, session dependency wiring, session-related APIs and save/load paths
- New dependency usage: MySQL becomes authoritative for session checkpoint metadata; NFS may remain the blob store for raw checkpoint files
- Removes silent shared-file overwrite as the default session persistence behavior
