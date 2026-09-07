## ADDED Requirements

### Requirement: Runtime SHALL capture categorized persisted context occupancy
The system SHALL capture a numeric **Persisted Context Occupancy** snapshot from the real Main Agent runtime before an ordinary Chat session state is committed. The snapshot SHALL use the active Agent `running.max_input_length` as capacity and SHALL be an estimate produced by the configured runtime token counter.

#### Scenario: Real runtime context is sampled
- **WHEN** the Main Agent has assembled its current system prompt, skill context, MCP tools, dynamic tool groups, and effective memory
- **THEN** the snapshot SHALL count the assembled runtime rather than reconstructing an Agent in a later GET request
- **AND** sampling SHALL NOT call the model

#### Scenario: Clean persisted state is sampled
- **WHEN** internal continuation messages or duplicate external approval messages will be removed before persistence
- **THEN** the snapshot SHALL count the cleaned memory state
- **AND** the snapshot and cleaned Agent state SHALL be committed atomically

### Requirement: Occupancy SHALL expose three non-overlapping categories
The snapshot SHALL split estimated used tokens into System Context, Active Tool Definitions, and Online Conversation Messages. The sum of the three category counts SHALL equal `used_tokens`.

#### Scenario: System context is counted
- **WHEN** the Agent has a system prompt, active skill prompt, completed compressed summary, long-term-memory wrapper, or runtime injection
- **THEN** those fixed and prefixed inputs SHALL be counted in `system_context_tokens`

#### Scenario: Active tool definitions are counted
- **WHEN** the Agent toolkit contains active built-in, source, skill, MCP, or dynamically activated tool schemas
- **THEN** the schemas actually exposed to the model SHALL be counted in `tool_definition_tokens`
- **AND** inactive tool groups SHALL NOT be counted

#### Scenario: Online conversation is counted
- **WHEN** effective uncompressed memory contains user, assistant, structured tool-use, or tool-result content
- **THEN** that content SHALL be counted in `conversation_tokens`
- **AND** already-archived raw history and unsent composer text SHALL NOT be counted

### Requirement: Snapshot SHALL preserve confidentiality and session durability
The persisted snapshot SHALL contain only schema version, numeric counts, ratios, configured thresholds, status, and sampling metadata. It SHALL NOT persist prompt text, message content, tool schemas, secrets, or cumulative billing records.

#### Scenario: Sampling fails
- **WHEN** the configured counter or runtime inspection fails during cleanup
- **THEN** ordinary session persistence SHALL continue
- **AND** an existing committed snapshot SHALL be preserved rather than overwritten with misleading zeroes

### Requirement: Chat API SHALL expose an ownership-gated snapshot
The system SHALL expose `GET /chats/{chat_id}/context-usage`. The endpoint SHALL reuse existing Chat user/source/agent authorization and the non-blocking persisted-session reader.

#### Scenario: Authorized Chat has a snapshot
- **WHEN** the current owner requests an existing Chat with a committed snapshot
- **THEN** the response SHALL include `available`, `used_tokens`, `max_tokens`, `remaining_tokens`, `usage_ratio`, `system_context_tokens`, `tool_definition_tokens`, `conversation_tokens`, configured threshold ratios, `status`, `estimated`, `stale`, and `as_of`

#### Scenario: Chat has no snapshot
- **WHEN** a new or legacy Chat has no committed context snapshot
- **THEN** the endpoint SHALL return `available: false`
- **AND** it SHALL NOT rebuild an Agent, connect MCP, run Hooks, or fabricate zero occupancy

#### Scenario: Chat is running
- **WHEN** a turn is running or stopping while a previous snapshot exists
- **THEN** the endpoint SHALL return the previous committed snapshot with `stale: true`
- **AND** it SHALL NOT wait for the active session transaction

#### Scenario: Chat is not owned by the request
- **WHEN** user, source, or Agent ownership does not match
- **THEN** the endpoint SHALL return the same 404 behavior as Chat detail

### Requirement: Status SHALL follow the configured context budget
The snapshot status SHALL be `normal`, `governance`, `active`, `emergency`, or `overflow` according to the current Agent context-compaction threshold ratios and raw occupancy ratio. Status SHALL be presentation-only and SHALL NOT block submission or alter compaction behavior.

#### Scenario: Capacity is exceeded
- **WHEN** `used_tokens` is greater than or equal to `max_tokens`
- **THEN** status SHALL be `overflow`
- **AND** `remaining_tokens` SHALL be zero

### Requirement: Console composer SHALL show a compact occupancy control
The Console Chat composer action row SHALL show a stable, keyboard-accessible control displaying `上下文` and the current approximate percentage. When no snapshot is available it SHALL display `上下文 --` without shifting the composer layout.

#### Scenario: Snapshot is available
- **WHEN** the active Chat has a context snapshot
- **THEN** the control SHALL display the rounded percentage and a status treatment
- **AND** warning or emergency states SHALL include text or an accessible label rather than relying only on color

#### Scenario: Snapshot is unavailable
- **WHEN** the active Chat has no snapshot or the request fails
- **THEN** the control SHALL remain usable and expose unavailable/retry guidance

### Requirement: Composer control SHALL expose categorized details
Clicking, pressing Enter/Space, or focusing and activating the control SHALL open an accessible popover. The popover SHALL show approximate used/capacity values, remaining capacity, a progress bar, status explanation, and the three categories with compact and full numeric values.

#### Scenario: User opens details
- **WHEN** the user activates the context control
- **THEN** the popover SHALL show the three categories and their counts
- **AND** it SHALL state that values are estimates rather than provider billing totals
- **AND** the trigger SHALL expose `aria-haspopup` and current expanded state

#### Scenario: Narrow embedded workspace
- **WHEN** horizontal space is constrained or `hideMenu=true`
- **THEN** the trigger and popover SHALL remain usable without covering the submit action or overflowing the viewport

### Requirement: Frontend SHALL refresh only on stable context events
The frontend SHALL request the snapshot on active Chat change, after generation transitions from active to idle, after matching conversation compaction, and after model/Agent context configuration changes. It SHALL NOT poll continuously or refresh from draft typing or every stream fragment.

#### Scenario: Session switch races an older response
- **WHEN** a previous Chat request finishes after the user switches Chats
- **THEN** the stale response SHALL NOT replace the active Chat indicator

#### Scenario: Refresh is in flight
- **WHEN** a previously rendered value exists
- **THEN** the previous value SHALL remain visible without a spinner or global toast
