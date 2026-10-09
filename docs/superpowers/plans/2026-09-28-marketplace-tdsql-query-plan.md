# Marketplace TDSQL Query Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move application-market skill and MCP list/detail metadata queries, browse facets, and branch counts from `index.json` to TDSQL while retaining NAS for content files.

**Architecture:** Add database-backed market metadata registries. `MarketplaceService` consumes registries for market listing/detail paths, while NAS remains responsible for skill files and MCP configuration payloads. Existing API response models and Console calls remain unchanged.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic, aiomysql, pytest, pytest-asyncio, MySQL/TDSQL JSON functions.

---

### Task 1: Add database schemas and row mapping tests

**Files:**
- Create: `scripts/sql/marketplace_tdsql_query_migration.sql`
- Modify: `market/src/market/marketplace/models.py`
- Test: `market/tests/unit/marketplace/test_market_skill_registry.py`
- Test: `market/tests/unit/marketplace/test_market_mcp_registry.py`

- [x] Write failing tests for mapping TDSQL rows into `MarketItem` and for MCP market registry reads.
- [x] Run the focused tests and confirm they fail because the new registry/query methods do not exist.
- [x] Add the incremental schema migration and new `MarketMCPRegistry`.
- [x] Extend `MarketSkillRegistry` with list/detail queries and shared row conversion.
- [x] Run focused registry tests.

### Task 2: Move service list/detail queries to TDSQL

**Files:**
- Modify: `market/src/market/marketplace/service.py`
- Test: `market/tests/unit/marketplace/test_service.py`
- Test: `market/tests/unit/marketplace/test_skills_browse.py`
- Test: `market/tests/unit/marketplace/test_mcp_browse.py`

- [x] Add failing tests proving skill and MCP list/detail paths consume registry rows even when `index.json` is absent.
- [x] Run the focused tests and confirm the existing file-backed implementation fails them.
- [x] Inject/use `MarketSkillRegistry` and `MarketMCPRegistry` for list/detail reads.
- [x] Keep NAS reads only for skill content and MCP configuration masking.
- [x] Run service and browse tests.

### Task 3: Move browse facets and branch counts to the same metadata source

**Files:**
- Modify: `market/src/market/app/routers/market_browse.py`
- Modify: `market/src/market/marketplace/service.py`
- Test: `market/tests/unit/marketplace/test_market_browse.py`

- [x] Add a failing test showing `/market/browse` works from TDSQL rows without `index.json`.
- [x] Replace the direct `load_index()` call with the service/registry metadata set.
- [x] Make `list_all_bbk_ids()` use the same source.
- [x] Run browse and branch-count tests.

### Task 4: Keep metadata synchronized on market writes

**Files:**
- Modify: `market/src/market/marketplace/market_skill_registry.py`
- Modify: `market/src/market/marketplace/mcp_registry.py`
- Modify: `market/src/market/marketplace/service.py`
- Modify: `market/src/market/app/routers/skills_market.py`
- Modify: `market/src/market/app/routers/mcp_market.py`
- Test: `market/tests/unit/marketplace/test_skills_market.py`
- Test: `market/tests/unit/market/test_mcp_versions_api.py`

- [x] Add failing tests for publishing/updating metadata and soft deletion.
- [x] Make skill and MCP publish/edit/delete paths upsert or soft-delete TDSQL metadata.
- [x] Preserve `index.json` writes for compatibility.
- [x] Run write-path tests.

### Task 5: Add migration and consistency verification

**Files:**
- Create: `market/src/market/marketplace/metadata_migration.py`
- Test: `market/tests/unit/marketplace/test_metadata_migration.py`

- [x] Add failing tests for idempotent skill/MCP index migration and missing-content reporting.
- [x] Implement dry-run and idempotent migration helpers.
- [x] Add consistency comparison for item metadata.
- [x] Run migration tests.

### Task 6: Full verification

- [x] Run `PYTHONPATH=src:market/src .venv/bin/python -m pytest market/tests/unit`.
- [x] Run `git diff --check`.
- [x] Review the final diff for unrelated changes.
- [x] Attempt GitNexus discovery; the MCP tool is unavailable in this environment.
