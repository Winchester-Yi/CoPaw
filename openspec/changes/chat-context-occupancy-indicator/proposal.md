## Why

Users currently cannot see how much of the Main Agent context window is already occupied before continuing a Console Chat session. This makes long-running sessions opaque until compaction or context-limit behavior surprises the user.

## What Changes

- Capture a categorized context occupancy snapshot from the real Main Agent runtime and expose it through an ownership-gated read-only Chat API.
- Estimate the context that would actually enter the next Main Agent model input after completed compaction, split into system context, active tool definitions, and online conversation messages.
- Exclude unsent composer draft text and cumulative billing/token usage from the indicator.
- Persist the snapshot atomically with the cleaned Agent session state so reads never rebuild an Agent or trigger Hook/MCP side effects.
- Add a compact context percentage control in the Console Chat composer action row.
- Open an accessible click/focus popover with approximate used/capacity values, progress, remaining capacity, and the three categorized counts.
- Keep the indicator quiet during refresh and generation: preserve the previous value without spinner or updating text, and refresh once generation completes.
- Show a stable `上下文 --` unavailable control with retry guidance when no committed snapshot exists or loading fails.

## Capabilities

### New Capabilities

- `chat-context-occupancy`: Estimates and displays persisted Main Agent context-window occupancy for Console Chat sessions.

### Modified Capabilities

None.

## Impact

- Chat routing gains `GET /chats/{chat_id}/context-usage`, reusing the existing user/source/agent ownership checks and non-blocking persisted-session read path.
- Runtime sampling uses the exact assembled Main Agent system context, active tool schemas, effective memory, and configured token counter; only numeric snapshots are persisted.
- Console Chat frontend adds API typing/client code and renders the indicator through the standard composer action area.
- Console Chat refresh wiring updates occupancy on page entry, session switch, history reload, model/running-config changes, and stream completion.
- Tests cover categorized runtime sampling, atomic snapshot persistence, ownership-safe reads, stale-state signaling, and frontend popover/refresh behavior.
