# Conversation migration completion audit

2026-11-01, Asia/Shanghai. Audit of feature HEAD `0ff2f5ee8`, including the final
committed validation delta. This report distinguishes implementation, local
proof, and target-environment acceptance. HTTP, semantic-runtime, and ended-owner
recovery reviews are complete. A complete integrated MySQL run found two stale
test contracts; a focused real-SQL rerun is recorded in the test report. No push,
production DDL, cluster deployment, or business-data migration has been run.

The scope remains the complete backend and historic-data migration in the
[15-task implementation plan](../plans/2026-10-30-conversation-mysql-nas-implementation.md).
The plan contains 78 checkbox steps (despite its earlier audit label saying 79);
the table below reconciles every checkbox step.
Existing workspaces and the original NAS are reused. One continuous outage,
all production scopes, no permission to lose new writes, and complete reverse
export remain binding. Local implementation and local tests do not close target
MySQL/NAS, two-Pod, fixed-old-image, or full-scale gates.

## Evidence and remaining gates

- T1–T13 and report-only T15 have implementation commits through `0ff2f5ee8`.
  T14 tools are committed in `a1fc353b9`; the progress document now records those
  commits instead of the obsolete uncommitted status.
- T8.5 has production wiring: `SemanticRuntimePublisher` is installed by
  async `IndexedQuery.bind_runtime`, including the inherited ToolGuard dispatch
  path, complete recovery roots, bounded streaming publication, and producer
  cancellation/drain. Focused units prove sequential advancement stops and
  already-started parallel peers drain after unknown publication. Six final
  real SQL/NAS cases pass, including the post-merge failure-hook fence.
- Unknown/prepared admission, terminal, and semantic-root settlement use the
  original operation and complete confirmed root without replay. A separate CLI
  reconstructs owner/generation from durable rows after process restart and
  requires auditable stopped-owner/IO-drain evidence; lease expiry is insufficient.
- The HTTP/MySQL §10.1 matrix is implemented and reviewed. The 57-case full run
  and two-case final delta cover 58 unique cases (one overlaps). Helpers exercise
  actual ASGI/SQL routes; production auth/bootstrap and live TCP CLI remain
  outside this harness.
- T10 direct-entry inventory is committed in `58ee8c62b`, with authority branches
  for legacy and SQL paths. That audit found and fixed NAS bundle deletion through
  generic file governance and SQL-authority bypass through Shell backup/restore.
  Real direct/alias bundle protection and no-IO backup guards passed 97 tests.
- The two stale fixture/return-contract tests exposed by the final integrated
  run are corrected in `0ff2f5ee8`; both files pass 12 real SQL cases. Exact
  delta hooks, the 1,238-case cross regression, and the 78-step evidence ledger
  are recorded in the linked reports. Original failures remain recorded.

Final evidence: cross-regression 1,238/0 failures; full real-MySQL directory
268 passed/2 failed followed by 12/0 focused passing cases after correcting two
stale test assumptions; semantic post-fence SQL 6/0; API full 57/0 plus delta
2/0 with one overlap. The final full SQL run includes all final API cases, but
the directory was not rerun as one all-green 270-case invocation. Exact
commands/results and baseline static-check limitations are in the
[test report](2026-11-01-conversation-test-report.md). None of these runs
substitutes for the required target NAS, old-image, or production-scale gates.

## All 78 plan checkbox steps

`Local` means implementation and relevant local evidence exist, not that every
production acceptance condition is met. `Open` marks a specific unfinished local
proof/implementation. `Gate` marks an unavailable target/rehearsal requirement.
`Record` marks a missing or stale deliverable. Mixed labels preserve both facts.
Step numbers are the checkbox order within each original task.

