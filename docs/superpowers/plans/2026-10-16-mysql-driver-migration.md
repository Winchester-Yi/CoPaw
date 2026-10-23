# Core MySQL driver migration implementation plan

> Execute with the executing-plans / subagent-driven-development workflow. User approved implementation and shared CRUD encapsulation on 2026-10-16.

**Goal:** Core `src/swe` uses official asynchronous Connector/Python, with bounded connection ownership and a small SQL facade; Tracing writes binary prepared multi-row chunks. Independent services and schema stay unchanged.

**Architecture:** `DatabaseConnection` manages a bounded pool and task-owned sessions. A session exposes `execute`, `execute_result`, `insert`, `execute_many`, `fetch_one`, `fetch_all`; acquire and transaction yield this facade. `ExecutionResult` carries affected rows and lastrowid. Existing database-level calls reuse the owning transaction. SQL remains parameterized and owned by each Store; no ORM or dynamic identifier builder.

## 1. Shared operations, pool and transaction contracts

- Add tests before implementation for write results, dictionary reads, reuse, nested transactions, named-lock plus transaction, cross-database/task isolation, pool saturation/cancellation and bounded cleanup.
- Replace aiomysql with optional `mysql.connector.aio.MySQLConnection`; retain public aliases and no-database mode.
- Implement finite acquisition, handshake, I/O and cleanup budgets. Retain unresolved cleanup tasks and capacity until actual completion; retrieve all task exceptions.
- Validate pool configuration and expose timeouts without changing existing credential loading.
- Verify with `PYTHONPATH=src ../../venv/bin/python -m pytest tests/unit/database -q`.

## 2. Migrate business Stores to the facade

- Replace raw cursors/commit/rollback in skill readiness, featured cases, workflow config, goals, skill results, upload records and feedback.
- Use `insert` for lastrowid, `transaction` for atomic writes and `fetch_one/all` for locks/reads. Keep SQL and business decisions.
- Adapt existing fakes to model the shared API; test rollback/locking and ID contracts.
- Run related Store/router suites and scan core for remaining raw driver use.

## 3. Prepared multi-row writes

- Add trusted immutable insert template and bounded chunk iteration, respecting row/byte/65535-parameter/server packet limits.
- Cache at most four prepared cursors per physical session, preserving SQL object identity; close evicted handles, discard uncertain sessions.
- Test command counts with official aio prepared cursor/protocol fake, payload round-trip encoding, reuse/eviction, chunk failure rollback and cancellation.
- Change Span serialization to an iterator and use the prepared API in one transaction; preserve JSON, all 23 fields and idempotent confirmation counts.
- Run full Tracing and database suites, including existing shutdown regression tests.

## 4. Dependencies, documentation and final verification

- Pin `mysql-connector-python==26.7.0` in `[mysql]`, update both core Dockerfiles.
- Provide CRUD/transaction usage and an opt-in isolated-database integration smoke test. Do not write to production for verification.
- Update design/playbook with actual scope and remaining external acceptance (TDSQL, image/Python matrix, A/B performance).
- Run relevant pytest, formatting and diff checks; GitNexus detect_changes before commit; review resulting scope.

**External acceptance:** Real TDSQL authentication/proxy/prepared/GET_LOCK/packet behavior, image builds, Python 3.10–3.13 and repeated A/B measurements require isolated infrastructure. Unit/protocol tests cannot establish production compatibility or performance improvement.


## Local delivery record (2026-10-16)

- Tasks 1–4 implementation and local regression are complete. Independent review findings (late handshake socket, repeated cancellation, swallowed prepared close errors and post-close rollback) were fixed with regression coverage.
- Candidate driver 26.7.0 tested from an isolated `/private/tmp/swe-mysql267` install using the project virtual environment; the main virtual environment was not replaced.
- Expanded suite: **581 passed, 1 skipped** (13.96s). The skipped test requires explicit isolated MySQL/TDSQL settings.
- All checks on changed files passed: AST/TOML, encoding/private-key/whitespace, trailing commas, mypy, black, flake8, pylint. GitNexus detect_changes reported medium impact, within shared database/Store/Tracing scope; source review supplements its stale symbol positions/new-file limitations.
- No production database writes, deployment, container build, Python version matrix or A/B performance results are claimed. These remain release acceptance work.
