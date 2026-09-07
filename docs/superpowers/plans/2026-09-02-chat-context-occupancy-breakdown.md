---
title: Chat Context Occupancy Breakdown
status: active
date: 2026-09-02
origin: openspec/changes/chat-context-occupancy-indicator
---

# Chat Context Occupancy Breakdown Implementation Plan

## Goal

Expose a complete categorized estimate of the persisted Main Agent context in the Console Chat composer. The control displays the current percentage and opens a click/focus popover for System Context, Active Tool Definitions, Online Conversation Messages, remaining capacity, and runtime-aligned warning state.

## Scope And Boundaries

- Use `running.max_input_length` as capacity.
- Count the real assembled Main Agent runtime once per ordinary Chat save; do not rebuild an Agent in GET.
- Persist numeric metadata only, atomically with the cleaned session state.
- Exclude unsent draft text, archived raw history, cumulative billing records, and inactive tool groups.
- Return the last committed snapshot as stale while a turn is running or stopping.
- Do not modify compaction behavior, generic Sender behavior, Cron skip-history persistence, or provider model metadata.
- Preserve all unrelated dirty diagram/tool-group work. Apply only surgical patches to overlapping `console/src/pages/Chat/index.tsx` and related tests.

## Decision Trace

- `CONTEXT.md` defines the product term as **Persisted Context Occupancy**.
- `openspec/changes/chat-context-occupancy-indicator/` is the authoritative requirement set.
- Runtime sampling is required because prompt injections, skill prompts, MCP discovery, source tools, selected modes, and dynamic tool groups cannot be reconstructed safely by a read-only GET.
- `GET /chats/{chat_id}/context-usage` reuses the existing Chat ownership and non-blocking persistence boundaries.

## Implementation Unit 1: Categorized Runtime Snapshot

Files:

- Add `src/swe/app/runner/context_usage.py`.
- Add `tests/unit/app/test_context_usage.py`.

Behavior:

- Define a versioned snapshot contract and `CONTEXT_USAGE_STATE_KEY`.
- Count `agent.sys_prompt` plus prefixed summary/long-term-memory context as System Context.
- Count `agent.toolkit.get_json_schemas()` as Active Tool Definitions.
- Count effective online `Msg` content, including structured tool-use/result blocks, as Online Conversation Messages.
- Sum categories into used/capacity/remaining/ratio and classify against configured governance/active/emergency/overflow thresholds.
- Never persist source text or schema content.

TDD scenarios:

1. Empty online history still counts fixed system context.
2. Summary/long-term prefix is counted once and online messages are not duplicated.
3. Active schemas count; inactive tool-group schemas do not.
4. Structured tool-use/result message payloads count.
5. Category sum equals total; overflow clamps only remaining capacity.
6. Counter failure propagates to the caller without invoking a model.

Verification:

```bash
venv/bin/python -m pytest tests/unit/app/test_context_usage.py -q
```

## Implementation Unit 2: Atomic Persistence And Read API

Files:

- Modify `src/swe/app/runner/runner.py`.
- Modify `src/swe/app/runner/api.py`.
- Modify `src/swe/app/runner/models.py` only if the response model is not kept in `context_usage.py`.
- Extend `tests/unit/app/test_session_state_merge_coordination.py`.
- Extend or add API tests beside `tests/unit/app/test_chat_answer_turn_api.py`.

Behavior:

- Sanitize the detached `agent.state_dict()` before sampling so removed internal continuation/duplicate approval messages do not inflate the snapshot.
- Capture the final snapshot before the existing atomic regular-session mutation.
- On capture failure, continue saving and preserve any older snapshot.
- Add `GET /chats/{chat_id}/context-usage` with `_authorize_chat` and `_read_history_state`.
- Return `available=false` without constructing runtime resources when no snapshot exists.
- Mark the last committed snapshot stale while the answer-turn coordinator reports running/stopping.

TDD scenarios:

1. Snapshot and cleaned Agent state commit in the same mutation.
2. Removed internal messages do not contribute to conversation tokens.
3. Capture failure preserves the old snapshot and session save succeeds.
4. Missing, cross-user, cross-source, and cross-Agent Chat reads return existing 404 semantics.
5. Active Chat reads the persisted snapshot without acquiring the live execution state.
6. GET never constructs an Agent, registers MCP, executes Hooks, or calls a model.

Verification:

```bash
venv/bin/python -m pytest tests/unit/app/test_context_usage.py tests/unit/app/test_session_state_merge_coordination.py tests/unit/app/test_chat_answer_turn_api.py -q
```

## Implementation Unit 3: Console API, State, And Popover

Files:

- Add `console/src/api/types/contextUsage.ts`.
- Modify `console/src/api/modules/chat.ts` and its focused tests.
- Add `console/src/pages/Chat/components/ContextUsageIndicator/index.tsx`.
- Add `console/src/pages/Chat/components/ContextUsageIndicator/index.module.less`.
- Add `console/src/pages/Chat/components/ContextUsageIndicator/index.test.tsx`.
- Surgically modify `console/src/pages/Chat/index.tsx` and `console/src/pages/Chat/index.test.tsx`.

Behavior:

- Resolve the active backend Chat id from `ChatAnywhereSessionsContext` and `sessionApi`.
- Keep the last value during refresh and ignore out-of-date requests after Chat switches.
- Refresh on Chat id change, loading active-to-idle transition, matching `conversation_compacted`, and `model-switched`.
- Do not refresh while typing or on stream fragments.
- Render `上下文 <percent>` through existing `sender.prefix` and welcome `prefixItems`.
- Open an Ant Design Popover with progress, approximate used/capacity/remaining values, three categories, and non-color-only status text.
- Keep the trigger accessible and resilient in narrow/embedded layouts.

TDD scenarios:

1. Available and unavailable trigger states render without layout shift.
2. Popover shows category values and estimate disclosure.
3. Button name, `aria-haspopup`, `aria-expanded`, keyboard activation, and progress semantics work.
4. Chat-switch races cannot overwrite the active value.
5. Generation completion, matching compaction, and model switch refresh once.
6. Draft typing and unrelated compaction events do not refresh.
7. Failed request keeps the prior value or shows retry guidance without global toast.

Verification:

```bash
pnpm vitest run console/src/pages/Chat/components/ContextUsageIndicator/index.test.tsx console/src/pages/Chat/index.test.tsx
pnpm tsc -b --noEmit
```

## Review And Completion Gate

- Run four independent review cycles required by the engineering gauntlet: spec/correctness, quality/maintainability, security/API contract, and frontend/accessibility/testing.
- Fix every Critical or Important finding, rerun focused checks, and re-review the changed diff.
- Run backend focused suites, frontend focused suites, TypeScript, lint/format checks, and build where feasible.
- Browser-verify 1280x720, 1440x900, 1920x1080, narrow embedded width, and `hideMenu=true`.
- Run `npx gitnexus detect-changes` against the target diff before any feature commit.
- Do not commit or push unless the user separately requests delivery.
