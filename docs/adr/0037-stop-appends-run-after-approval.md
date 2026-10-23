---
status: accepted
---

# Stop Appended Messages Run After Completion Approval

Stop Appended Messages are produced by dedicated command, HTTP, or prompt Handlers only after the final response passes the existing Stop completion gate. We choose a separate, opt-in append phase over contributions generated during validation so rejected attempts do not generate append content, ordinary response streaming remains available, and append failures cannot reopen model execution or veto the approved response. Appended content never starts another Stop evaluation.

Each successful contribution becomes a distinct assistant message in the same Answer Turn and enters both Chat history and subsequent model context. This deliberately chooses shared conversation evidence over a display-only record. Handlers run concurrently against the same pre-append context, while messages are committed and displayed in fixed source/configuration order.

Persistence precedes stream publication: a committed message counts as delivered even if the browser has not received it, remains available after stopping or reconnection, and is deduplicated by its stable identity. Uncommitted results may be lost on a crash; recovery does not rerun append Handlers, and no exactly-once guarantee is made for external side effects. Handler failures are diagnostic-only, while storage failures still follow the existing session settlement rules.

This extends [ADR 0020](0020-stop-is-the-unified-completion-hook.md) without adding a competing completion gate or weakening the replacement finalization rules in [ADR 0029](0029-stop-output-transformations-use-strict-two-phase-finalization.md). The first release covers Console delivery for ordinary answers and Goal formal delivery; other Channels do not execute append Handlers.