| Step | Requirement | Evidence / remaining work |
| --- | --- | --- |
| T1.1 | Shared old/new contract fixtures | Local: legacy contracts/readers and final SQL/HTTP matrix |
| T1.2 | Complete identity and difficult old data | Local: identity, history/share, target Tracing/overlay; Gate: all production scopes |
| T1.3 | Explicit safe isolated DB fixture | Local: fixture-safety tests, generated empty schema, no business-schema cleanup |
| T1.4 | Fresh old contract baseline | Local: recorded 79 baseline; final regression must retain command/result provenance |
| T2.1 | Exact UTF-8 identities | Local: identity tests/immutable identity objects |
| T2.2 | SQL-aligned byte limits and absence | Local: identity/schema tests |
| T2.3 | Bundle bounds/version/path/truncation | Local: bundle and fault tests |
| T2.4 | Versioned block table and byte extents | Local: bundle plus projection/archive tests |
| T2.5 | Durable exclusive NAS publication | Local: fsync/exclusive faults; Gate: original NAS/two-node visibility |
| T2.6 | Deterministic behavior verification | Local: bundle tests; target NAS faults remain separate |
| T3.1 | Real MySQL uniqueness/nullability | Local: schema tests |
| T3.2 | Exact six tables/151 columns/charset | Local: schema + schema-drift acceptance; Gate: target MySQL/TDSQL |
| T3.3 | Preserve legacy share schema/counters | Local: extension/install/share tests; Gate: actual source share snapshot |
| T3.4 | Representative EXPLAIN and admin cost | Local: 30 Sessions/1500 Chats/900 Turns/2000 publications/6000 Messages; Gate: target plans/performance |
| T3.5 | Same-connection transaction/unknown | Local: share/publication transaction and lost-response tests |
| T4.1 | Reservation/commit/late-writer fault matrix | Local component tests and final SQL/NAS runtime; Gate: target late Pod writer |
| T4.2 | Durable prepared install plan | Local: commit-plan/publication tests and independent settlement review |
| T4.3 | NAS-first atomic metadata install | Local: execution/archive/publication and new fsync tests |
| T4.4 | Same-key reconciliation and fencing | Local: same-key settlement, restart recovery, exact owner/generation audit |
| T4.5 | Global lock order | Local: publication/retention/GC races and semantic mutation-lock tests |
| T5.1 | Guard race/legacy scope/pin races | Local: retention and GC integration tests |
| T5.2 | Guard marker and live locator pins | Local: retention/schema tests |
| T5.3 | Batched page pins/recheck/finally | Local: history deletion races and reader tests |
| T5.4 | Dependency closure and owner-death reaping | Local: retention/archive/backup/GC tests; Gate: actual crashed Pod evidence |
| T6.1 | CRUD/meta/CAS/ties/retry contract | Local: repository and final HTTP SQL matrix |
| T6.2 | Direct SQL convenience methods | Local: SQL repository and unit contracts |
| T6.3 | Revisions/rebind/busy/no resurrection | Local: repository races and binding tests |
| T6.4 | Tombstone only/share/session retention | Local: repository/share/GC-backup-export tests |
| T6.5 | Both cursors/full array/SQL filters | Local: repository and HTTP SQL matrix; selected logical Session ordering covered |
| T7.1 | Same-API SQL historical contract matrix | Local: API SQL matrix plus explicit 58 unique cases across full run/delta |
| T7.2 | Bounded Message/archive/old Turn reads | Local: history/point/cursor tests |
| T7.3 | Snapshot/pins/fragments/numeric context | Local: history/projection and API SQL assertions |
| T7.4 | Target Tracing and approval overlay | Local: API delta asserts target scope, permissions and overlay |
| T8.1 | Admission/Stop/death/settling matrix | Local: execution races and unknown/restart SQL evidence; Gate: real Pod death |
| T8.2 | Durable admission before Agent work | Local: execution/Runner tests and unknown admission/no model call cases |
| T8.3 | Short SQL locks and lease authority | Local: execution/Runner lease tests; Gate: actual Pod crash/late writer |
| T8.4 | Complete recovery/save/load/modify/Cron | Local: projection/complete-root/export and API entry tests |
| T8.5 | Semantic boundaries and bounded text | Local: production publisher, byte/event/time SQL tests, failure-hook fence; Gate: batch tuning |
| T8.6 | Terminal/Stop atomicity/pending/final frame | Local: terminal-frame, Stop, restore and restart SQL evidence |
| T9.1 | Shared current/Chat-only archive | Local: history/archive runtime tests |
| T9.2 | Atomic compact/checkpoint/event/root | Local: archive integration and rollback tests |
| T9.3 | Event contiguity/checkpoint CAS/epochs | Local: archive validation/late-authority tests |
| T9.4 | Atomic /new and /clear retention | Local: archive reset plus real command path/API SQL mapping |
| T9.5 | Evidence/attachment injection/dependency | Local: archive binding/readers, projection reuse, committed direct-entry inventory |
| T10.1 | Existing workspace/W+/Cron/no fallback | Local: factory/Runner and W+/Cron SQL entry matrix; reuse original workspace/NAS |
| T10.2 | Trusted scope/same DB/original NAS | Local: runtime manifest/readiness/factory cases |
| T10.3 | Share locator and atomic selected QA | Local: share integration/lost-commit/selection-race tests |
| T10.4 | Permanent token/503/no blind unlink | Local: deleted Chat/orphan/missing body/count failure tests |
| T10.5 | Readiness only/no startup DDL | Local: exact schema/index/engine/charset drift tests |
| T10.6 | Every direct production entry accounted | Local: committed branch-by-branch inventory and governance/Shell backup bypass regressions in 58ee8c62b |
| T11.1 | All historical formats and corruption | Local: multi-format roundtrip fixture parametrization/unit rejection tests |
| T11.2 | Explicit identity inventory/controlled paths | Local: inventory + manifest tests |
| T11.3 | Stable manifest IDs/operations/reentry | Local: import reentry and manifest tests |
| T11.4 | Full old reader/root/work-copy recovery | Local: legacy readers and roundtrip/unknown module tests |
| T11.5 | Deterministic batch occurrence/order | Local: roundtrip repeated import comparisons |
| T11.6 | All share fields and orphan closure | Local: orphan share and roundtrip tests; Gate: complete production share inventory |
| T11.7 | Full semantic verify/machine blockers | Local: fixture roundtrips and readonly blockers; Gate: all production scopes/data |
| T12.1 | New writes → fixed old image → continue | Local: current-tree old reader/writer tests; Gate: fixed production old image/digest |
| T12.2 | Complete reverse snapshot/share copy | Local: exporter and new v3-ledger/GC tombstone regressions |
| T12.3 | Scope fence/closure/pins/verified restore | Local: backup/restore + prepared convergence tests; Gate: full-scale target restore |
| T12.4 | Workspace + orphan share/failed protection | Local: orphan inclusion and fence/pin tests |
| T12.5 | Empty-target restore/legacy command refusal | Local: backup/CLI boundary tests |
| T13.1 | Offline CLI/preview/apply/manifest | Local: seven commands committed in b93ebbf4e |
| T13.2 | Fail-fast controls and zero-write verify | Local: CLI/install/readonly tests; full integration run exercised installer suite |
| T13.3 | Same-instance/schema/privileges/MDL | Local: installer checks and simulated failures; Gate: target permission/MDL probe |
| T13.4 | Atomic seven-table RENAME/receipt | Local: actual isolated rename and reversal; Gate: target engine atomic capability |
| T13.5 | Continuous-outage audited runbook | Local runbook; Record/Gate: completed operator/time/exit/report ledger from rehearsal |
| T13.6 | Every outage failure branch rehearsed | Local component/fault tests; Gate: actual stage rehearsal including after-new-write rollback |
| T14.1 | Two different-node Pods/NAS evidence | Gate: namespace, empty equivalent RWX PVC, fixed verifier digest; no Job run |
| T14.2 | Full real adapter/environment faults | Local: real MySQL/NAS publication/restore/failure cases; Gate: target NAS/Pod failures |
| T14.3 | Complete §10 same-adapter matrix | Local: HTTP evidence report maps rows/limits; Gate: full auth/bootstrap and target NAS |
| T14.4 | Console compatibility | Local: 8 files/93 passed; exact command and limits in test report |
| T14.5 | Full-scale outage/performance/rollback budget | Gate: production-scale frozen copy, complete stage timings/RSS/NAS/locks/p95/p99/rollback |
| T14.6 | Final checks and baseline separation | Local: final exact changed-file hooks and 1,238 cross regressions pass; all-files hooks fail (console `tsc` missing; 85 mypy errors exactly match main baseline after fixing two W+ scope typing errors; formatting hooks rewrote 199 unrelated files, restored); first sandbox cross run hit process/secret-dir permissions, escalated final run passed |
| T15.1 | Report-only and every protection | Local: 29-case GC integration set; default physical deletion remains disabled |
| T15.2 | Current-read mark/guard/fence races | Local: GC concurrent pin/fence tests |
| T15.3 | Safe exact unlink/retry/tombstone/dependency | Local: cancellation/unlink/late-file/dependency tests |
| T15.4 | Quarantine/reaper/no age-based data loss | Local: dangling scope/unknown reference tests; physical enablement requires target gates |

