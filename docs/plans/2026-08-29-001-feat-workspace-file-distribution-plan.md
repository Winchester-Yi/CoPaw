---
title: "feat: Distribute a workspace file across user scopes"
type: feat
status: completed
date: 2026-08-29
---

# feat: Distribute a workspace file across user scopes

## Summary

Add a backend endpoint that resolves a source file inside the current request's Agent workspace and copies it to the same Agent workspace in each explicitly identified target scope, using relative source and destination paths throughout.

---

## Requirements

- R1. Resolve the source workspace from the current request headers and existing Agent workspace rules.
- R2. Accept camelCase `sourcePath`, `targetPath`, and a non-empty list of `{tenantId, sourceId}` targets; reject legacy snake_case and caller-supplied `scopeId` fields.
- R3. Derive each target `scopeId` from `tenantId + sourceId`, and reject invalid identities, absolute paths, traversal, symbolic-link escapes, missing/non-file sources, and duplicate derived scopes.
- R4. Copy the source file to `<WORKING_DIR>/<scopeId>/workspaces/<current-agent>/<targetPath>`, creating ordinary parent directories and replacing an existing regular target file.
- R5. Return one result per target so a failure for one target does not prevent attempts for the remaining targets.

---

## Scope Boundaries

- Only regular files are copied; directories and symbolic links are not supported.
- The endpoint does not bootstrap missing target users or Agent workspaces.
- The endpoint does not add a new authorization model; it remains behind the repository's existing tenant, source, and authentication middleware.
- The endpoint does not provide transactional rollback across targets.

---

## Context & Research

### Relevant Code and Patterns

- `src/swe/app/agent_context.py`: resolve and verify the current Agent workspace without starting its runtime.
- `src/swe/config/context.py`: canonicalize and decode runtime scope identifiers.
- `src/swe/app/routers/hook_management.py`: validate cross-tenant targets and return per-target distribution results.
- `src/swe/app/routers/files.py`: existing file-related HTTP surface.

### External References

- Python `pathlib.Path.resolve()` resolves symbolic links and removes `..` components before containment checks.
- Python `shutil.copy2()` replaces an existing file and attempts to preserve file metadata, subject to platform limits.

---

## Key Technical Decisions

- Reuse `resolve_file_manager_workspace_dir()` for the source workspace so header, active-Agent, and workspace-root validation stay centralized.
- Generate each target scope with the repository's canonical `encode_scope_id(tenant_id, source_id)` helper instead of trusting a caller-supplied scope.
- Keep Python model attributes snake_case internally while requiring and emitting camelCase aliases at the HTTP boundary.
- Resolve source and target candidates before checking that they remain within their workspace roots; reject symlink endpoints explicitly.
- Preserve batch semantics by returning per-target success/error results while rejecting malformed request-wide inputs before any copy begins.

---

## Implementation Units

### U1. Define and test the copy contract

**Goal:** Establish request validation, safe path behavior, copy semantics, and per-target results with failing tests first.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** None

**Files:**
- Test: `tests/unit/routers/test_files.py`

**Approach:**
- Exercise the route function with a request carrying a verified current Agent workspace.
- Cover both request-wide validation failures and isolated target failures.

**Execution note:** Start with failing tests and confirm they fail because the new contract is absent.

**Patterns to follow:**
- `tests/unit/routers/test_hook_management.py`
- `tests/unit/routers/test_skills_tenant_scope.py`

**Test scenarios:**
- Happy path: copy one source file to two matching target scopes, create nested parent directories, and preserve content.
- Happy path: replace an existing regular target file.
- Edge case: one missing target workspace fails while a later valid target still succeeds.
- Error path: reject an absolute or traversing source/target path.
- Error path: reject source and target paths that escape through symbolic links.
- Error path: reject invalid `tenantId`/`sourceId`, duplicate derived scopes, legacy snake_case fields, and explicit caller-supplied `scopeId`.
- Error path: reject an empty target list and a missing or non-file source.

**Verification:**
- The focused test file fails before implementation and passes after U2.

### U2. Implement the workspace file distribution endpoint

**Goal:** Add the validated copy endpoint to the existing file router.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** U1

**Files:**
- Modify: `src/swe/app/routers/files.py`
- Test: `tests/unit/routers/test_files.py`

**Approach:**
- Add strict camelCase Pydantic request/response aliases and small single-purpose helpers for relative-path validation, containment, target identity validation, scope derivation, and one-target copying.
- Use `shutil.copy2()` only after all request-wide validation completes.
- Report filesystem failures as target-specific errors without exposing file content.

**Patterns to follow:**
- `src/swe/app/routers/hook_management.py`
- `src/swe/app/routers/skills.py`

**Test scenarios:**
- Integration: the route resolves the current Agent from request state/header conventions and targets the same Agent directory name in each scope.
- Regression: the existing file preview route remains registered and unchanged.

**Verification:**
- Focused tests and router import/registration checks pass on the project virtual environment.

---

## System-Wide Impact

- **Interaction graph:** request middleware binds tenant/source/scope, the endpoint resolves the current Agent workspace, then performs independent filesystem copies.
- **Error propagation:** malformed batch input is an HTTP error; target-local filesystem failures are recorded in that target's result.
- **State lifecycle risks:** earlier successful copies remain if a later target fails; the response makes that partial outcome explicit.
- **API surface parity:** no CLI or Console surface is added.
- **Integration coverage:** route tests cover real path construction and file writes under temporary workspace roots.
- **Unchanged invariants:** existing preview behavior and current workspace resolver remain unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Path traversal or symlink escape | Resolve paths and enforce workspace-root containment; reject symlink endpoints. |
| Forged or inconsistent target scope | Derive scope server-side from validated `tenantId + sourceId`; forbid caller-supplied `scopeId`. |
| Partial batch writes | Preserve per-target results and do not claim transactionality. |
| Platform metadata differences | Treat file content copy as the contract; `copy2()` metadata preservation is best effort. |

---

## Documentation / Operational Notes

- Add endpoint summary and response models to generated OpenAPI through FastAPI declarations.
- Monitor errors by endpoint path and target result failures; no database migration or feature flag is required.

---

## Sources & References

- Related code: `src/swe/app/agent_context.py`
- Related code: `src/swe/config/context.py`
- Related code: `src/swe/app/routers/hook_management.py`
- Python pathlib documentation: https://docs.python.org/3/library/pathlib.html
- Python shutil documentation: https://docs.python.org/3/library/shutil.html
