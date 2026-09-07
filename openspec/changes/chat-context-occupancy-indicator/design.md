## Context

The Console Chat composer currently has an action row with quick-menu and submit controls. The standard bottom composer is implemented through `AgentScopeRuntimeWebUI/core/Chat/Input` and `ChatInput`, while the new-chat welcome composer uses a custom welcome layout.

Swe already tracks cumulative token usage for billing/analytics, but that data is not the same as context-window occupancy. Runtime context limits and compaction are driven by the Agent running configuration, especially `running.max_input_length`, `context_compact`, and tool-result compaction settings. The effective next Main Agent model input also depends on memory state, completed compressed summary, system prompt, and compacted tool results, so frontend-visible messages are insufficient for an accurate estimate.

## Goals / Non-Goals

**Goals:**

- Provide a backend-computed persisted context occupancy estimate captured from the scoped session's real Main Agent runtime.
- Use `running.max_input_length` as the denominator.
- Estimate effective persisted context after completed compaction, excluding unsent composer draft text.
- Persist categorized estimates atomically with the cleaned session state.
- Render a quiet context percentage control in the Console Chat composer action row.
- Let users click or focus the control to inspect system context, active tool definitions, online conversation messages, and remaining capacity.
- Refresh only on stable chat events: page entry, session switch, history reload, model/running-config changes, and stream completion.
- Keep the indicator non-disruptive during loading and generation.

**Non-Goals:**

- No continuous polling.
- No inclusion of unsent composer draft text.
- No submission blocking based on occupancy status.
- No change to compaction trigger behavior.
- No use of provider-reported model context-window metadata in the first version.
- No attempt to show historical billing/token usage in the composer.

## Decisions

### Decision: Add an ownership-gated Chat context usage endpoint

Add `GET /chats/{chat_id}/context-usage` beside the existing Chat detail API.

Rationale:

- The Chat record already binds tenant/source/agent/user ownership to the persisted session.
- Existing Chat authorization intentionally returns 404 for cross-owner reads.
- Mixing this into chat history would force the frontend to reload messages just to refresh an auxiliary indicator.

Alternative considered: compute in the frontend from chat messages. Rejected because frontend messages do not include system prompt, compressed summary, fixed runtime context, or compaction state accurately enough.

### Decision: Sample the real runtime instead of rebuilding it during GET

After the Main Agent has loaded history and assembled its prompt, skills, MCP tools, and dynamic tool groups, the backend counts the final cleaned runtime context before committing the ordinary session state. The GET endpoint only returns the latest committed numeric snapshot.

Rationale:

- This counts the same system context, active tool schemas, and online messages the next model call would receive.
- GET remains read-only and never invokes hooks, MCP discovery, model construction, or compaction.

Alternative considered: use tracing `total_input_tokens` from the previous model call. Rejected because it measures completed calls, not the current persisted state that will feed the next turn.

### Decision: Use Agent `running.max_input_length` as capacity

The denominator is the active Agent running configuration `max_input_length`, not model metadata.

Rationale:

- Runtime compaction and fit checks are configured against `max_input_length`.
- Current provider model metadata does not expose a reliable context-window field.
- This matches the existing Agent configuration UI label for maximum context length.

Alternative considered: infer model context windows from provider/model ids. Rejected as brittle and likely wrong for custom providers.

### Decision: Persist the snapshot with session state

The final snapshot is written to a server-owned top-level session-state key in the same atomic mutation as the cleaned Agent state. It contains numeric counts, configured thresholds, sampling time, and schema version, but no prompt text, messages, or tool schemas.

Rationale:

- Session persistence is the deterministic cache and version boundary.
- The snapshot cannot drift from the Agent state committed in the same transaction.
- A running turn may return the previous committed snapshot with `stale=true` rather than blocking on the active session lock.

Alternative considered: an in-process estimate cache computed by GET. Rejected because it would still require reconstructing request-specific runtime state and would not be authoritative across multiple instances.

### Decision: Return categorized raw values and runtime-aligned status

The endpoint returns availability, used/capacity/remaining values, ratio, estimation/staleness flags, configured context thresholds, and three categories: system context, active tool definitions, and online conversation messages. Status follows the configured governance, active-compaction, emergency, and overflow boundaries.

Rationale:

- Returning status and thresholds keeps frontend clients aligned with the active Agent configuration.
- Returning category counts makes the estimate inspectable without exposing sensitive prompt or message content.
- The threshold is visual only and must not affect submission.

Alternative considered: frontend-only status calculation. Acceptable, but backend status makes API tests clearer and prevents multiple clients from drifting.

### Decision: Render through the existing composer prefix extension

For the standard bottom composer, inject the control through the existing `sender.prefix` action-row area. The new-chat welcome composer reuses its existing `prefixItems` extension and shows an unavailable state until a real Chat snapshot exists.

For the welcome composer, add a narrow prop or equivalent action-row extension so the same indicator can be placed next to its submit button.

Rationale:

- The indicator is semantically tied to submit readiness.
- It should not become a global header badge or sidebar metric.
- A narrow extension avoids redesigning the composer.

Alternative considered: place the indicator in the chat header. Rejected because the user asked for placement beside the submit button and because the metric is submit-context related.

### Decision: Keep refreshes quiet and details explicit

The frontend keeps the last rendered value during refresh, shows no global toast, and uses an unavailable placeholder when no snapshot exists. The trigger always exposes a compact percentage state; click or keyboard focus opens a popover with a progress bar, approximate totals, remaining capacity, three categories, and a non-color-only status label.

Rationale:

- The indicator is auxiliary and should not distract from sending.
- Session state is most reliable after stream completion.
- The user explicitly prefers no loading affordance.

Alternative considered: show a spinner during refresh. Rejected as visually noisy for a small composer control.

## Risks / Trade-offs

- [Risk] Token estimate may differ from provider-side accounting. → Mitigation: mark tooltip copy as approximate and use the configured runtime token counter.
- [Risk] Counting effective context may accidentally mutate memory if it reuses compaction helpers directly. → Mitigation: implement estimation as read-only inspection or clone state before applying display-time compaction views.
- [Risk] Snapshot capture fails during cleanup. → Mitigation: never block session persistence; preserve the previous committed snapshot and mark it stale until a later successful turn.
- [Risk] The exact "immediately left of submit" slot may require a small composer API change. → Mitigation: prefer the narrowest action-row extension and add component tests to lock placement.
- [Risk] Estimation could be expensive for very large sessions. → Mitigation: sample once during the existing save path and avoid refresh on typing or polling.

## Migration Plan

1. Add runtime snapshot model, categorized estimator, and atomic persistence.
2. Add ownership-safe read-only Chat endpoint and stale-state classification.
4. Add frontend API client/types.
5. Add compact percentage control with click/focus detail popover and unavailable state.
6. Wire indicator into standard and welcome Console Chat composers immediately left of submit.
7. Trigger refresh on page entry, session switch, history reload, model/running-config changes, and stream completion.
8. Add backend and frontend tests.
9. Rollback by hiding the frontend indicator and disabling the endpoint route; no persisted migration is required.

## Open Questions

None. The grilling session resolved denominator, categorized numerator scope, real-runtime sampling, atomic snapshot persistence, refresh timing, loading behavior, and popover behavior.
