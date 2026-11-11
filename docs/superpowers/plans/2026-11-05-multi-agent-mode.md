# Multi-Agent Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-Chat Multi-Agent Mode toggle that persists explicit SubAgent intent for later user requests, while preserving existing text intent, Goal authorization, permissions, and expert-selection boundaries.

**Architecture:** Store the Chat mode in Chat metadata and expose a server-side atomic transition that coordinates it with the persisted Expert Main binding revision. Include the mode in each submitted request as `enable_subagents`, while Goal steering continues to establish authorization through its existing durable Goal state. The Console restores the Chat-scoped state, renders the mode affordance, and reports Goal authorization from the authoritative Goal snapshot.

**Tech Stack:** FastAPI/Pydantic, Python ChatManager/repository, React/TypeScript, Ant Design, Vitest, pytest.

---

## File Map

- Modify `src/swe/app/runner/api.py`: add the validated Multi-Agent state transition endpoint and metadata ownership/validation rules.
- Modify `src/swe/app/runner/manager.py`, `src/swe/app/runner/repo/base.py`, and `src/swe/app/runner/repo/json_repo.py`: serialize mode transitions with binding revision changes and update the Chat atomically.
- Modify `src/swe/app/runner/expert_binding.py` and its API route: ensure expert selection cannot be committed while Multi-Agent Mode is enabled, using the same atomic boundary.
- Modify `console/src/api/modules/chat.ts` and `console/src/api/types/chat.ts`: expose the transition API and its response shape.
- Create `console/src/pages/Chat/multiAgentMode.tsx` and `console/src/pages/Chat/multiAgentMode.test.tsx`: encapsulate state resolution, request mode helper, optimistic-safe persistence orchestration, and focused unit tests. Follow the existing `planMode.tsx` pattern where useful.
- Modify `console/src/pages/Chat/index.tsx` and its tests: bind Chat scope, render menu and active control, enforce UI mutual exclusion, pass request intent, and preserve it through Goal steering.
- Modify `console/src/pages/Chat/components/GoalMonitor/index.tsx` and tests: display durable Goal delegation authorization from `GoalSnapshot` and expose an explanation when the mode is switched off while that authorization exists.
- Update `analysis/subagent-current-implementation-report.md`: correct the stale statement about request-level `enable_subagents` only after verifying the completed behavior.
- Keep `CONTEXT.md` and `docs/superpowers/specs/2026-11-05-multi-agent-mode-design.md` as the approved domain/design references.

## Task 1: Add atomic backend Chat mode transition

**Files:**
- Modify: `src/swe/app/runner/repo/base.py`
- Modify: `src/swe/app/runner/repo/json_repo.py`
- Modify: `src/swe/app/runner/manager.py`
- Modify: `src/swe/app/runner/api.py`
- Test: `tests/unit/app/test_multi_agent_mode.py` (create)
- Test: `tests/unit/app/test_expert_main_binding.py`

- [ ] **Step 1: Write failing repository and API tests**

Cover: enabling mode stores `multi_agent_mode_enabled=true`; disabling clears it; missing metadata defaults false; enabling releases a current Expert Main binding and increments/returns its revision; stale `expected_expert_binding_revision` returns a conflict without changing either value; enabling while an explicit expert is selected in non-main mode follows the same cleared selection contract if that selection is server-owned; changing expert binding while mode is enabled is rejected or atomically disables mode according to the approved mutual-exclusion contract; unrelated metadata remains intact.

- [ ] **Step 2: Run the focused tests and confirm the missing behavior**

Run: `venv/bin/python -m pytest tests/unit/app/test_multi_agent_mode.py tests/unit/app/test_expert_main_binding.py -q`

Expected: new tests fail because the atomic Multi-Agent transition does not exist.

- [ ] **Step 3: Implement one serialized transition at repository/manager boundary**

Add a repository operation accepting `(chat_id, enabled, expected_expert_binding_revision)` and returning the updated Chat plus authoritative binding view/revision. In `JsonChatRepository`, perform the read, revision check, mode metadata update, binding release when enabling, and persist under the same repository lock used by expert binding CAS. Add the matching `ChatManager` method under its manager lock. Keep both directions consistent: selecting an expert clears the mode as part of the same write or is rejected with a typed conflict; never perform two independent updates and attempt client rollback.

- [ ] **Step 4: Add an authorized API endpoint and protect server-owned metadata**

Add a strict request model with `enabled: bool` and `expected_expert_binding_revision: int`; add an endpoint under `/chats/{chat_id}/multi-agent-mode`. Reuse `_authorize_chat`; return current mode and binding revision/view. Reject direct writes to `multi_agent_mode_enabled` through generic Chat metadata updates so stale metadata patches cannot bypass the atomic operation. Ensure generic metadata merge continues preserving existing values.

- [ ] **Step 5: Verify focused backend behavior**

Run: `venv/bin/python -m pytest tests/unit/app/test_multi_agent_mode.py tests/unit/app/test_expert_main_binding.py -q`

