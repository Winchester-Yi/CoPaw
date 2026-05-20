## Why

The system currently has no dedicated API for reading text assets from `SWE_WORKING_DIR/asset` or for publishing UTF-8 text into a tenant scope's public static directory. Existing `copy_file_to_static` behavior is agent-scoped and relies on the current runtime context, which is not a fit for service-to-service publishing or anonymous external publishing.

## What Changes

- Add an internal read API that accepts a file name and returns UTF-8 text from `SWE_WORKING_DIR/asset/<file_name>`.
- Add an internal write API that accepts `user_id`, `source_id`, and UTF-8 text content, generates an `.html` file name from `user_id + timestamp`, writes into the resolved tenant scope's `workspaces/default/static` directory, and returns a permanent public URL.
- Add matching external read and write APIs that expose the same behavior without requiring internal authorization headers.
- Keep the public URL format aligned with the existing static file route and `FILE_URL` base URL behavior.
- Enforce 404 for missing asset files and reject invalid file names or invalid UTF-8 payloads.

## Capabilities

### New Capabilities

- `internal-text-asset-apis`: internal and external text asset read/write endpoints with permanent public URL publishing.

### Modified Capabilities

- None.

## Impact

- Backend:
  - New internal and external file asset endpoints.
  - New scope-aware file publishing helper and filename generator.
  - New validation for asset file names and UTF-8 content.
- Tests:
  - New coverage for 404, invalid UTF-8, generated names, scope resolution, URL generation, and anonymous external access.
