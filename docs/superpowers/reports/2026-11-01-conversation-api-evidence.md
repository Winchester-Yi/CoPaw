# SQL/NAS API compatibility report

2026-11-01, Asia/Shanghai. Agent `/root/api_finish`; worktree `/Users/shixiangyi/code/Swe/.worktrees/conversation-mysql-nas`; original review baseline `58ee8c62b`; parent committed implementation in `caed335f5` and merged main in `201deb168`. Reuse the existing business workspace and original NAS; temporary test roots/generated schemas do not change that runtime contract.

## Changes

- `chat_sharing/router.py`: trusted Chat.channel Session adapter; before/after root/history/archive snapshot comparison; server-created selection proof.
- `chat_sharing/service.py`: forwards internal proof; no public response field added.
- `conversation_storage/shares.py`: preserve completed legacy questions without typed Turns and archived questions; bind exact selected source occurrence/publication/layer to Session+Chat revisions/highs/epoch; commit-lock recheck completed typed Turn ownership and exact visibility. First duplicate source slice retains legacy ordering; proof cannot upgrade active/unknown typed Turn status.
- `runner/api.py`: six lines preserve SQL projected list status across array/page/cursor; empty local coordinator cannot overwrite running as idle.
- `routers/console.py`: trusted SQL Chat+Session watermark, projected state gate, exact post-payload revision/high/epoch/active Turn/generation recheck. Non-idle or changed state gives 503+Retry-After. Session adapter follows trusted Chat.channel. SQL bypasses legacy admitted-to-failed root mutation. Stored interrupted maps only to external existing failed status; SQL/root stay interrupted.
- `tests/integrated/conversation_storage/test_api_mysql.py`: real HTTP/SQL/NAS matrix and command/W+/Cron/Console helper paths below.

`session_adapter.py` latest-Turn tuple fix is owned/tested by semantic_finish; actual SQL v3 Console cases exercise its caller. Settlement/runtime files remain owned by settlement_finish.

## Commands and counts

Commands below ran in the worktree using project venv. DB wrappers read main repo `src/swe/config/envs/dev.json` without printing credentials; only generated empty `test_conversation_control_<uuid>` and per-test schemas are created and removed. Fixture rejects application schemas and checks empty schema. No business schema DDL/cleanup, deployment, migration, merge or push. Subprocess secrets are redacted.

Canonical complete SQL command:

```text
../../venv/bin/python /private/tmp/run_conversation_api_matrix_final_mysql.py
```

Actual child argv (executable resolves to main project venv):

```text
/Users/shixiangyi/code/Swe/.worktrees/conversation-mysql-nas/../../venv/bin/python -m pytest tests/integrated/conversation_storage/test_api_mysql.py tests/integrated/conversation_storage/test_shares_mysql.py -v --tb=short --junitxml=/private/tmp/conversation-api-matrix-final-report.xml
```

Observed wrapper exit 0: **57 passed in 517.28s**, zero failures/errors/skips; `/private/tmp/conversation-api-matrix-final-report.xml`; generated control schema removed. This run collected 50 API cases and 7 share cases before the final narrow delta. The final source has 51 API cases plus 7 share cases: **58 unique cases have passing evidence across complete run and delta**, not 59, because the Tracing delta case overlaps. There is no single final-source 58-case full-run XML; complete 57 plus focused 2 is the exact evidence boundary.

Final delta: complete run had already collected when final review added the Tracing list request and trusted-channel Console case. Explicit separate evidence:

```text
../../venv/bin/python /private/tmp/run_conversation_api_final_delta_mysql.py
```

Actual child argv:

```text
/Users/shixiangyi/code/Swe/.worktrees/conversation-mysql-nas/../../venv/bin/python -m pytest tests/integrated/conversation_storage/test_api_mysql.py -k 'tracing_target_scope_permissions_and_approval_overlay or terminal_recovery_uses_trusted_chat_channel' -v --tb=short --junitxml=/private/tmp/conversation-api-final-delta-report.xml
```

Observed wrapper exit 0; **2 passed, 49 deselected in 26.90s**, no failures/errors/skips; XML `/private/tmp/conversation-api-final-delta-report.xml`; generated control schema removed. Do not sum with the complete run: the Tracing case overlaps, while the new Console channel case is one additional unique case.

