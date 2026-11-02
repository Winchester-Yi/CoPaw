# Conversation Service Unit Isolation Implementation Plan

> **For agentic workers:** Use the approved service-unit design and execute the checked tasks below. Independent file groups may use `superpowers:dispatching-parallel-agents`; integration and final reviews remain sequential.

**Goal:** Enforce the service unit as an immutable storage boundary across the seven-table MySQL authority and each service's existing NAS.

**Architecture:** Keep global integer IDs and globally unique share tokens. Add service as the first SQL scope dimension, bind each adapter to one validated service, and version all external scope documents rather than inferring old ownership. Runtime reads the deployment environment once; offline jobs use explicit verified ownership and per-service NAS bindings.

**Tech Stack:** Python dataclasses, MySQL/InnoDB, immutable NAS bundles, pytest and project pre-commit hooks.

---

## 1. Shared contracts and test-first foundation

- [x] Add identity regression tests, run them red, then implement explicit service identity in `src/swe/app/conversation_storage/identity.py`.

```python
scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="LQ13.03@a_prd")
assert scope.sql_values == (b"LQ13.03@a_prd", b"tenant", b"source", b"agent")
assert WorkspaceScope.from_values(scope.values) == scope
assert WorkspaceScope.from_row(dict(zip(SCOPE_COLUMNS, scope.sql_values))) == scope
```

`service_unit_id` is a required keyword-only field with no fallback. `service_unit_bytes(value)` validates the exact nonempty UTF-8 value, rejects whitespace boundaries/control characters and lengths over 256 bytes. `service_unit_from_env()` reads `CMB_CAAS_SERVICEUNITID` explicitly. `SCOPE_COLUMNS` is `("service_unit_key", "tenant_key", "source_key", "agent_key")`; `values`, `sql_values`, and `fields` share this order. `from_values` requires exactly four values, and `from_row` requires all four columns. Do not silently accept old triples.

Run: `../../venv/bin/python -m pytest tests/unit/app/conversation_storage/test_identity.py -q`.

- [x] Synchronize `schema_contract.py` with the already committed target SQL, including all six service columns and changed indexes. Bind `PublicationStore(database, bundles, *, service_unit_id)` and `ExecutionStore(database, *, service_unit_id, ...)` explicitly.

## 2. Bound runtime, shares and readiness

Files: `runtime.py`, `factory.py`, `schema_contract.py`, `shares.py`, `src/swe/app/chat_sharing/models.py`, `src/swe/app/chat_sharing/store.py`; corresponding unit and MySQL tests.

- [x] Test missing/invalid service, manifest mismatch, v1 manifest rejection and immutable runtime service.
- [x] Runtime manifest v2 has top-level `service_unit_id` plus existing NAS and workspace list; each workspace `scope` is the full four-value list. Compare all service values before adapter creation.
- [x] Extend `ChatShareRecord` with optional service metadata for legacy compatibility; service-aware `ChatShareStore` operations require and filter a validated bound service, and verify the final NOT NULL schema. Legacy-mode callers keep their old behavior.
- [x] Reject wrong-service share reads before pins, NAS IO or access-count writes. Do not expose service ownership in anonymous responses.

## 3. Normal reads, writes, publication and retention

Files: `execution.py`, `publications.py`, `publication_associations.py`, `message_installs.py`, `chat_mutations.py`, `chat_repository.py`, `session_adapter.py`, `history.py`, `archive_reader.py`, `archive_runtime.py`, `projection_references.py`, `projection_publication.py`, `retention.py`, `source_messages.py`, `runner_integration.py` and their tests.

- [x] First add cross-service regression tests using identical business IDs, operation/path and distinct bodies.
- [x] Populate service on every inserted entity; constrain external/operation-key queries by service and validate service for all internally referenced rows before IO or writes.
- [x] Keep parent integer IDs global, original same-service Chat rebind semantics, Session execution locking and exact authorization.
- [x] Bind stores to service, reject foreign requests/authorities, include service in cursor/cache namespaces, and retain batched pins/dependencies with cancellation-safe cleanup.

## 4. Backup, restore, GC and ended-owner recovery

Files: `backup.py`, `backup_set.py`, `gc.py`, `recovery.py`, `recovery_cli.py`; backup/GC/recovery tests.

- [x] Upgrade backup/evidence documents to v2 and include full service scope in validation, guards and scans.
- [x] Retain same-service multi-scope restore. For complete multi-service restore accept an explicit mapping of service to isolated BundleStore, reject mixed-service input with a single root, validate the whole set before mutation, and install metadata in one transaction.
- [x] Deduplicate legacy shares only within `(service_unit_id, tenant)` and retain global primary-key/token collision rejection.
- [x] Reject cross-service recovery proofs, candidates and publications before unlink or mutation; preserve all original physical-GC enablement and stop/IO-drain requirements.

## 5. Offline migration and CLI

Files: `migration/inventory.py`, `manifest.py`, `importer.py`, `install_records.py`, `install.py`, `verify.py`, `exporter.py`, `readonly.py`, `src/swe/cli/conversation_storage_cmd.py`, `scripts/rehearse_conversation_storage.py`; associated tests.

- [x] Upgrade inventory/manifest/binding/report versions, require explicit per-source service ownership, and use one coordinated allocation space for all services.
- [x] Support per-service NAS stores in one shared staging schema; never infer service from tenant/path/current machine environment.
- [x] Backfill share ownership from inventory, run the committed finalize SQL after validated import, and only then check/install the complete seven-table set.
- [x] Export and verify each service with its own NAS root, reject unmapped services and preserve original rollback data.

## 6. Integration, review and delivery

- [x] Run `../../venv/bin/python -m pytest tests/unit/app/conversation_storage tests/unit/cli/test_conversation_storage_cmd.py tests/unit/scripts/test_rehearse_conversation_storage.py -q`.
- [ ] Use only generated isolated MySQL schemas for the two-service runtime/NAS test and representative execution/share/backup/migration/GC tests. Record exact commands and XML counts, remove generated schemas, and never use business NAS as scratch space.
- [ ] Run pre-commit on changed files, inspect the full SQL access diff, and request independent spec-compliance and code-quality reviews. Fix confirmed issues and repeat relevant checks.
- [ ] Commit only task-owned changes, update the design with actual implementation/evidence limits, preserve pre-existing user changes, and report any outstanding work accurately.
