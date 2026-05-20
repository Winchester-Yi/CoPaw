## Context

The codebase already exposes public static files under `/static/{user_id}/{agent_id}/{file_name}` and already has an internal route style under `/api/internal/...`. However, there is no dedicated contract for:

- reading plain text assets from `SWE_WORKING_DIR/asset`
- writing service-generated UTF-8 text into a tenant scope's `workspaces/default/static`
- returning a stable public URL for the published content

The new write path must be scope-aware, because tenant/source isolation is represented by `scope_id`, not by raw `user_id`.

## Goals / Non-Goals

**Goals:**

- Provide an internal read API for text assets under `SWE_WORKING_DIR/asset`.
- Provide an internal write API that generates file names and publishes UTF-8 text to a tenant scope's public static directory.
- Provide matching external read/write APIs with the same behavior and no internal header requirement.
- Return permanent public URLs that match the existing static serving route shape.
- Keep read/write content limited to UTF-8 text.

**Non-Goals:**

- No binary upload/download support.
- No user-facing UI.
- No file listing, delete, rename, or cleanup lifecycle.
- No short-lived signed URLs.

## Decisions

### Decision 1: Keep the internal router boundary and add a separate external router

The internal endpoints will continue to live behind the existing internal API boundary and should follow the same internal authorization pattern as other `/api/internal/...` routes, including `X-Internal-Token` when internal auth is enabled. A separate public router will expose `/api/assets/text/read` and `/api/assets/text/write` without requiring that header.

Reasoning:
- The internal router already has a clear convention for service-to-service operations.
- The user now explicitly requires an additional external route that does not depend on internal headers.
- Keeping two route layers allows the core file logic to be shared without weakening the internal route contract.

### Decision 2: Read path is a single filename under `SWE_WORKING_DIR/asset`

Both read endpoints will accept a single `file_name`, validate it as a safe file name, and resolve it under `SWE_WORKING_DIR/asset`.

Behavior:
- missing file -> `404`
- invalid file name -> `400`
- invalid UTF-8 -> `400`

### Decision 3: Write path is scope-aware and system-generated

Both write endpoints will accept `user_id`, `source_id`, and `content`.

Processing:
1. Validate inputs and UTF-8 text content.
2. Resolve `scope_id` from `user_id` and `source_id`.
3. Generate a file name server-side.
4. Write to `<WORKING_DIR>/<scope_id>/workspaces/default/static/<generated_name>.html`.
5. Return a permanent public URL using the existing static route shape.

The response URL will follow:

```text
{FILE_URL}/static/{scope_id}/default/{generated_name}.html
```

If `FILE_URL` is not configured, the endpoints will fall back to the same default base used by `copy_file_to_static`.

### Decision 4: File name generation uses `user_id + timestamp` with an `.html` extension

Use a server-generated name derived from `user_id` plus a timestamp, with a `.html` extension.

Reasoning:
- The user explicitly wants the file name to reflect the logical user.
- The `.html` extension matches the intended published artifact type.
- The timestamp keeps names operationally readable.

To avoid collisions for the same `user_id`, the timestamp should use at least millisecond precision.

### Decision 5: Public URLs are permanent

Published files are treated as durable public assets and are not subject to automatic expiry in this change.

Reasoning:
- The user explicitly requires long-lived public URLs.
- Cleanup policy would be a separate lifecycle concern.

## Risks / Trade-offs

- Public static publishing makes content externally reachable by URL. This is intended, and the new external write route additionally allows anonymous callers to publish content for any valid `user_id/source_id` combination.
- Permanent URLs imply no built-in cleanup. That is acceptable for this change, but storage growth may need a later lifecycle policy.
- Scope resolution must be consistent with the rest of the runtime; otherwise the published file and returned URL will diverge.
