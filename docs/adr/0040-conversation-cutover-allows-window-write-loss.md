---
status: accepted
---

# Conversation Cutover Allows Limited Window Write Loss

For the MySQL/NAS conversation redesign, the user explicitly accepts losing
the very small volume of legacy writes that may occur after the selected
migration source snapshot and before cutover completes. The migration will not
build a dedicated write-rejection gate, change-data capture, dual writes, or
incremental catch-up solely to preserve those writes. This choice limits
cutover complexity at the cost of acknowledged migration-window loss.

The migration source snapshot must still be internally consistent and fully
validated. The exception does not permit silently skipping damaged snapshot
records, mixing half-written files, discarding unrelated data, or weakening
normal publication guarantees after cutover. Rollback exports must preserve
the new system's subsequently committed changes.

Routine backups have a different boundary: short scoped write quiescence is
allowed to establish a matching database snapshot and NAS content retention
set. Copying can continue after writes resume while referenced objects remain
protected. Backup success requires complete metadata and content for the
declared recovery point; migration-window loss tolerance does not apply.

Confirmed on 2026-10-20. This is design authorization for the trade-off,
not a record of a production migration or actual data loss.
