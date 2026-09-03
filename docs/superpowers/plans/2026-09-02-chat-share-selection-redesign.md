# Chat Share Selection Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the chat-share modal with an in-place selection mode that selects complete Answer Turns and exposes only copy-link and browser-preview actions.

**Architecture:** Keep the existing immutable snapshot API and turn IDs. Provide share-mode state through a Chat-level Context, let each existing Bubble render a synchronized checkbox from the `messageId → turnId` index, and render a fixed bottom toolbar from the existing ChatActionGroup. Generate a URL lazily and invalidate it when selection changes.

**Tech Stack:** React 18, TypeScript, Ant Design, AgentScope Bubble, Less modules, Vitest, existing FastAPI share endpoints.

---

### Task 1: Encode turn-pair selection rules

**Files:**
- Modify: `console/src/pages/Chat/components/ChatActionGroup/shareSelection.ts`
- Test: `console/src/pages/Chat/components/ChatActionGroup/shareSelection.test.ts`

- [x] Add tests for extracting completed user turns, pairing assistant output, default-all selection, and atomic toggle behavior.
- [x] Run the focused Vitest file and verify it fails before implementation.
- [x] Implement small pure helpers used by the page and action group.
- [x] Run the focused Vitest file and verify it passes.

### Task 2: Build the in-place Share Selection Mode UI

**Files:**
- Create: `console/src/pages/Chat/components/ChatShareSelection/index.tsx`
- Create: `console/src/pages/Chat/components/ChatShareSelection/index.module.less`
- Test: `console/src/pages/Chat/components/ChatShareSelection/index.test.tsx`

- [x] Test synchronized checkboxes, disabled incomplete turns, select-all state, empty state, and fixed toolbar actions.
- [x] Run tests to confirm red state.
- [x] Implement a Chat-level selection Context that indexes the existing Bubble message IDs without copying or mutating source messages.
- [x] Add accessible labels, keyboard focus styles, responsive toolbar wrapping, and bottom safe-area padding.
- [x] Run focused tests and verify green.

### Task 3: Integrate lazy URL generation and actions

**Files:**
- Modify: `console/src/pages/Chat/index.tsx`
- Modify: `console/src/pages/Chat/components/ChatActionGroup/index.tsx`
- Modify: `console/src/pages/Chat/components/ChatActionGroup/shareUrl.ts`
- Test: `console/src/pages/Chat/components/ChatActionGroup/index.test.tsx`

- [x] Add tests for default-all initialization, URL cache invalidation after selection changes, copy action, browser action, and successful auto-exit.
- [x] Run focused tests and verify red state.
- [x] Move share loading/selection state to Chat page, let ChatActionGroup only open the mode, and preserve the existing API calls.
- [x] Generate one URL on first action, reuse it for the second action, clear it on selection changes, and close mode after success.
- [x] Run focused tests and verify green.

### Task 4: Remove modal-only implementation and update docs

**Files:**
- Modify: `console/src/pages/Chat/components/ChatActionGroup/index.tsx`
- Modify: `share_mode.md`

- [x] Remove the old selection Modal and duplicate state paths.
- [x] Ensure no stale five-channel copy remains in the design document.
- [x] Run TypeScript, lint, focused tests, and Console build.

### Task 5: Graph and regression verification

- [x] Run `node .gitnexus/run.cjs detect-changes --scope all --repo .` and inspect affected flows.
- [x] Run `venv/bin/python -m pytest tests/unit/app/chat_sharing/test_router.py` to confirm backend contracts remain unchanged.
- [x] Run `pnpm --dir console test:run`, `pnpm --dir console typecheck`, and `pnpm --dir console build`.
