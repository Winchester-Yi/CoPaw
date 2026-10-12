# Token Usage Diagnostics Implementation Plan

> **For agentic workers:** Use the accepted design and execute the tasks sequentially. The repository's AGENTS.md maps agent work to sequential work in the main task; review reports must identify that constraint rather than claim independent agents reviewed the code.

**Goal:** Save independent per-attempt Token records, diagnose five classes of potential inefficiency, and expose numeric evidence in Console.

**Architecture:** SWE publishes numeric snapshots through a bounded, best-effort collector to independent MySQL tables and transactions. Monitor owns pure rules, revision-aware background analysis and query APIs. Trace references remain optional and are not foreign keys.

**Tech Stack:** Python/Pydantic/aiomysql/FastAPI; MySQL; React/TypeScript/Ant Design.

**Spec:** `docs/superpowers/specs/2026-10-10-token-usage-diagnostics-design.md`

## Global Constraints

- Work only in the existing `codex/token-usage-diagnostics` managed worktree.
- Enablement follows effective `SWE_TRACING_ENABLED`, independently of successful Trace initialization.
- Own IDs, pool, tables, transactions, retries, analysis, queries and cleanup; business execution wins over telemetry delivery.
- No original messages/results, no model-based rules, no automatic runtime optimization, no business-case setup.
- Unknown usage stays null; include physical observable retries without counting cache/reasoning subcomponents twice.
- Verify affected existing symbols with fresh GitNexus upstream impact before editing.
- Use the project virtualenv. This worktree uses the existing repository virtualenv; test imports must resolve to this worktree's source.
- Do not commit or push unless the user requests delivery through Git.

## Task 1: Protocol, numeric usage and input evidence

Files: `src/swe/token_diagnostics/{models,config,context,usage,input_capture,provenance}.py`, `tests/unit/token_diagnostics/test_capture.py`.

- [x] Write failing tests for zero/unknown/partial usage, cache subitems, HMAC scope, structured tool content and partial capture limits.
- [x] Add independent event DTOs and immutable request evidence with explicit provenance/quality.
- [x] Verify defaults do not load tokenizers, collect raw payloads or require Trace imports.
- [x] Verify: project Python `-m pytest tests/unit/token_diagnostics/test_capture.py -q`.

## Task 2: Independent schema, store and bounded writer

Files: `scripts/sql/token_diagnostics_tables.sql`, `src/swe/token_diagnostics/{store,writer,collector}.py`, `tests/unit/token_diagnostics/test_writer.py`, `docs/deploy/token-diagnostics.md`.

- [x] Write failing tests for independent transactions, revision replay, bounded buffering, expiry, failed writes and shutdown limits.
- [x] Implement five Token tables, own connection acquisition, transactional snapshots and branch revisions.
- [x] Preserve business execution on collector/writer failure; expose evidence gaps without inferring missing records.
- [x] Verify: project Python `-m pytest tests/unit/token_diagnostics/test_writer.py -q`.

## Task 3: Model attempt and stream lifecycle

Files: `src/swe/token_diagnostics/model_observer.py`, `src/swe/agents/model_factory.py`, `tests/unit/token_diagnostics/test_model_observer.py`.

- [x] Write failing tests for observable retries, normal/non-stream calls, exception, cancellation and explicit stream close.
- [x] Connect logical identity outside retry and attempt observer inside retry; response stream consumed once.
- [x] Verify callbacks and writer exceptions never replace model output or original errors.
- [x] Verify: project Python `-m pytest tests/unit/token_diagnostics/test_model_observer.py tests/unit/tracing/test_model_wrapper.py -q`.

## Task 4: Task, subtask, auxiliary and tool evidence integration

Files: `src/swe/app/_app.py`, `runner/runner.py`, `subagents/{runtime,worker}.py`, model/prompt/tool producers, `tests/unit/token_diagnostics/test_runtime.py`.

- [x] Write failing tests for root identity, optional Trace binding, child snapshot inheritance, auxiliary purposes and tool-result operation identity.
- [x] Initialize/shutdown independent writer; bind accounting context around query and child execution.
- [x] Publish tool/compaction facts and reliable source sidecars; unknown origin remains unclassified.
- [x] Verify existing entry/stop/subagent contracts with focused tests; report platform-specific collection limitations precisely.

## Task 5: Five deterministic rules

Files: `monitor/src/monitor/app/services/token_diagnostics/rules.py`, `tests/unit/token_diagnostics/test_rules.py`.

- [x] Write failing boundary tests for all five rules, repeat-vs-poll, zero baselines, differing epochs and missing metadata.
- [x] Implement versioned findings with stable keys, evidence and insufficient-evidence reasons.
- [x] Verify rule computations use original estimates and distinct operations/logical calls.
- [x] Verify: project Python `-m pytest tests/unit/token_diagnostics/test_rules.py -q`.

## Task 6: Monitor queries and revision-aware analyzer

Files: `monitor/src/monitor/app/services/token_diagnostics/{store,query_service,analysis}.py`, `models/token_diagnostics.py`, `routers/token_diagnostics.py`, Monitor lifecycle/router registration, `tests/unit/token_diagnostics/test_monitor.py`.

- [x] Write failing tests for range isolation, known/unknown totals, lease takeover, stale claims and concurrent data revisions.
- [x] Implement independent pool, read projections and dirty-branch scanner with conditional lease claims.
- [x] Expose read-only run/call/input/finding/Trace-reference endpoints, with bounded pagination and safe failure states.
- [x] Implement independently bounded retention cleanup under the same branch lease guard.
- [x] Verify: project Python `-m pytest tests/unit/token_diagnostics/test_monitor.py -q`.

## Task 7: Console diagnostics

Files: `console/src/api/{types/tokenDiagnostics,modules/tokenDiagnostics}.ts`, `pages/Analytics/TokenDiagnostics/`, Analytics routes/navigation, Trace detail link, locales.

- [x] Write failing UI tests for null data, trace/run selection races, empty/failed/insufficient analysis and category derivation.
- [x] Build the white-first run list, usage/category evidence, call sequence and finding panels, using own request controller.
- [x] Add discoverable navigation and optional Trace correlation entry without mutating existing statistics.
- [x] Verify targeted Vitest tests, TypeScript, scoped lint/format and Console build.

## Task 8: Cross-boundary review and delivery verification

- [x] Review protocol/privacy, streaming/runtime, store/analysis and UI/contracts as separate substantive passes; resolve findings and rerun affected verification.
- [x] Fresh focused Python regression suites, scoped static checks and Console build.
- [x] Review final diff against accepted design and record live-MySQL/provider/browser limits separately from verified tests.
- [x] Report exact worktree/branch, verification and remaining deployment prerequisites; do not push.

## 完成记录

2026-10-11 完成首期实现与本地校验，未提交或部署。实现文件采用合并后的模块内类型，实际清单与验证限制见 `docs/superpowers/reviews/2026-10-11-token-diagnostics.md`；迁移、身份网关和分析窗口说明见 `docs/deploy/token-diagnostics.md`。
