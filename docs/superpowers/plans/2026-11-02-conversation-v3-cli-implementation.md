# Conversation v3 CLI Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development with focused review and TDD.

**Goal:** Implement automatic discovery and DML-only migration into manually prepared tables.
**Architecture:** Pure discovery freezes a v3 manifest. A SQL journal owns tickets and progress. Existing typed publication semantics remain; dedicated named-lock connection serializes import and durable completion receipts gate activation.
**Tech Stack:** Python, Click, pytest, existing async MySQL facade and original NAS bundles.

Approved design: docs/superpowers/specs/2026-11-02-conversation-discovery-precreated-import-design.md. User authorizes design followed by implementation in this session; reuse original worktree.

## Tasks and validation

- [ ] Discovery/freeze: migration/discovery.py and v3_manifest.py; tests/unit/app/conversation_storage/test_v3_discovery.py. Validate disabled profiles, exact identities, path rebasing, orphan coverage, share-only services, fingerprints, stable positions. Watch tests fail before implementation.
- [ ] Journal/import: migration/journal.py and direct_import.py; narrowly extend importer/install_records with injected store and async allocation hooks. Five entity types use entries.id tickets; Retention uses own IDs; original share CAS updates. Tests cover persistence, resumption, transactions, no DDL and unknown-row refusal.
- [ ] Authority/restore: migration/authority.py plus runtime and backup hooks. Tests cover incomplete jobs, normal reboot and actual restored scope receipts with no old import resume.
- [ ] CLI/orchestration: discovery/freeze/preflight/resume/finalize; v3 direct application target; refuse v2 DDL writes. Full schema and source validation, pinned named-lock connection, verified original-path runtime manifests.
- [ ] Verification/docs: full conversation unit regression and isolated DML-only MySQL tests, exact operational guide and SQL status, independent review, hooks and commits.

## Shared interfaces

Discovery: discover_services(services, *, shares=(), overrides=()) returns report. freeze_discovery(report,path,boundary,application_schema) returns V3Manifest. Services list entries: service_unit_id/source_root/config_root/nas_root, optional snapshot_root/original_root. Manifest has version=3, services, workspaces, source_boundary, offline.application_schema, assignments and batches, supporting old manifest API. Semantic inventory remains version=2 with stable positions.

Journal: MigrationJournal(database,manifest), async prepare(), allocate(kind,position), set_batch_state(position,state), hydrate(), mark_state(state,verification=None). DirectImporter follows old importer interface. Allocation becomes awaited through base _allocate hook; assigned reads cached assignments. Direct target schema equals application_schema; migration_id remains fixed. Never invoke DDL installer.

Authority: validate_authority(database,document), backup_authority(database,scope), restore_authorities(transaction,receipts,scopes,roots). Runtime accepts optional migration_authority in v2 mapping with strict receipt verification. Old mappings cannot bypass incomplete jobs for that service.

One coordinated job covers all services. Initial six business tables empty; later only same-job resumption. DatabaseConnection.named_lock pins its acquire connection for nested work, so no reconnect-and-continue. Keep credentials outside artifacts.

## Execution

Each component writes and runs failing tests first using ../../venv/bin/python -m pytest, implements minimally, then runs adjacent regressions. Final scope: tests/unit/app/conversation_storage and CLI tests. MySQL fixtures use disposable schemas and temporary NAS; never print dev.json credentials. Protect pre-existing user changes.
