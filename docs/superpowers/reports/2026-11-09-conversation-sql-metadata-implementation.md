# Conversation SQL Metadata Implementation

Date: 2026-11-09. Status: implementation slice; no database DDL applied.

## Scope

- Chat create, draft creation, and ordinary light-metadata updates can commit in a short SQL transaction. They do not reserve a Publication or access NAS. Content-bearing Chat metadata continues through the existing immutable bundle publication path.
- Existing `metadata_text` already contains only the same nine fields returned in summary display metadata. The implementation reuses it with a 40 KiB serialized-byte limit; it does not add a duplicate display column.
- Chat stores `external_session_key`, copied under the Session lock. Summary pagination filters and returns this field directly from Chat, so it now uses one scoped Chat query with no Session/Turn joins, NAS hydration, pins, or count query.
- Chat `status` is maintained alongside Turn reservation, Stop, rejection, and terminal installation. Session/Turn state remains the execution authority. New status-only writes do not change recent ordering; terminal writes retain their previous recency update.
- Migration inserts copy the Chat external Session key. Full import verification compares the denormalized Session key, SQL metadata projection and idle status with the source.
- The DML migration facade now exposes only its controlled lease operations, packet budget and validated fixed prepared inserts, fixing the existing `acquire` gap without opening DDL execution.

## Compatibility

Existing routes and complete Chat responses remain. Unknown/content Chat metadata still uses a NAS bundle. Existing files are not modified or deleted. The current CREATE DDL and readiness contract now require the extra `external_session_key` column. Existing deployed tables need a separately reviewed stopped-writer backfill and schema transition; the updated CREATE file is not that procedure.

## Verification

- Conversation storage unit directory: 1,013 passed.
- Isolated real-MySQL schema and projection tests: 9 skipped because no dedicated test schema was configured.
- Ruff and diff whitespace checks pass for the changed implementation and test files.
- Isolated real-MySQL tests cover SQL-only mutations, status parity, and recency, but remain unverified against a configured dedicated test schema.
- No production or shared database was modified.

## Remaining

The central user request is not complete yet: implement immutable legacy source registration/resolution so the v3 migration imports SQL metadata and locators without creating historical content bundles, then update backup/restore, GC, schema readiness and cutover verification. Validate old NAS formats and payload compatibility on real source samples. Design the stopped-writer migration from the inspected existing schema. Rerun full real-MySQL acceptance after its known DML/restore blockers are resolved.
