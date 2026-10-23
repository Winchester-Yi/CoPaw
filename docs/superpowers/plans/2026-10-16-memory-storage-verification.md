# Memory storage verification implementation plan

**Goal:** Run local two-process storage checks and repeatable migration/rollback rehearsals without touching existing user sessions. Actual two-Pod NAS acceptance remains pending because the user currently has no Pods.

**Architecture:** Extend the existing operator verifier with hard-link checks and a reader handshake so an empty observation window cannot pass. Use real CLI migrations and exports on synthetic session/journal fixtures; reopen exports through legacy readers and append new data. Run fixtures under a fresh `.swe/.storage-verification` directory and retain evidence.

**Scope:** No application storage behavior changes, no production migration, no format flags enabled.

## 1. Storage primitive verification

- [x] Add tests in `tests/unit/scripts/test_verify_session_nas_lock.py` for hard links, indexed worker read handoff, and two-process execution.
- [x] Verify new checks fail against the current script, then extend `scripts/verify_session_nas_lock.py`.
- [x] Check nonblocking flock exclusion, SIGKILL release, atomic JSON replacement, directory/file fsync and exclusive hard-link publication; require a nonempty reader sample and final revision match.
- [x] Add matching Pod labels to the existing Job's anti-affinity selector so later two-Pod runs require distinct nodes.

## 2. Migration and rollback rehearsal

- [x] Add `tests/integrated/test_memory_storage_rehearsal.py` with real CLI preview/apply/validate/export steps on synthetic data.
- [x] Compare all turn bodies/statuses and journal event identities/epochs, include writes and updates after migration, then reopen old-format exports and append again.
- [x] Record record counts and canonical SHA-256 digests as JSON evidence without exporting user data.
- [x] Run existing subprocess abrupt-exit recovery and concurrent-writer regressions beside the rehearsals.

## 3. Execution and evidence

- [x] Run all new checks locally, then on a newly created child of `/Users/shixiangyi/.swe/.storage-verification` (filesystem permission required).
- [x] Save commands, results, file-system identity, limitations and report paths in `docs/superpowers/specs/2026-10-16-memory-storage-verification-report.md`.
- [x] Run focused pytest and pre-commit; update the Playbook. Leave actual two-Pod NAS acceptance unchecked.
