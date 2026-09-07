## 1. Documentation And Safety Gates

- [x] 1.1 Update the CoPaw glossary and OpenSpec artifacts to define categorized Persisted Context Occupancy and the click/focus detail popover.
- [x] 1.2 Run GitNexus impact analysis for every existing symbol modified by the implementation and report any HIGH/CRITICAL risk before editing.
- [x] 1.3 Write the repository implementation plan with exact files, TDD scenarios, validation commands, and dirty-worktree boundaries.

## 2. Backend Tests First

- [x] 2.1 Add estimator tests for system context, summary/long-term prefix context, active tool schemas, online structured messages, category summation, capacity, remaining tokens, thresholds, and overflow.
- [x] 2.2 Add tests proving inactive tool groups, archived raw history, unsent draft text, and billing records are excluded.
- [x] 2.3 Add persistence tests proving the snapshot uses cleaned Agent state, commits atomically with session state, preserves an older snapshot on sampling failure, and never blocks session saving.
- [x] 2.4 Add API tests for available/unavailable snapshots, non-blocking persisted reads, running/stopping stale state, and user/source/agent ownership 404s.
- [x] 2.5 Add API tests proving GET never constructs an Agent, registers MCP, invokes Hooks, or calls a model.

## 3. Runtime Snapshot And Persistence

- [x] 3.1 Add a versioned context-usage snapshot model and server-owned session-state key.
- [x] 3.2 Implement categorized sampling with the configured token counter, `agent.sys_prompt`, effective prefixed memory, online memory, and `toolkit.get_json_schemas()`.
- [x] 3.3 Return only numeric/category metadata and omit prompt, message, schema, and secret content.
- [x] 3.4 Sample the cleaned ordinary Chat Agent state before save and persist the snapshot in the same atomic session mutation.
- [x] 3.5 Preserve the previous committed snapshot when sampling fails; do not write misleading zeroes or block persistence.
- [x] 3.6 Keep Cron skip-history persistence from overwriting the Console Chat occupancy snapshot.

## 4. Ownership-Safe Read API

- [x] 4.1 Add response types for available/unavailable snapshots and configured status thresholds.
- [x] 4.2 Add `GET /chats/{chat_id}/context-usage` using the existing Chat manager, ownership authorization, and persisted non-blocking session reader.
- [x] 4.3 Mark a returned committed snapshot stale while the current Chat is running or stopping.
- [x] 4.4 Return `available: false` without runtime construction when no snapshot exists.

## 5. Frontend Tests First

- [x] 5.1 Add API adapter tests for the context-usage route and response typing.
- [x] 5.2 Add component tests for percentage, unavailable state, used/capacity/remaining values, three categories, progress semantics, and non-color-only status.
- [x] 5.3 Add accessibility tests for button naming, keyboard activation, `aria-haspopup`, `aria-expanded`, focus, and retry behavior.
- [x] 5.4 Add refresh tests for Chat switch races, generation completion, matching compaction, model switch, request failure, and no refresh during typing/stream fragments.
- [x] 5.5 Add a narrow Chat-page wiring test without rewriting existing dirty Chat tests.

## 6. Console Composer UI

- [x] 6.1 Add TypeScript API types and `chatApi` context-usage method.
- [x] 6.2 Add a focused Chat-page hook/component that keeps the last value during refresh and rejects out-of-date responses.
- [x] 6.3 Render the compact `上下文 <percent>` control through the existing composer prefix extension without modifying generic Sender behavior.
- [x] 6.4 Render the unavailable control for new/legacy Chats and add retry guidance without a global toast.
- [x] 6.5 Add a click/focus Popover with progress, approximate totals, remaining capacity, three categories, and status explanation.
- [x] 6.6 Refresh on stable Chat/model/compaction/run-completion events only.
- [x] 6.7 Verify long counts, CJK labels, narrow containers, `hideMenu=true`, visible focus, and reduced-motion-safe behavior.

## 7. Review And Verification

- [x] 7.1 Run focused backend tests for estimator, persistence, API, and existing regular-session save behavior.
- [x] 7.2 Run focused frontend tests for the API adapter, indicator, Chat wiring, and affected composer behavior.
- [x] 7.3 Run Python static checks plus Console TypeScript, lint/format, and build checks appropriate to touched files.
- [ ] 7.4 Manually verify the active Chat UI at 1280x720, 1440x900, 1920x1080, narrow embedded width, and `hideMenu=true`.
- [x] 7.5 Complete the single user-approved independent review/fix cycle and re-run targeted verification after every material fix.
- [x] 7.6 Run GitNexus `detect_changes` against the final target diff and confirm only expected symbols and flows are affected.