Fresh unit regression:

```text
PYTHONPATH=src ../../venv/bin/python -m pytest tests/unit/routers/test_console_chat_reconnect.py tests/unit/app/chat_sharing tests/unit/app/test_chat_pagination.py tests/unit/app/test_expert_main_binding.py -q --tb=short --junitxml=/private/tmp/conversation-api-unit-all-final.xml
```

Observed exit 0: **86 passed in 3.53s**, no failures/errors/skips. Separate unit cases, not live SQL count.

Final exact-file hooks:

```text
PYLINTHOME=/private/tmp/conversation-api-pylint PYTHONPATH=src ../../venv/bin/python -m pre_commit run --files src/swe/app/chat_sharing/router.py src/swe/app/chat_sharing/service.py src/swe/app/conversation_storage/shares.py src/swe/app/runner/api.py src/swe/app/routers/console.py tests/integrated/conversation_storage/test_api_mysql.py
```

Observed exit 0, process 49124: AST/docstring/encoding/private-key/whitespace/trailing-comma/mypy/black/flake8/pylint passed. Nonmatching file-type checks skipped. `git diff --check` exit 0.

Older overlapping evidence retained, **never added to final counts**:

- `conversation-api-complete-mysql-report.xml`: 47 passed/441.71s, observed wrapper exit 0 and control schema removed; wrapper `/private/tmp/run_conversation_api_complete_mysql.py`.
- `conversation-api-finish-mysql-report.xml`: 44 passed/405.99s.
- `conversation-api-final-mysql-report.xml`: 36 passed/329.64s, passing replacement of prior transient connection snapshot.
- `conversation-api-routing-command-mysql-report.xml`: 3 passed/41.92s.
- `conversation-api-reconnect-all-report.xml`: 10 passed/69.76s.
- `conversation-api-added-mysql-report.xml`: intermediate 7 passed/1 failed from harness Agent middleware overwriting path identity; corrected and superseded. All XML paths are under `/private/tmp`.

## Exact §10.1 row mapping

All names below are exact functions in `test_api_mysql.py` unless stated. Parametrized cases count once per XML case; one function may substantiate multiple matrix assertions without summing twice.

