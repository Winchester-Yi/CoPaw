# Permanent share links use immutable conversation snapshots

Status: accepted
Date: 2026-09-02

## Context

The Console Chat needs a share action that lets a user select conversation history and give other people a link to view it. A link must remain usable across requests and Kubernetes Pods, while the source Chat can continue to change. The shared representation therefore cannot be a live read of the source session file.

The selection unit is an **Answer Turn**: one user question and all persisted output belonging to that answer. Users may select any non-contiguous set of completed turns from the current Chat. A running, stopping, or failed turn is not shareable, and an empty selection must not create a link.

The selected turn is deliberately lossless with respect to Chat history: tool calls, system messages, approval records, and other persisted message content remain in the snapshot. The ordinary Chat history transformation is still authoritative for hidden context: apply `_redact_hidden_context_messages` exactly as the normal Chat history path does. No additional share-specific filtering or size limit is introduced.

## Decision

### Link and lifecycle

- `POST /api/chats/{chat_id}/share` accepts one or more selected turn IDs.
- Authenticated `GET /api/chats/{chat_id}/share-options` returns the display
  history and authoritative status for each selectable turn.
- The server validates Chat ownership and that every selected turn is completed, then creates a fresh high-entropy opaque **Share Token**.
- Every generation creates a new independent token, even when its selection is identical to an earlier link.
- `GET /api/chat-shares/{token}` is anonymous and read-only; the public route
  bypasses authentication and tenant/source identity requirements so a viewer
  needs no owner or tenant headers.
- Links are permanent and cannot be revoked. A generated link has no expiry timestamp or owner-side disable operation.
- The token is the sole public locator; it must not encode or reveal Chat ID, session ID, tenant, channel, or user identity.

### Snapshot contents and rendering

Creation freezes a **Shared Conversation Snapshot** containing exactly the selected turns in their original Chat order. The snapshot is independent of later Chat rename, extension, mutation, or deletion.

The public route renders the snapshot with the Chat renderer's structured display in strict read-only mode. It may show the Chat title and message timestamps, but must disable continuing the Chat, submissions, approvals, feedback, file mutations, and every other state-changing action. Public envelope data must not disclose owner, tenant, channel, Chat ID, session ID, or other internal identity fields. Within the selected turns, persisted tool/system/approval content is retained subject only to the established hidden-context redaction above.

### Persistence

MySQL stores only the share index and small audit metadata:

- token (primary key)
- source Chat ID and creator ID (for authorization/audit, never public)
- tenant/storage scope (for cross-Pod path resolution, never public)
- snapshot object key
- creation time
- access count
- most recent access time

The potentially large snapshot body is a separate JSON file on the existing shared RWX volume. It is not written to MySQL, and it is not a reference to or reuse of the source session file. The object key is derived from the opaque token and is resolved under the configured snapshot root. Reads require the exact `{tenant}/chat_shares/{token}.json` key, preventing a corrupted index row from crossing tenant directories.

Snapshot creation writes a temporary file in that directory, flushes it, and atomically replaces the final file. This makes a completed snapshot visible as one immutable object to every Pod. The database index is created only after the snapshot write succeeds; if index creation fails, the orphan file is removed when possible.

Both creation and access fail closed when the required MySQL connection or shared snapshot storage is unavailable. There is no local-disk fallback. Invalid, tampered, missing, or otherwise unresolvable tokens return the same generic `404 Not Found`; infrastructure/storage failures return generic `503 Service Unavailable`.

Accesses increment the link's count and update its most recent access time. Anonymous viewer identity and snapshot body copies are not stored in audit metadata.

## Consequences

This design provides stable cross-Pod links and guarantees that a shared page cannot change when its source Chat changes. It keeps large content out of MySQL and prevents token formats from becoming an identity oracle. The trade-offs are permanent storage growth, dependence on the shared RWX volume, and the intentional inability to withdraw a link after it has been created.

## Rejected alternatives

- **Live projection of the source Chat:** later edits or deletion would change or destroy previously shared content.
- **Reusing the source session file:** couples public access to private session layout and risks exposing unselected or newly added turns.
- **Storing snapshot bodies in MySQL:** unsuitable for potentially long conversation content and increases database load.
- **Expiring or revocable links:** explicitly outside the product contract; the chosen lifecycle is permanent and non-revocable.
