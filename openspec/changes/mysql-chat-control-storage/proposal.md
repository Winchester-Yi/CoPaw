## Why

Chat metadata is still stored in shared `chats.json` files on NFS, which means multi-instance reads share visibility but not safe mutation semantics. Concurrent creates, updates, and deletes can overwrite one another, and completed run facts are not durably queryable beyond ephemeral runtime state.

## What Changes

- Replace `chats.json` as the authoritative chat metadata store with MySQL-backed repositories.
- Introduce durable run records for interactive chat executions so completed and failed runs remain queryable after ephemeral runtime coordination expires.
- Add migration and dual-read safeguards so existing JSON-backed chat data can be cut over safely.
- Keep NFS-backed files only as transitional import sources or for non-authoritative file assets.

## Capabilities

### New Capabilities
- `mysql-chat-control-storage`: Store chat metadata and durable interactive run facts in MySQL so cross-instance mutations and reads remain consistent.

### Modified Capabilities
- None.

## Impact

- Affected modules: `src/swe/app/runner/repo/json_repo.py`, chat manager wiring, `src/swe/app/runner/api.py`, workspace service factories
- New dependency usage: MySQL becomes authoritative for chat metadata and durable interactive run records
- Removes `chats.json` from the long-term cross-instance control path
