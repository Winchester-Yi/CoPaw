# chat-message-history-loading Specification

## Purpose

Define progressive rendering and archive loading for a single chat detail so current-memory messages appear in bounded batches, compacted history follows only after local exhaustion, reading position remains stable, and normal and content-only workspaces share accessible loading, retry, and terminal feedback.

## Requirements
### Requirement: Current chat messages SHALL render progressively

When a persisted chat detail is restored, the Conversation Workspace SHALL initially render the latest 10 historical message cards. It SHALL reveal at most 10 additional locally available historical cards each time upward scrolling leaves and re-enters the history preload region. Live or newly streamed message cards SHALL remain immediately visible and SHALL NOT count against the historical visibility limit.

#### Scenario: Long current snapshot opens with a bounded card count
- **WHEN** a restored chat contains more than 10 current-memory historical message cards
- **THEN** the detail initially renders its latest 10 historical cards
- **AND** older current-memory cards remain available in frontend state without being mounted in the message list

#### Scenario: Short current snapshot renders completely
- **WHEN** a restored chat contains 10 or fewer current-memory historical message cards
- **THEN** all of those cards are rendered immediately

#### Scenario: Upward scrolling reveals one local batch
- **WHEN** current-memory historical cards remain hidden
- **AND** the user leaves and re-enters the history preload region by scrolling upward
- **THEN** the next 10 locally available historical cards are rendered without a network request
- **AND** the reveal has no fixed artificial delay or loading spinner

#### Scenario: Live output remains visible
- **WHEN** a new user, assistant, tool, or progress card arrives while older historical cards remain hidden
- **THEN** the new card is rendered immediately
- **AND** the historical visibility limit remains unchanged

### Requirement: Archive requests SHALL follow local exhaustion

The Conversation Workspace SHALL request compacted archive history only after every current-memory historical card is visible. Each archive request SHALL use the current cursor with `limit=20`, and every unseen card converted from the returned archive page SHALL be rendered as one complete page without an additional frontend visibility cursor.

#### Scenario: Hidden local cards prevent an archive request
- **WHEN** the user enters the history preload region
- **AND** current-memory historical cards remain hidden
- **THEN** the Console reveals a local batch
- **AND** it does not request `/chats/{chat_id}/history`

#### Scenario: Local exhaustion starts archive paging
- **WHEN** every current-memory historical card is visible
- **AND** the user later re-enters the history preload region
- **THEN** the Console requests `/chats/{chat_id}/history` with the current cursor and `limit=20`

#### Scenario: Archive page renders in full
- **WHEN** an archive request returns unseen raw messages and optional compaction boundaries
- **THEN** all cards converted from that response are inserted and rendered together
- **AND** the next cursor is retained for a later upward-scroll trigger

#### Scenario: Archive page does not duplicate existing cards
- **WHEN** an archive response contains a message or boundary already present in the chat detail
- **THEN** the Console excludes the duplicate before inserting the page

### Requirement: History progression SHALL preserve reading position and chat isolation

Revealing local cards or inserting an archive page SHALL preserve the reader's visible message anchor. Pagination, cursor, loading, retry, and terminal state SHALL be scoped to the resolved backend chat identity, and stale results from another chat or invalidated compaction generation SHALL NOT alter the active chat.

#### Scenario: Local reveal preserves the visible message
- **WHEN** a local batch is rendered above the current viewport
- **THEN** the previously visible anchor message retains its container-relative position after layout settles

#### Scenario: Archive insertion preserves the visible message
- **WHEN** an archive page is inserted above the current viewport
- **THEN** the previously visible anchor message retains its container-relative position after layout settles

#### Scenario: Switching chats resets progressive state
- **WHEN** the resolved backend chat identity changes
- **THEN** the new chat starts with its own 10-card historical visibility limit and initial archive cursor
- **AND** pagination state from the previous chat is not reused

#### Scenario: Stale archive result is ignored
- **WHEN** an archive request resolves after the active chat or compaction generation has changed
- **THEN** its messages, cursor, and status do not modify the active chat

### Requirement: Archive feedback SHALL be truthful, recoverable, and accessible

The Conversation Workspace SHALL show loading feedback only while an archive network request is active. A failed request SHALL preserve existing messages and cursor and SHALL expose an accessible retry action. When a response reports `has_more=false`, the workspace SHALL render that response and immediately show the terminal conversation-start state without issuing a later empty confirmation request.

#### Scenario: Archive request displays loading feedback
- **WHEN** the Console is waiting for an archive response
- **THEN** a compact inline status announces “正在加载更早的消息…”
- **AND** repeated archive requests are locked until the active request finishes

#### Scenario: Local reveal omits loading feedback
- **WHEN** the Console reveals a locally available batch
- **THEN** no loading spinner or artificial loading state is shown

#### Scenario: Archive failure offers retry
- **WHEN** an archive request fails
- **THEN** already visible messages remain unchanged
- **AND** a compact alert announces “历史消息加载失败” and exposes a keyboard-accessible retry action
- **AND** retry requests the same cursor and is disabled while that retry is active

#### Scenario: Final archive page reaches the conversation start
- **WHEN** an archive response reports `has_more=false`
- **THEN** all unseen cards from that response are rendered
- **AND** the status announces “已到达会话开始处”
- **AND** no additional archive request is issued for that chat generation

### Requirement: History status presentation SHALL follow the Conversation Workspace design system

Loading, error, retry, and terminal history feedback SHALL use a compact inline treatment based on existing semantic theme tokens. It SHALL keep a stable footprint across asynchronous states, SHALL NOT use a large card or decorative styling, and SHALL provide non-color-only status meaning, WCAG-compatible text contrast, visible keyboard focus, and reduced-motion-safe transitions.

#### Scenario: Error treatment remains subordinate to conversation content
- **WHEN** archive loading fails
- **THEN** the error state uses a subtle semantic error surface, an error icon, concise text, and a low-emphasis retry action
- **AND** it does not obscure or displace the loaded conversation beyond its stable status footprint

#### Scenario: Retry exposes complete interaction states
- **WHEN** the retry action is rendered
- **THEN** it has default, hover, active, focus-visible, loading, and disabled states
- **AND** its accessible name identifies the history retry action

### Requirement: Normal and content-only chat details SHALL share history behavior

The normal Conversation Workspace and `showContentOnly=true` chat detail SHALL use the same progressive rendering, archive sequencing, scroll anchoring, loading, error, retry, and terminal behavior without changing content-only permissions or omitted surfaces.

#### Scenario: Content-only chat progressively renders history
- **WHEN** a content-only chat restores a long persisted conversation
- **THEN** it follows the same 10-card local batches and 20-raw-message archive pages as normal chat
- **AND** it does not add composer, upload, navigation, or mutation capabilities

#### Scenario: Normal chat compatibility
- **WHEN** a normal chat uses progressive history loading
- **THEN** streaming, reconnect, cancellation, message actions, composer behavior, and chat identity retain their existing semantics