Expected: PASS, including CAS conflict/no-partial-update and metadata preservation.

## Task 2: Add Console API and isolated mode-state helpers

**Files:**
- Modify: `console/src/api/modules/chat.ts`
- Modify: `console/src/api/types/chat.ts`
- Create: `console/src/pages/Chat/multiAgentMode.tsx`
- Create: `console/src/pages/Chat/multiAgentMode.test.tsx`

- [ ] **Step 1: Write helper tests for state resolution and request intent**

Test metadata true/false/missing, scope changes across Chat IDs, temp-to-real Chat ID aliasing, and request flag construction. The mode helper must return `enable_subagents: true` only for the captured enabled state and must not rewrite message text or mode (`normal`/`plan`).

- [ ] **Step 2: Run helper tests and confirm failure**

Run: `cd console && pnpm test:run src/pages/Chat/multiAgentMode.test.tsx`

Expected: FAIL because the helper and endpoint type are not implemented.

- [ ] **Step 3: Add typed API client and helpers**

Export `MultiAgentModeView` and `MultiAgentModeUpdate` types; add `chatApi.updateMultiAgentMode(chatId, update)` using `PUT /chats/{id}/multi-agent-mode`. Implement pure functions to resolve `multi_agent_mode_enabled` and add the captured `enable_subagents` flag to a request without modifying other business parameters.

- [ ] **Step 4: Verify helper and API client tests**

Run: `cd console && pnpm test:run src/pages/Chat/multiAgentMode.test.tsx console/src/api/modules/chat.multiAgentMode.test.ts`

Expected: PASS; the API test asserts exact URL, method, request body, and returned authoritative state.

## Task 3: Integrate per-Chat toggle, active control, and expert mutual exclusion

**Files:**
- Modify: `console/src/pages/Chat/index.tsx`
- Modify: `console/src/pages/Chat/planMode.tsx` only if a shared active-control primitive is required; avoid changing Plan Mode persistence semantics.
- Modify: `console/src/pages/Chat/index.test.tsx`
- Test: `console/src/pages/Chat/multiAgentMode.test.tsx`

- [ ] **Step 1: Add failing UI and persistence tests**

Cover menu toggle and active composer control; mode restoration per Chat; default off on a new/reset Chat; enabling mode clears a legacy selected expert or releases an Expert Main binding; selecting an expert disables mode; request failure/CAS conflict keeps the prior UI selection; successful switching shows the active state; Plan and Goal can remain enabled alongside Multi-Agent Mode.

- [ ] **Step 2: Run the focused UI tests and confirm failure**

Run: `cd console && pnpm test:run src/pages/Chat/index.test.tsx src/pages/Chat/multiAgentMode.test.tsx`

Expected: FAIL on missing menu item, active control, and transition behavior.

- [ ] **Step 3: Implement scoped state and transactional toggle flow**

Resolve mode from the active Chat metadata with the same temporary-ID handling used for Plan Mode. On toggle, call the atomic API with the current binding revision; publish the returned mode/binding only after success. While transition is pending, disable mode/expert controls. On conflict or failure, retain the old mode and selected expert/binding, sync the authoritative binding if returned, and show a retryable error while preserving the input draft. Wire expert selection through the reciprocal atomic operation. Keep new/reset Chat default false.

- [ ] **Step 4: Add composer menu and active control**

Add a “多智能体” switch in the existing mode submenu and an active, keyboard-accessible “多智能体” control in the input area. The control can switch the mode off. Include a clear notice when the switch releases an expert selection/binding. Respect `composerDisabled`, narrow widths, and `hideMenu=true` while ensuring the active control remains available in the applicable embedded layout.

- [ ] **Step 5: Verify UI switching and regression tests**

Run: `cd console && pnpm test:run src/pages/Chat/index.test.tsx src/pages/Chat/multiAgentMode.test.tsx src/pages/Chat/planMode.test.tsx src/pages/Chat/useExpertBinding.test.tsx`

Expected: PASS; existing Plan Mode and expert-binding behavior remains intact.

## Task 4: Propagate intent to ordinary Chat and Goal steering

**Files:**
- Modify: `console/src/pages/Chat/index.tsx`
- Modify: `console/src/api/modules/chat.ts`
- Modify: `console/src/pages/Chat/index.test.tsx`
- Test: `tests/unit/app/test_subagent_intent.py` (existing intent tests if present; otherwise add focused test)
- Test: backend request route tests covering `/console/chat` parsing and request context.

- [ ] **Step 1: Write failing request propagation tests**

Assert that an enabled mode places top-level `enable_subagents: true` on the captured `/console/chat` request; disabled mode omits/sets false; text remains unchanged; Plan Mode's `mode` stays `plan`; the active Goal steering request passes the same captured flag to `enqueueGoalSteering`; a later mode toggle does not mutate an already-built request; synthetic Goal resume messages do not inherit the current mode flag.

- [ ] **Step 2: Run request tests and confirm failure**

