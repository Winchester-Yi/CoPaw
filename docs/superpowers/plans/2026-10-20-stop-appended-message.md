# Stop Appended Message Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Preserve normal response streaming and add durable, independent Markdown messages after final Stop approval.

**Architecture:** Explicit `outputAppend` handlers are excluded from validation and run concurrently in a post-approval phase. Contributions become true assistant memory messages, committed before publication through a Stop-coordinated boundary, then grouped by stable logical identity in Console.

**Tech Stack:** Python/Pydantic/asyncio/pytest; React/TypeScript/Vitest; existing AgentScope runtime/session ledger.

Approved specification: `docs/superpowers/specs/2026-10-20-stop-appended-message-design.md`.
Worktree: `.worktrees/stop-appended-message`; tests use `PYTHONPATH=src ../../venv/bin/python -m pytest`.

## Task 1 — Hook declaration, protocol, execution and collection

Files: `src/swe/agents/hook_runtime/{models,output,executor,resolver,runtime}.py`,
new `src/swe/agents/hook_runtime/append.py`, `src/swe/config/config.py`,
tests under `tests/unit/agents/hook_runtime/` and `tests/unit/config/`.

- [x] Add failing contract tests for Stop-only outputAppend, transform/once conflicts, allow/appendText/no-op and strict fields; prove RED.
- [x] Add `output_append` alias `outputAppend` and `append_text` normalized result; default total budget 30 seconds in Agent Profile hook runtime settings.
- [x] Normalize shared append output with required non-empty reason <=2,000 chars and optional non-empty appendText <=16,384 UTF-8 bytes; preserve exact text.
- [x] Add append-aware bounded command/HTTP/prompt reads (256 KiB response, 64 KiB stderr), resource cleanup and effective diagnostic-only failure behavior.
- [x] Exclude append handlers from ordinary/transform plans. Add source-aware, Skill-name-sorted append plan with frozen context, normal matcher/overlay/if rules.
- [x] Add concurrent collection API returning ordered contributions, preserving completed successes at deadline, enforcing 65,536 total UTF-8 bytes and cancelling unfinished work.

Integration API:

```python
class StopAppendContribution(BaseModel):
    source: str
    group_id: str
    handler_id: str
    order: int
    text: str

# HookRuntime method; optional snapshot provider follows existing finalization.
async def emit_stop_append(self, context, *, workspace_dir,
                           max_append_seconds, conversation_snapshot_provider=None,
                           stop_requested=None) -> list[StopAppendContribution]: ...
```

- [x] Verify new tests RED→GREEN and existing hook tests: `PYTHONPATH=src ../../venv/bin/python -m pytest tests/unit/agents/hook_runtime tests/unit/config/test_hook_config_models.py -q` (229 passed).
- [x] Spec review, then quality review; resolve findings before next task (logging isolation, pipe cleanup, Python 3.10 compatibility and deadline race fixed).

## Task 2 — Durable post-approval delivery and stopping

Files: `src/swe/app/runner/{turn_lifecycle,runner,query_attempt}.py`, new
`src/swe/app/runner/stop_append.py`, `src/swe/app/answer_turn/coordinator.py`,
tests under `tests/unit/app/` and `tests/unit/answer_turn/`.

- [x] Add failing tests proving original final message precedes append, rejected attempts never execute append, and ordinary/Goal final delivery share behavior.
- [x] Add narrow coordinator commit admission operation: verify exact current turn and non-STOPPING status under coordinator lock, invoke short session persistence callback, never hold this lock during handler execution.
- [x] New runner helper collects only on Console, enabled/non-skipped Stop and nonempty approved response; guard request-scoped phase start against rerun.
- [x] Create one stable Msg.id per contribution, role assistant, metadata.stop_append with message_id/turn/source/group/handler/order.
- [x] Append to true agent memory, commit complete state through existing held session execution, then yield; restore only uncommitted local memory on failure and preserve commits across cleanup/cancel.
- [x] Check stop at entry, during collection and before each commit; retain committed messages on stop, cancel remaining work, do not confuse disconnect with stop.
- [x] Block generic query retry after append begins; storage errors enter nonretryable settlement path, never rerun model or hooks.
- [x] Verify runner/Goal/coordinator/history tests, including v2/v3 session state, commit-before-send, cancellation and replay.
- [x] Spec review then quality review; resolve v3 published-write cancellation and safe Trace error-closure findings (46 append tests passed in final quality recheck).

