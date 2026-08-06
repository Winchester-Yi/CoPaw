## Why

Opening a long chat currently renders every current-memory message card at once, while older compacted history uses a separate cursor API. This creates avoidable rendering cost, and restoring the former BubbleList pagination directly would reintroduce competing local and remote pagination paths with unstable loading and scroll behavior.

## What Changes

- Progressively render current-memory chat history: show the latest 10 message cards initially and reveal 10 more whenever upward scrolling re-enters the existing preload region.
- Request compacted history only after every current-memory card is visible.
- Request archived history with `limit=20`, render each returned cursor page in full, and request the next page only on a later upward-scroll trigger.
- Preserve the reader's visible-message anchor when local cards are revealed or an archive page is inserted.
- Keep live messages immediately visible and isolate pagination state between chats.
- Replace the current inline history loading/error copy with a compact, token-based Conversation Workspace status treatment, including an accessible retry action and a terminal conversation-start state.
- Apply the same behavior to normal chat and `showContentOnly=true` chat detail surfaces.

## Capabilities

### New Capabilities

- `chat-message-history-loading`: Defines progressive current-message rendering, cursor archive sequencing, scroll stability, status feedback, retry behavior, and normal/content-only consistency for a single chat detail.

### Modified Capabilities

None.

## Impact

- Frontend chat message orchestration in `MessageList`, history preloading, message-list presentation, and related tests.
- The existing `GET /chats/{chat_id}/history` contract remains unchanged; the Console changes its requested limit from the default 50 to 20.
- No changes to chat identity, routes, streaming, reconnect, mutations, permissions, backend storage, or chat-list pagination.
- Existing BubbleList pagination remains available to other consumers but is not re-enabled as a competing pagination owner for chat detail.