Run: `cd console && pnpm test:run src/pages/Chat/index.test.tsx console/src/api/modules/chat.goalIntent.test.ts`

Expected: FAIL for missing ordinary Chat and Goal steering fields.

- [ ] **Step 3: Capture and pass the request flag**

When `customFetch` builds the request, include `enable_subagents` from the mode state captured at submit. Pass that same captured boolean to `chatApi.enqueueGoalSteering(goalId, chatId, userText, enableSubagents)`. Do not infer persistence from the current UI after request creation, mutate message content, or add the flag to generated Goal resume requests.

- [ ] **Step 4: Confirm backend intent convergence and Goal scope**

Test `has_subagent_intent` with the explicit flag and existing keywords, then test the request context receives `enable_subagents` and `ReactAgent` refreshes the four Background SubAgent tools under the same gate. Verify Goal service persists authorization only when a real steering/current user message enables it; automatic turns and unrelated Chats remain unchanged; false does not revoke an existing Goal authorization.

- [ ] **Step 5: Run focused Console and backend tests**

Run: `cd console && pnpm test:run src/pages/Chat/index.test.tsx console/src/api/modules/chat.goalIntent.test.ts`

Run: `venv/bin/python -m pytest tests/unit/app/test_subagent_intent.py tests/unit/app/test_goal_service.py tests/unit/app/test_runner_subagent_intent.py -q`

Expected: both focused suites PASS. If a named backend test file differs in the checkout, locate the existing intent/Goal/request-context tests with `rg --files tests | rg 'subagent|goal|runner'` and run those exact files.

## Task 5: Show authoritative Goal delegation state and explain non-revocation

**Files:**
- Modify: `console/src/pages/Chat/components/GoalMonitor/index.tsx`
- Modify: `console/src/pages/Chat/components/GoalMonitor/index.test.tsx`
- Modify: `console/src/pages/Chat/index.tsx`

- [ ] **Step 1: Add failing Goal monitor tests**

Cover authorized and unauthorized Goal snapshots, refresh after steering, and display of the durable authorization independently from the current mode toggle. Cover a mode-off action while Goal authorization is true and assert one explanatory notice that disabling the Chat mode does not revoke the active Goal authorization. When snapshot fetch fails, do not show a claim that authorization is absent or revoked.

- [ ] **Step 2: Run the Goal monitor tests and confirm failure**

Run: `cd console && pnpm test:run src/pages/Chat/components/GoalMonitor/index.test.tsx`

Expected: FAIL for missing authorization status and explanatory notice.

- [ ] **Step 3: Render snapshot-backed authorization state**

Render a concise authorization status from `goal.subagent_delegation_enabled`. Trigger the mode-off explanation only when the authoritative current Goal snapshot says delegation is enabled; do not infer Goal authorization from the Chat toggle. Refresh the Goal monitor after the steering response updates Goal state.

- [ ] **Step 4: Verify Goal monitor behavior**

Run: `cd console && pnpm test:run src/pages/Chat/components/GoalMonitor/index.test.tsx src/pages/Chat/index.test.tsx`

Expected: PASS for both Goal authorization states and failure/loading behavior.

## Task 6: Correct documentation and run full feature verification

**Files:**
- Modify: `analysis/subagent-current-implementation-report.md`
- Review: `CONTEXT.md`
- Review: `docs/superpowers/specs/2026-11-05-multi-agent-mode-design.md`

- [ ] **Step 1: Update the implementation report from verified behavior**

Replace the stale statement that ordinary `enable_subagents` cannot expose Background SubAgent tools with the tested actual gate: fixed user-text intent OR explicit request enablement; retain the separate explicit-expert entry and cite ADR 0041.

- [ ] **Step 2: Run feature and repository checks**

Run: `cd console && pnpm test:run src/pages/Chat/index.test.tsx src/pages/Chat/multiAgentMode.test.tsx src/pages/Chat/components/GoalMonitor/index.test.tsx src/pages/Chat/planMode.test.tsx src/pages/Chat/useExpertBinding.test.tsx`

Run: `cd console && pnpm typecheck`

Run: `venv/bin/python -m pytest tests/unit/app/test_multi_agent_mode.py tests/unit/app/test_expert_main_binding.py tests/unit/app/test_subagent_intent.py tests/unit/app/test_goal_service.py -q`

Run: `git diff --check`

Expected: all targeted tests, type checking, and whitespace checks pass.

- [ ] **Step 3: Verify browser requirements**

Run the changed Chat view in the current browser and real Chromium 108/360 Chromium engine. Check desktop and 360px layouts, menu/active-control placement, scrolling, keyboard interaction, and `hideMenu=true`. If the real baseline is unavailable, report that exact gap and do not claim it passed.

- [ ] **Step 4: Review the final diff against the approved design**

Confirm all UI state derives from the owning Chat, Goal authorization derives from Goal snapshots, transitions are atomic under concurrent updates, request flags are captured at submission, text intent remains compatible, and no permission or execution behavior was widened beyond current rules.