## Task 3 — Console independent bubbles and Hook management

Files: Console Chat runtime aggregation hooks, `pages/Chat/sessionApi/index.ts`, new shared marker helper,
`api/modules/hookManagement.ts` (existing Hook types), `pages/Control/HookManagement/` and existing tests.

- [x] Read console/DESIGN.md and apply CoPaw frontend checks.
- [x] Add failing tests for direct/nested metadata markers, stable logical ID, ordered independent bubbles, replay upsert and history parity.
- [x] Keep runtime event Message.id/content.msg_id aggregation intact; split display cards using controlled metadata.stop_append.message_id.
- [x] Add append mode switch for command/http/prompt, mutually exclusive with transform, clearing/disabling once; fixed effective failure policy and return/size help.
- [x] Add configuration and manual-test UI assertions; preserve existing endpoints and embedded behavior.
- [x] Verify focused Vitest, typecheck, build and changed-file formatting/lint (145 tests passed; existing lint findings only).
- [x] Spec review then quality review; safe summary output_append field restored and covered in page/modal tests.

## Task 4 — Management test summaries, examples and documentation

Files: `src/swe/app/hook_management.py`, router tests, new `wiki/hook/stop-output-append-command-demo/`,
`wiki/hook/{README,hook-runtime}.md`, targeted `analysis/playbook/` additions.

- [x] Test single-handler manual execution exposes append status/byte count/failure/effective policy without returning generated content or writing memory.
- [x] Implement minimal summary changes; add working stdin→JSON command demo.
- [x] Document streaming, limits, cancellation, first-release Console scope and crash/no-rerun behavior (49 tests; independent spec and quality reviews approved).
- [x] Mark design/ADR accepted after matching implementation and validation.

## Task 5 — Integration verification and review

- [x] Run combined affected Python suites and Console focused suites/typecheck/build (651 Python tests; 145 Console tests).
- [x] Use actual subprocess regression tests for timeout/size/cancellation, not mock-only assertions.
- [x] Review all accepted spec criteria 1–14; add real command → approved Stop → durable v2/v3 history → actual next model input, compaction archive and sharing coverage.
- [x] Run git diff --check and GitNexus detect_changes before any commit; never include unrelated root worktree changes (exact worktree analyzed; no commit made).
- [x] Complete independent spec review followed by quality review and final feature review (all 14 criteria approved; final quality review found no actionable defects; each reviewer independently ran 92 core tests).

Execution is already authorized by the user; proceed in this session without another execution-choice gate.

## Verification record (2026-10-20)

- Combined affected Python regression: **651 passed**. Includes hook protocol/execution,
  configuration, normal/Goal finalization, Stop arbitration, query retry, v2/v3 session
  ledger, Chat history/turn API, management service/router, compaction and Goal suites.
- Console focused regression: **145 passed** across 10 files. TypeScript typecheck,
  production build and Prettier checks for all 16 changed TS/TSX files passed.
- Four new cross-layer acceptance cases use real command subprocesses and production
  Stop/runtime/session/history stages. An actual AgentScope reasoning call into a local
  capturing model receives the restored appended Markdown as assistant content, without
  reason, diagnostics or Hook metadata. Archive/share preserve stable IDs and turn membership.
- Browser QA uses mocked API data (not a deployed-backend end-to-end test): normal Hook
  editor and history at 1280/1440/1920 widths, plus hidden-menu embedded mode. Original
  body and two appended Markdown responses remain separate and ordered.
- Full changed-file pre-commit: all hooks pass except **11 existing runner mypy arg-type
  errors**. The identical 11 errors reproduce when checking the 14 corresponding baseline
  files together; those root-worktree files match base `bc54a0c`. New append/runtime/test
  code passes its scoped mypy/flake8/pylint checks. Existing frontend any/dependency lint
  warnings and build chunk/import warnings are unchanged.
- GitNexus exact-worktree analysis: 47 changed files, 73 indexed symbols, CRITICAL broad
  impact through core imports. New symbols are not in the baseline index and adjacent-symbol
  hunk attribution is approximate; source inspection and focused regression remain required.
- Generated browser artifacts and the temporary Console dependency symlink are not product
  changes. New product files are intent-to-add only; no content is staged or committed.
