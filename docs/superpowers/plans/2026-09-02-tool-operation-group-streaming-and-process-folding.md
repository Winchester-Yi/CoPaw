---
title: Improve streaming tool operation groups
type: fix
status: active
date: 2026-09-02
origin: docs/brainstorms/2026-08-27-tool-call-grouping-requirements.md
---

# Improve streaming tool operation groups

## Summary

Extend the existing Console-only operation-group presentation so a group appears on its first live tool call, preserves any number of interleaved reasoning messages, keeps every child tool's original expandable card, and moves into the existing execution-process disclosure after the response completes.

---

## Requirements

- R22. Render an open operation group immediately from its first live grouped tool call.
- R23. Reuse the original expandable Tool card for every grouped invocation.
- R24. Preserve one or more reasoning messages inside an open group in stream order without splitting the group.
- R25. Move completed operation groups into ProcessDisclosure while keeping live groups directly visible.

**Origin actors:** A1 Console user, A3 runtime and Console
**Origin flows:** F1 create/update group, F2 collapse/expand
**Origin acceptance examples:** AE12, AE13, AE14, AE15

---

## Scope Boundaries

- Do not change backend operation-group fields, Tool Guard semantics, persistence, or approval replay.
- Do not expose tool details from the group header or custom group summaries; preserve the original Tool card's existing detail boundary.
- Do not infer groups for events without an explicit operation-group declaration.
- Do not redesign ProcessDisclosure or the broader Conversation Workspace.

---

## Context & Research

### Relevant Code and Patterns

- `operationGrouping.ts` keeps reasoning inside an open group and must preserve repeated reasoning messages without changing the explicit group boundary rules.
- `OperationGroup.tsx` owns the default-collapsed group UI; its expanded body currently replaces ordinary Tool cards with simplified step rows.
- `Card.tsx` currently routes every group to `direct`, explicitly excluding groups from ProcessDisclosure.
- `Reasoning.tsx` is the existing rendering contract for reasoning content and should be reused inside a group.

---

## Key Technical Decisions

- Keep grouping as a pure projection over the current merged message list; an unfinished group is a valid renderable result.
- Treat every reasoning message after a grouped tool as part of the open group. Ignore ordinary assistant boundaries with no content or whitespace-only text, including completed boundaries; preserve visible content, errors and interaction metadata as boundaries.
- Derive group status and tool counts from tool messages only; reasoning participates in ordering and process-step counts but not tool status aggregation.
- Reuse the existing Reasoning and Tool components within OperationGroup so grouped children match their ordinary presentation and interaction.
- Route grouped items into ProcessDisclosure only after the existing response-completion gate succeeds.

---

## Implementation Units

### U1. Extend grouping semantics and labels

**Goal:** Preserve repeated reasoning in an open group and render unfinished groups without changing explicit boundaries.

**Requirements:** R22, R23, R24; AE12, AE13, AE14

**Dependencies:** None

**Files:**
- `console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/operationGrouping.ts`
- `console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/operationGrouping.test.ts`

**Execution note:** Add failing tests before changing projection logic.

**Test scenarios:**
- A single running grouped tool produces a group immediately.
- Tool, multiple reasoning messages, and the next same-group tool remain ordered in one group.
- A reasoning-completion boundary with no visible content is omitted and does not split equal group IDs.
- Replay the reported SSE transition from null content to three newlines and then completed text blocks (three newlines plus an empty string); equal group IDs must stay in one group at each projection.
- Whitespace that later becomes visible text must split the group on the next projection. Media, refusal/data blocks, user/system messages, failures and approval/retry/plan metadata must not be discarded.
- User-facing text, ungrouped tools and a new group remain boundaries.
- Reasoning does not alter aggregate tool status.

### U2. Render original tools and reasoning inside operation groups

**Goal:** Reuse the established Tool and Reasoning presentations inside expanded group details.

**Requirements:** R24; AE14

**Dependencies:** U1

**Files:**
- `console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/OperationGroup.tsx`
- `console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/OperationGroup.test.tsx`
- `console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/style.ts`

**Test scenarios:**
- Each grouped tool uses the ordinary default-collapsed Tool card and remains independently expandable.
- Multiple reasoning messages appear between the correct tool cards after expansion.
- Collapsed groups hide reasoning.
- Reasoning does not change the group status icon; the group header does not expose tool details.

### U3. Fold completed groups into execution process

**Goal:** Keep live groups direct and move completed groups under ProcessDisclosure.

**Requirements:** R22, R25; AE12, AE15

**Dependencies:** U1, U2

**Files:**
- `console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Card.tsx`
- `console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Card.test.tsx`

**Test scenarios:**
- A generating response displays the first grouped tool directly without ProcessDisclosure.
- A completed response with final text places the whole group inside ProcessDisclosure.
- Process counts include grouped reasoning and tools once; tool-call counts include tools only.
- Failed grouped tools contribute to the ProcessDisclosure failure count.

### U4. Reduce flagged Python control-flow complexity

**Goal:** Bring the three Sonar-reported functions within the repository complexity budget without changing selected-expert replay, assistant-response extraction, tracing, streaming, or turn-settlement behavior.

**Requirements:** Repository Sonar complexity limit of 15

**Dependencies:** None

**Files:**
- `src/swe/agents/tool_guard_mixin.py`
- `src/swe/app/runner/runner.py`
- `tests/unit/agents/test_complexity_budget.py`
- Existing selected-expert, Stop-hook, TraceSDK and QueryExecution tests under `tests/unit/subagents/` and `tests/unit/app/`

**Execution note:** Add the three reported targets to the executable complexity budget before extracting cohesive helpers.

**Test scenarios:**
- Selected-expert start, wait, terminal, cancellation-fetch and stop paths preserve their existing next action.
- Assistant response extraction keeps accepting supported assistant content and rejecting live/tool messages.
- Query handler preserves both QueryExecution and legacy admission frame order, trace parenting and terminal outcome reporting.
- Each reported function measures at or below 15 using the repository complexity counter.

---

## Verification Strategy

- Run the three focused Response test files with Vitest.
- Run Console TypeScript typecheck, Prettier check and ESLint for changed files.
- Attempt the Console build and one browser screenshot pass; report unrelated baseline blockers without changing them.
- Run `git diff --check` and GitNexus change detection before any commit.

---

## Sources & References

- `docs/brainstorms/2026-08-27-tool-call-grouping-requirements.md`
- `console/DESIGN.md`
- `docs/adr/0003-tool-call-status-is-rebuilt-for-presentation.md`

---

## Follow-up: preserve Tool Guard status across the live adapter

- Add a regression test for the complete `Msg -> AgentScope live adapter -> Runner stream boundary` path and prove that pending approval is not projected as execution failure.
- Copy only trusted Tool Guard governance markers into internal message metadata keyed by tool call ID before the adapter rebuilds tool-result blocks.
- Consume and remove that private metadata at the Runner stream boundary, then reuse the existing `tool_governance` projection for pending, rejected and blocked states.
- Keep ordinary tool output untrusted: `error_type` text alone must not create a governance status.

## Follow-up: render live approval state without a session refresh

- Keep Tool Governance Status separate from execution loading: pending and blocked states stop the spinner and use explicit governance badges on the original Tool card.
- Synchronously mount a newly created assistant response before starting its SSE request so the mounted-response guard cannot discard fast Tool Guard frames.
- Cover the exact live sequence where an assistant message first carries `approval_action` metadata and its text content follows in a later content frame; the current response must immediately contain both the response card and approval action card.