| §10.1 entry | Exact tests | Contract / limits |
| --- | --- | --- |
| GET /chats | `test_http_list_full_array_exact_count_filters_and_page`; `test_http_list_preserves_sql_active_status_without_local_owner` | Full admin array 16 rows (over UI page 10); current user/channel filter; running SQL survives empty process-local owner. |
| page/page_size | Same list tests; `test_http_list_rejects_invalid_pagination` | Exact filtered total14, page2/size10 has4 and has_more=false; incomplete pair/cursor mixing/malformed cursor422. |
| cursor v1/v2 | `test_http_v2_and_legacy_creation_cursors_keep_total_and_advance`; invalid/status tests | Updated v2 vs created v1 ordering, total/page1, next cursor advancement/exhaustion. Same-timestamp UUID and channel-cursor EXPLAIN remain repo/schema-suite scope, not newly measured HTTP evidence. |
| UUID detail | `test_http_detail_shared_layers_duplicate_ids_chat_anchors_and_meta`; archive test | Shared memory/task/error; Chat-only anchor; duplicate source IDs; full task/custom meta; independent archive; wrong user404. |
| Session→Chat | `test_manager_no_user_latest_and_equal_timestamp_registry_order` | Actual ChatManager+SQL repo, omitted user spans users, equal timestamp registry order, newer updated wins. Internal entry rather than HTTP. |
| answer-turn | `test_http_answer_turn_legacy_unknown_explicit_chat_and_stored_owner`; `test_http_cold_turn_preserves_current_view_boundary` | User required400; explicit Chat wins over mismatching Session; non-user404; old unknown status null; typed owner/status respected; fully archived remains outside current view, visible cold works. |
| archive history | `test_http_archive_source_limit_fragment_identity_and_cursor`; cold-turn test | Source limit (two sources produce three fragments), ascending payload, boundary/cursor advancement/exhaustion, other-Chat cursor422. Numeric limit-bound rejection combinations not separately driven here. |
| context-usage | `test_http_context_missing_invalid_future_is_unavailable_without_nas` (3); `test_http_context_numeric_stale_and_sql_failure_are_distinct` | Missing/malformed/future unavailable; numeric snapshot and active-Chat stale; other shared Chat not stale; NAS sentinel proves no NAS read; SQL fault503. Invalid-marker-only stale branch not independently driven. |
| expert-binding | `test_http_expert_binding_live_definition_and_disable_release` | Actual SQL CAS revision, live unavailable expert, disabled feature releases stored binding and advances revision; reenable no resurrection. Definition/config services supplied. |
| Tracing list/detail | `test_http_tracing_target_scope_permissions_and_approval_overlay` | Final delta calls list(user+channel) and detail under operator; target tenant/agent scope, memory+task independent view, no normal-detail errors/anchors/meta, approval sees target tenant/source/alice, wrong target404. Identity resolvers supplied. |
| share-options | `test_http_share_options_selected_turns_permanent_token_and_missing` (hot legacy/cold legacy/cold typed); `test_http_share_uses_chat_channel_identity_not_console_or_meta`; `test_http_share_options_rejects_changed_archive_epoch` | Full archive+current candidates, stored completion/failure, correct channel despite deceptive meta/opposite console state, epoch race unavailable. |
| selected QA share | Above variants; `test_http_duplicate_archive_question_uses_first_legacy_slice_and_source_proof`; `test_http_share_proof_never_overrides_active_or_unknown_turn`; `test_http_share_rejects_revision_change_during_payload_read`; `test_http_share_rechecks_proof_after_durable_body` | Source question IDs, legacy no synthetic Turn, cold typed allowed, failed400, first duplicate slice ordering; active/unknown never upgraded; root/history races during read and after durable bundle abort publication. Share SQL tests also cover status/visibility race. |
| permanent token | Share variants; `test_http_legacy_orphan_token_uses_publication_scope_and_preserves_counts`; all 7 `test_shares_mysql.py` cases | Survives Chat delete, anonymous token read, missing404, counts, orphan publication scope/opaque key; missing body/count failure503, immutable locator, lost commit single token/body. |
| reconnect/terminal | `test_restarted_console_reconnect_does_not_invent_sql_terminal` (5); `test_console_reconnect_reads_committed_v3_terminal_snapshot` (2); `test_console_terminal_snapshot_rechecks_sql_watermark` (3); `test_console_terminal_recovery_uses_trusted_chat_channel` (delta) | Actual empty coordinator vs reserved/admitted/running/settling/stopping =>503+Retry-After without root mutation; actual admission/start/finish yields v3 completed/interrupted SSE+headers; three concurrent-change checks reject; console/cron same logical ID uses trusted adapter, Console target resolver excludes cron. Helpers/SSE exercised, not entire HTTP Console route. |
| CLI / Agent-scoped | `test_cli_list_and_detail_use_real_sql_http_api`; `test_agent_scoped_route_and_header_select_sql_workspace` | Click list/get via sync bridge to actual ASGI routes/SQL/NAS; actual AgentContextMiddleware, scoped router and get_agent_for_request choose second SQL workspace via path/header; cross-agent UUID404. No live TCP CLI. |
| task/Cron recovery | Detail task meta/layer; `test_cron_receipts_recover_from_new_adapter_without_chats_json`; `test_conversation_command_dispatch_resets_sql_epoch_and_complete_root` (2) | New adapter restores complete-root outcome, owner conflict, clear; actual /new,/clear retain Cron/unknown module and reset ReMe memory+SQL epoch. Full managed scheduler/task-run lifecycle not end-to-end here. |
| W+ tool | `test_wplus_event_tool_projects_sql_chat_without_chats_json` | Actual emit tool/W+ store/runtime manifest/outbox/SQL meta projection; JsonChatRepository sentinel/no chats.json. W+ business store stays separate. |

Exact additional share tests in `test_shares_mysql.py`: `test_permanent_share_survives_chat_deletion_and_counts_access`, `test_legacy_orphan_preserves_token_key_counts_and_uses_publication_scope`, `test_lost_share_commit_retains_original_bundle_and_one_token`, `test_missing_body_and_failed_count_are_unavailable_not_empty`, `test_token_cannot_be_repointed_to_another_share_same_scope`, `test_share_commit_rejects_selection_changed_after_snapshot` (status/visibility).

