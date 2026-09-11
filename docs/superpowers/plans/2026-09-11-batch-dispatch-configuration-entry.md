# Separate batch configuration and remove the Scheduler token requirement

- Add a visible `批调度配置` action to Cron management rows. Enable it only for saved batch-dispatch parents; a parent's own enabled state or independently paused batch state must not disable configuration.
- Move the existing priority editor and independent run-state control out of the broadcast dialog. Keep recipient selection, batch-mode activation and offset controls in broadcasting. Mount configuration controls only while the new dialog is open.
- Remove token prerequisites/headers from SWE's batch Scheduler proxies and token validation from Scheduler's shared batch-operation dependency. Preserve source/actor requirements, SWE manager/admin checks, source/parent checks, version fencing and worker limits. The follow-up request also removes Scheduler-to-SWE Cron callback authentication; other services' token handling stays unchanged.
- Security boundary: these Scheduler endpoints trust an internal caller and must be network-isolated; caller identity headers are not proof of authentication. No deployment permissions or live network settings are changed by this task.

## Follow-up: remove remaining Scheduler token dependencies

The user subsequently expanded the scope to all Scheduler-token dependencies. Remove the callback client's token constructor argument, environment constants and outgoing header, and remove token verification specifically from SWE's unified Cron callback. Keep other internal SWE/Market routes protected, preserve model authentication, source/tenant/execution identity and `claim_token` fencing. Verify callbacks with configured, missing and invalid tokens for both direct and encoded payloads; verify unrelated internal endpoints still reject unauthenticated calls. Update current Cron documentation/examples so they no longer require Scheduler tokens.

Follow-up verification: 237 targeted tests passed (all Scheduler unit tests, SWE batch proxies/control and callback tests, protected source-template routes), plus 47 internal tenant-scope/callback compatibility tests under the project WSL virtual environment. Initial tests reproduced 13 token-related failures before the fix. Final searches of Scheduler runtime, SWE Scheduler adapters/proxies and tracked deployment/configuration code find no Scheduler authentication-token reads or headers; remaining token mentions are intentional regression fixtures, unrelated service authentication or claim fencing/Cron parsing. Static unused-name/import and syntax checks passed. No live credentials or deployment configuration were modified.

## Execution

1. Update no-token HTTP/API and action/dialog tests first; verify expected failures.
2. Inspect symbol impact and implement the scoped proxy and UI changes.
3. Run relevant Python tests, Console tests, type checking and scoped formatting/lint checks. Verify normal/batch/closed-parent buttons and the configuration dialog with real components in an isolated browser fixture.
4. Update current deployment/security documentation. Preserve previous worker-history changes. No commit or package unless requested.

Engineering and CoPaw frontend skills guide this work. Superpowers tools are unavailable and repository instructions require sequential main-thread work; verification is local self-review, not independent agent review.

## Verification outcome

- Expected failures reproduced before implementation for missing-token calls and the missing configuration entry.
- 64 targeted backend tests passed, including no-token GET/PUT/initialization POST, configured/unconfigured token environments, failure/retry proxies, manager/source validation, version conflicts, and batch operations/claims.
- 33 Console tests passed across the management page, action column, priority editor and run-state control. TypeScript and scoped ESLint/Prettier checks passed. Existing jsdom/Ant Design warnings remain; a full Python Flake8 check also reports the pre-existing 81-character error string in `_ensure_batch_wakeup`, which was not changed.
- Actual components verified with isolated fixtures at 1440x900 and 1280x720: ordinary and child task buttons disabled; enabled batch parents and self-disabled batch parents configurable; separate dialog shows only priority and run-state controls; paused state can be resumed without changing the parent's own enabled state.
- Temporary preview files, browser tab and dev server cleaned up. No real database/API mutations, deployment changes, commit or package creation.
