# Python Installation Denial Implementation Plan

> Execute inline against the user-confirmed design, using test-first changes and GitNexus impact checks. Keep unrelated working-tree changes intact.

**Goal:** Reject visible pip-family installation commands before any foreground, background, or source-overridden shell execution, without approval or a terminal Agent stop.

**Architecture:** One syntax-aware policy in `src/swe/security/shell_installation_policy.py`, used at Agent dispatch and shared shell preparation. Inspect final Hook input before approval. Fail closed when inspection is unreliable.

**Tech Stack:** Python, Tree-sitter / Bash grammar, pytest in the project virtual environment.

### Task 1: Command analysis

- [x] Add `tests/unit/security/test_shell_installation_policy.py` with the design's deny/allow matrix, command substitutions, option values, static wrappers, malformed syntax, dynamic command names, parser failure, and ordinary text references.
- [x] Run `venv/bin/python -m pytest tests/unit/security/test_shell_installation_policy.py -q` and confirm failure before adding implementation.
- [x] Add Tree-sitter dependencies to `pyproject.toml` and implement `shell_installation_denial(command) -> str | None`. Return a stable installation-denial reason, a stable unverifiable-input reason, or `None`. Parse rather than execute shell input. Resolve only static command words and inspect visible nested shell code.
- [x] Rerun the test matrix and make it pass.

### Task 2: Mandatory boundaries

- [x] Add tests proving rejected foreground/background commands never start, including a compound command whose first clause would create a marker file.
- [x] Add Agent integration tests for both roles, Tool Guard disabled, preapproval, source override, Hook input rewrite plus `ask`, and continued processing after refusal.
- [x] Run the new boundary tests and confirm failures before editing existing symbols.
- [x] After GitNexus impact checks, connect the shared policy to `prepare_shell_command`, `_acting_impl`, and `_apply_pre_tool_hook`. Use structured `blocked` governance feedback without requesting approval. The check remains independent of tenant configuration.
- [x] Run policy, shell preparation/background, Hook runtime, and SubAgent/Plan Mode integration tests using `venv/bin/python -m pytest`.

### Task 3: Delivery checks

- [x] Update the design status and add a focused playbook entry covering the policy's input boundary, errors, and diagnostic entry points.
- [x] Run formatting and relevant checks. Inspect the final diff and GitNexus `detect_changes` for unexpected scope.
- [x] Report the actual verification results and remaining platform/input limitations. Leave changes reviewable in the working tree; no commit or deployment is required.