The /new and /clear cases invoke real run_command_path/CommandHandler/ReMe/SQL Session/archive and preserve future_module and top-level managed Cron receipts. No new business workspace bootstrap occurs. §10.6 introduces no full-text conversation search API; no invented search endpoint is claimed. Skills/MCP/file context references, monitor trace search/stats, Goals/SubAgents, attachments/files, push/suggestions/qa, Cron definition/auth, W+ business storage remain independent domains per §10.4 and parent direct-entry inventory.

## Harness boundaries

Actual production code: FastAPI routers with HTTPX ASGI transport; ChatManager; SQL repository/history/archive/context/share; PublicationStore/BundleStore; actual Click parsing, command dispatch, W+ tool/outbox, Cron receipts, AnswerTurnCoordinator and Console helpers.

Supplied components: request identity/source/feature flags and default workspace dependency; simple multi-agent manager/config definitions; expert definition, target tenant resolver, approval backend. These do not prove production auth/tenant bootstrap/remote identity. Dedicated Agent test removes default dependencies and runs actual AgentContextMiddleware/get_agent_for_request. CLI uses bridge into actual ASGI rather than network. Console helper tests do not cover entire /console/chat auth/stream routing.

NAS bodies use local temporary filesystem BundleStore: real immutable bodies/pins/extents and SQL semantics on this host, not original network NAS durability/cache or two-node visibility. No provider/model execution needed to read terminal truth: committed v3 state is created with actual execution API. Table explicitly lists untested variants instead of claiming all combinations.

## Independent review

- settlement_finish reviewed final share locked proof/ownership/source occurrence and Console trusted SQL before/after snapshot. Final disposition: no confirmed spec/quality blocker; concurrent admission rejected and interrupted remains stored unchanged. Optional missing-corrupt-Session error handling nonblocking due formal association.
- I reviewed parent-owned archive_maintenance/dream_logs protection of bundles. First review found symlink alias bypass; parent reproduced four red assertions and fixed scanner symlink skipping plus resolved-target KEEP protection. Final review: no concrete blocker for direct/static alias paths. Parent reports 97 combined boundary regressions and hooks; not counted in API evidence.
- I reviewed parent-owned Shell backup executor/worker and direct compress/decompress scripts: reject SQL/invalid authority before mutation/inventory/download, dedicated RestoreTargetRejected prevents refusal-triggered rollback. Final disposition: narrow, correct, no confirmed blocker; parent committed boundary fixes as 58ee8c62b.

## External gates remain

Target MySQL/TDSQL privileges/MDL/atomic-rename acceptance; original NAS/two different-node Kubernetes faults; fixed old production image reverse-write continuation; all production scopes/frozen full-data import/verify; full backup/restore; measured scale/outage/RSS/NAS/locks/p95/p99 remain open external gates. Null metrics are not zeros. Production login/tenant middleware, live TCP CLI, browser/cross-worker live attach and full scheduler/provider/independent-domain paths are not acceptance claims here.

Parent reviewed and committed the six owned files. The later complete 270-case MySQL directory run passed all 58 API/share cases; two archive/execution fixture assumptions failed, then were corrected and rerun separately. See the final test report for that boundary.

## Frozen owned-file SHA-256

```text
047d482ad0ac25adfc0189d65ef2ddafedfa3ab957eaebaf6ce64d4abd235b1f  src/swe/app/chat_sharing/router.py
0e563705a4c0b187c399fb532d16a1ebec3de3c2e9bce3f130d3bf6a7c43ef56  src/swe/app/chat_sharing/service.py
bb2357ac31676fc9c839bae89c371e7b1ab907be9e375b81fe49a5014c84cd8b  src/swe/app/conversation_storage/shares.py
bca34189f8a5ca5bd78e5ad052abbeb562795df3c535de8d7cac39ffbe5e56fb  src/swe/app/runner/api.py
fa4d769e4c9e94adaa0e78b1f8a0a8c7ae6c87f6c40d888f2d0755d155ec2db7  src/swe/app/routers/console.py
ee8fa8611b9194dffeaa2929f3968435bd0157488f37c3716c2ff7d9f4d59c8e  tests/integrated/conversation_storage/test_api_mysql.py
```
