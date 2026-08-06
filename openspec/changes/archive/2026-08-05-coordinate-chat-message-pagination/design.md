## Context

The chat detail loader currently returns every message still present in the active session snapshot. `ChatAnywhereSessionsContext` marks that snapshot as history, and `MessageList` renders all cards because BubbleList local pagination is explicitly disabled. Older compacted messages are loaded independently through `GET /chats/{chat_id}/history`, using a cursor, deduplication sets, generation guards, and visible-message anchoring.

The former BubbleList pagination cannot simply be re-enabled: it owns a second page counter, introduces a fixed one-second delay, and cannot tell when local cards are exhausted so archive loading may proceed. The normal and content-only chat surfaces share `MessageList`, so the sequencing and presentation must remain common.

## Goals / Non-Goals

**Goals:**

- Bound the initial DOM cost of a restored chat to the latest 10 historical message cards plus any live cards.
- Reveal locally available historical cards in batches of 10 before making an archive request.
- Fetch archive pages with 20 raw messages and render each returned page in full.
- Keep one upward-scroll trigger, one archive cursor, and one scroll-anchor mechanism.
- Preserve live-message visibility, compaction refresh safety, retry behavior, and per-chat state isolation.
- Provide restrained, accessible, token-based loading, error/retry, and terminal feedback in normal and content-only chat.

**Non-Goals:**

- Changing the chat-detail, archive, streaming, reconnect, or mutation API contracts.
- Paginating the initial chat-detail query at the backend.
- Adding numbered pagination, a manual “load more” control, or a fixed artificial delay.
- Reworking the chat-list pagination or unrelated Conversation Workspace visuals.
- Introducing message virtualization in this change.

## Decisions

### MessageList owns progressive disclosure

`MessageList` will own an explicit local visibility limit rather than re-enabling BubbleList pagination. On a new backend chat identity, the limit resets to 10. Before archive paging starts, the rendered list contains all non-history/live cards plus the latest historical cards up to that limit. Each eligible upward-scroll trigger raises the limit by 10.

This keeps the existing BubbleList as a presentation and scrolling primitive and prevents its legacy page counter, sentinel spinner, one-second timeout, and height-delta correction from competing with cursor history.

Alternative considered: restore `pagination={true}`. Rejected because BubbleList cannot sequence local exhaustion before archive requests and would create two independent loading paths.

### Archive paging begins only after local exhaustion

The same preload callback will choose one action per trigger:

1. If current-memory historical cards remain hidden, reveal the next 10 synchronously.
2. Otherwise, if archive history is available or not yet proven exhausted, request the current archive cursor.
3. If archive history is exhausted, perform no request.

The existing preload-zone latch remains responsible for requiring the user to leave and re-enter the 240px region before another action. The local reveal path does not enter a loading state and does not add a delay.

### Archive pages are server-bounded, not locally repaginated

Archive requests will call `getChatHistory(chatId, cursor, 20)`. Every unseen card produced from that response is inserted and rendered immediately. The frontend will not maintain a second visibility count for cards within an archive page.

This limits each archive response while keeping cursor progression, `has_more`, deduplication, and retry semantics straightforward. The count is expressed in raw backend messages; card conversion may produce fewer than 20 cards because assistant and tool messages can be grouped.

Alternative considered: retain the server limit of 50 and reveal archive cards 10 at a time. Rejected because it requires coordinating a server cursor with a second hidden-card cursor and complicates compaction invalidation and scroll anchoring.

### Local reveal and archive insertion share message anchoring

Before either action adds visible cards above the reader, `MessageList` captures the first visible message element and its container-relative offset. After React commits, the existing layout-effect anchor restoration runs immediately and again on the next animation frame.

The local visibility limit and archive phase reset only when the resolved backend chat identity changes. Live cards remain outside the historical visibility cap and therefore appear immediately. Stale requests continue to be rejected through the existing generation/session guards.

### Terminal and retry states follow server truth

When an archive response returns `has_more=false`, the UI immediately enters the terminal state after rendering that response; it does not issue a later empty request for confirmation. A failed request preserves all visible cards and the current cursor. Retry uses that same cursor and is locked while the request is active.

### History status uses a dedicated token-based presentation

The top-of-history feedback will become a compact inline status component rather than inline styles and a raw button:

- Loading: small spinner and “正在加载更早的消息…”, shown only for an actual archive request.
- Error: subtle semantic error background, an error icon, “历史消息加载失败”, and a low-emphasis retry action.
- Exhausted: quiet neutral “已到达会话开始处” treatment.

The component will use existing theme tokens for semantic colors, radius, focus, and spacing; keep a stable height across states; expose `status`/`alert` semantics and polite live announcements; and provide hover, active, focus-visible, loading, and disabled behavior for retry. It will not introduce a large card or decorative treatment that competes with conversation content.

## Risks / Trade-offs

- [Current and archived cards both use the `history` marker] → Keep an explicit pre-archive phase; once local current cards are exhausted and archive paging starts, render archive pages in full rather than trying to infer origins indefinitely from that marker.
- [A compaction refresh can replace the current snapshot while the user is reading] → Preserve the existing compaction generation guards and visible anchor, normalize refreshed snapshot cards as historical, and avoid resetting pagination unless chat identity changes.
- [Reverse-order flex scrolling is sensitive to layout changes] → Reuse message-element anchoring for both local reveal and archive insertion and retain disabled browser scroll anchoring.
- [Smaller archive pages increase request count for very long histories] → Requests occur only on explicit upward-scroll re-entry; the lower per-page render cost and simpler state machine are preferred over the current 50-message page.
- [Ten cards do not equal ten backend messages or turns] → Specify batching in rendered historical cards for current-memory data and in raw messages for archive API limits; test grouped assistant/tool output.
- [Shared component changes can regress content-only mode] → Run the same behavior and accessibility tests with the content-only provider and verify normal mode through the shared component path.

## Migration Plan

1. Add focused tests for the progressive local-to-archive state machine and status presentation.
2. Implement the local visibility phase in `MessageList` while leaving BubbleList pagination disabled.
3. Set the archive request limit to 20 and update terminal semantics.
4. Extract and style the compact history status component using existing tokens.
5. Run focused tests, frontend type/build checks, and browser verification of normal and content-only chat.

Rollback is a focused frontend revert: restore full `safeMessages` rendering and the prior inline status while retaining the unchanged backend archive API.

## Open Questions

None. Batch sizes, triggering, feedback, terminal behavior, archive page size, and surface scope were confirmed during requirement discussion.