## Required environment outputs

1. Target database: generated isolated schema; version/capability evidence;
   full integrated test XML, EXPLAIN, permission/MDL results, atomic probe,
   seven-table before/after/reversal observations. Local MySQL 8.0.46 is useful
   evidence, not the target TDSQL/production configuration.
2. Original/equivalent NAS: two required different-node Pods, immutable verifier
   digest and actual imageIDs, same writable PVC/PV/StorageClass/mountOptions,
   mountinfo, operation-key race logs and both exit codes, collected JSON.
3. Fixed old image: actual old digest, complete post-new-write reverse export,
   old process reading and continuing to write, including Stop/compact/delete,
   binding changes, new shares/counters, v3 ledger and unknown recovery modules.
4. Full-scale isolated copy: final source boundary and scope coverage; import and
   full verify reports; complete DB/NAS backup restore; all outage failure
   branches; all nine stage timings and rollback timing, measured RSS, NAS
   bytes/files, DB lock waits, p95/p99. Current null metric placeholders stay
   unmeasured; they are not zero or an outage budget.

## Integration with main worktree

Main worktree remains `perf/event-loop-optimization` at `5c8e96e5c`. Feature
merged that main commit in `201deb168`; README and Runner merged automatically.
The historical code baseline was `494171e55`; the actual pre-merge base was
`dc3332d2e1a337eca9028654f13744958bb94ef2`. No feature merge back into the user's
main worktree and no push have occurred.

The merged code preserves nickname starts/get/cancel/grouped waits and durable
reservations, catalog-only selected experts, and general approval replay. It
does not restore forced selected-expert wait/natural-end cancellation. SQL
admission, durable Stop, terminal-before-final-frame ordering, complete recovery
roots and semantic producer drain coexist with main's changes. Two independent
reviewers inspected those intersections; the final 63-file cross regression
passed 1,238 cases at the final code plus validation delta.

The final review found a storage-error path that could still dispatch configured
PostToolUseFailure external handlers. `4228d9167` fences it before failure-hook
work, retains the original exception/key and restores previous callback only
after producers drain. Twelve red storage-failure cases and six ordinary-error
controls support the fix; the final three-file contract/mypy delta has separate
12-case real SQL and exact-hook evidence.
