# Token Observation Foundation Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the isolated storage task and review; execute coordinated runtime changes in the current feature workspace. Track each task below. User has authorized implementation; do not add a handoff approval gate.

**Goal:** Deliver the agreed SWE/Monitor A scope: relational storage, request-scoped accounting, versioned events/prepared-input metadata, and explicit completeness.

**Architecture:** Keep independent Token pools/transactions and existing API/rule shapes. Convert the undeployed JSON schema into scalar columns and necessary child relations. Add runtime metadata without changing model calls or collecting original content. Dedicated Usage reconciliation remains deferred.

**Tech Stack:** Python/pytest, FastAPI, aiomysql/MySQL, existing Console/Vitest.

## Task 1: Relational storage and Monitor projections

Files: `scripts/sql/token_diagnostics_tables.sql`, SWE `token_diagnostics/store.py`, Monitor `services/token_diagnostics/store.py` and `query_service.py`; new storage contract/roundtrip tests under `tests/unit/token_diagnostics/`.

- [x] Add failing tests for schema constraints (no JSON/LOB/ENUM/FK, comments and PK), explicit SELECT columns, and write/read roundtrip of run children, Usage details, input metadata, events and five-rule evidence.
- [x] Use scalar columns for all existing persisted metadata; use relations for expected children, rule evaluation/missing fields and finding evidence references. No opaque text payload or general-purpose property bag.
- [x] Preserve revision guards, leases/dirty replay, scope predicates, analysis windows and independent transactions. Reconstruct existing DTO lists/dicts from typed columns/child rows; no API migration or rule expansion.
- [x] New runtime fields to persist: `schema_version`, `telemetry_source`, `execution_origin`, `execution_id`, `identity_source`, `agent_version`; Call `step_id`, `parent_step_id`, `context_id`, `assembled_at`, `assembly_stage`, `prompt_format_confidence`, `context_builder_version`, `prompt_modality`, `usage_origin`, `usage_accuracy`; Event `step_id`, `parent_step_id`, `started_at`, `ended_at`, `tool_kind`, `idempotency_class`, `is_cacheable`, `data_version`. Defaults explicitly describe unknowns.
- [x] Run `venv/bin/python -m pytest tests/unit/token_diagnostics/ -q`; watch new tests fail before implementing and pass afterward. Adjust existing cleanup test to include all new Token-only child tables.

Contract examples (Python DTO values, not SQL JSON):

```python
assert run["expected_child_branches"] == ["child-1"]
assert call["usage_details"] == {"cache_read_tokens": 8}
assert call["input_tokens"] == 10
assert finding["evidence"]["call_ids"] == ["call-1", "call-2"]
assert run["analysis_summary"][0]["missing_fields"] == ["fingerprint"]
```

## Task 2: Request identity and event contract

Files: `src/swe/token_diagnostics/context.py`, `runtime.py`, new `contract.py`, `tests/unit/token_diagnostics/test_foundation_runtime.py`.

- [x] Add failing tests: same scoped AnswerTurn identity gives the same Task/root branch; different execution/source/tenant gives another; scheduled execution retains its origin; child/auxiliary work inherits Task/version; events have identity and actual observed times.
- [x] Define `SCHEMA_VERSION = "1.0"`. Root execution association uses AnswerTurn when available, then actual scheduled persistence identity, otherwise Token-generated identity. Deterministic scoped UUIDs may represent known execution identities; never use session/B3 ID as a root key. Restore identity association without rerunning the model.
- [x] Pass allowlisted entry claims through accounting_query. Add explicit event source/version and execution association. Tool safety properties are unknown/false unless reliably declared; compact events preserve actual epoch and nullable unavailable sizes.
- [x] Run new runtime tests and existing runner/subagent-related diagnostics tests. No modification to admission/attachment coordination is required.

```python
assert first.fields["task_run_id"] == recovered.fields["task_run_id"]
assert first.fields["task_run_id"] != later.fields["task_run_id"]
assert event["schema_version"] == "1.0"
assert event["tool_kind"] == "unknown"
assert event["is_cacheable"] is False
```

## Task 3: Prepared context and Usage provenance

Files: `model_observer.py`, `input_capture.py`, `models.py`; tests in `test_foundation_runtime.py`/`test_capture.py`.

- [x] Add failing tests for context-to-call identity, messages_only/message_approx metadata, media unknowns, Usage origin/accuracy, and no original content in records.
- [x] Each observable attempt has a new context ID and step ID, with parent branch/step association. Persist actual assembly time; keep UTF-8 estimator explicitly approximate. Preserve stream cumulative snapshots and error cleanup; do not invent physical Provider request IDs.
- [x] Retain unknown Usage as null, Provider-reported values distinct from estimates, and existing cache/reasoning child semantics. Verify with numeric controlled responses only; no live reconciliation script or paid requests.
- [x] Run `venv/bin/python -m pytest tests/unit/token_diagnostics/ -q` and targeted factory/tool-output/compaction integration tests.

```python
assert call["assembly_stage"] == "messages_only"
assert call["prompt_format_confidence"] == "message_approx"
assert call["context_id"]
assert call["usage_origin"] == "provider"
assert "PRIVATE" not in str(call)
```

## Task 4: Verification and docs

- [x] Add/run isolated MySQL schema/write/Monitor roundtrip if a local server can be provisioned without using business credentials. No original business content is needed. If unavailable, deliver an opt-in integration test and explicitly report the unexecuted check.
- [x] Run scoped pytest, Console diagnostics Vitest, Black/Flake8 on changed Python files and `git diff --check`. Run GitNexus detect_changes with complete output; partial graph evidence is a limitation, not a clean check.
- [x] Review spec compliance and code quality; fix concrete findings and rerun affected tests.
- [x] Update deployment/playbook/spec: all Token tables, initial-only schema, event/version/unknown semantics, existing five-rule compatibility, deferred reconciliation and actual verification results.

User edits in AGENTS.md/CLAUDE.md and unrelated untracked artifacts are preserved. Work stays on feature branch `token_optimize`; no commit, push or database operations against a business environment are requested.


## Verification Record

- 70 Token diagnostics tests passed, including relational round trips, lifecycle replay, explicit unknown timestamps, bounded writer failures, stream/retry usage, identity scope, and privacy checks.
- 115 scoped backend tests passed with `SWE_TRACING_ENABLED=false`; the enabled factory integration is covered by the Token factory test.
- Console Token Diagnostics Vitest: 4 tests passed.
- Black and flake8 checks passed for changed Python files under repository pre-commit settings.
- GitNexus graph rebuilt; final detect-changes result was complete (`partial=false`, `truncated=false`), 145 changed symbols, risk level low, no affected process flows resolved. Process indexing has entry/branch budgets and dynamic dispatch limitations; this is not proof of no downstream effects.
- Disposable MySQL daemon was unavailable, so the opt-in `TEST_TOKEN_MYSQL_DSN` integration path was not executed. No business database or live Provider request was used.
