# 并行迁移资源与失败矩阵核对

日期：2026-11-12。按并行设计第 6、8、10 节核对当前源码和测试，以下记录测试覆盖，不能替代完整规模验收。

| 要求 | 当前直接证据 | 边界/缺口 |
| --- | --- | --- |
| 有界真实 IO 并发、SQL task 归属 | `test_migration_parallel.py::test_sql_commands_run_in_original_task_with_bounded_workers`、`test_io_has_real_concurrency_and_does_not_copy_sql_context`；真实 MySQL `test_migration_parallel_mysql.py::test_parallel_io_keeps_sql_in_original_named_lock_task` | 不代表所有故障注入均在真实数据库验证 |
| 单 worker 顺序、workspace 私有上下文 | `test_single_worker_preserves_order_and_rejects_unknown_command`；`test_parallel_import.py::test_workspace_io_runs_concurrently_and_contexts_are_private`、`test_parallel_workspace_import_keeps_legacy_stage_order` | 跨服务与重复外部 ID 另由真实生命周期测试覆盖 |
| 内存/磁盘背压、超大单元预检 | `test_migration_spool.py::test_byte_budget_blocks_and_releases_after_cancel`、`test_capacity_failure_does_not_leave_partial_record`、`test_concurrent_writes_reserve_capacity_and_unique_indexes`；`test_inventory_parallel.py::test_workspace_input_limit_fails_before_parser_allocation`、share snapshot/count budget tests | parser 内存为保守预算估算，非操作系统 RSS 硬限制；真实磁盘 ENOSPC 故障尚未注入 |
| 大清单流式加载、磁盘索引预算 | `test_disk_manifest.py::test_manifest_mirrors_round_trip_on_disk`（12k 票据）、`test_header_discards_progress_regardless_of_field_order`（10k）、`test_disk_policy_enforces_shared_budget_and_closes`、oversized value test | 条目数 1x/2x/4x 时 RSS 峰值实测尚缺；单票据/顶层值仍有显式 reader 上限 |
| Journal 分页与增量热路径 | `test_v3_journal.py::test_hydrate_reads_ordered_keyset_pages`、`test_hydrate_checks_mirror_after_all_pages_before_replacing_it`、`test_deferred_updates_keep_sql_authority_without_full_mirror_rewrites` | mock 验证无 ORDER BY 全读、无 persist；规模增长的端到端 SQL 查询量曲线未单独测量 |
| 固定全部 tenant cutoff，resume 保留 | `test_tenant_sync_batch.py::test_all_tenant_cutoffs_persist_once_and_resume_keeps_existing_values`；`test_parallel_import_requires_fixed_tenant_cutoff` | 依赖源全程强冻结，mtime 不覆盖保留时间戳的替换 |
| 取消后真实 NAS 排空、重复取消 | `test_cancel_waits_for_real_io_even_with_repeated_cancel`、`test_close_drains_thread_then_propagates_repeated_cancel`；`test_parallel_import.py::test_cancel_waits_for_running_io_and_clears_contexts` | 真实 NAS 不可中断 IO 永久阻塞不以超时谎报 drained |
| RPC 停止/取消结清 | `test_failure_rejects_waiting_requests_without_deadlock`、`test_coordinator_cancel_settles_inflight_and_pending_commands` | 新增 `test_full_rpc_slots_cancel_settles_acquirers_queued_and_active`：单 worker 下 8 个 RPC，1 active/1 queued/6 acquire waiters，分别注入重复取消与 active command failure，断言全部 caller/Future terminal、队列清空、slots归还 |
| 连接身份/命名锁失效 | `test_migration_lock_guard.py::test_guard_rejects_connection_lock_or_database_identity_change`、`test_idle_io_receives_owner_task_health_checks_and_failure_stops_workers`、CLI owner health wiring test | 目前模拟返回字段/health 异常；真实 MySQL KILL 持锁连接、IO 排空后重启续跑缺失 |
| 未知提交结果保留 operation key | `test_offline_commit_stages.py::test_commit_evidence_preserves_unknown_commit`；confirm/write/verify stages 的 transaction exit failure tests | 证据层模拟 commit 后 ConnectionError/CancelledError；parallel importer 真实提交响应丢失后恢复未完整注入 |
| 错误收尾与私有 store 清理 | `test_parallel_import.py::test_workspace_context_is_closed_after_import_failure`、`test_router_close_attempts_every_store_and_keeps_failed_context` | 清理失败保留上下文供重试；不证明任意进程硬杀后无孤儿正文 |
| 一个失败不能 ready、只读预览 | verify auxiliary failure test、`test_finalize_rejects_missing_runtime_workspace_before_ready`；`test_preview_uses_spool_without_database_or_cutoff`、`test_preview_source_blocker_does_not_rewrite_manifest` | 仍需故意漏任务/覆盖集合不等的全 CLI 回归；最终 ready 由完整 verify 屏障控制 |
| 恢复/跨 scope/万级数据 | `test_parallel_import_mysql.py::test_parallel_multiservice_import_resume_verify_finalize`；10k 四档基准含多服务、分享、空 workspace、长尾 Session、归档、独立 Session | 小规模真实生命周期通过不能代替四档 10k 最终结果；完整性能验收以独立报告记录 |

当前明确剩余故障验收：实际数据库连接被终止、parallel importer 未知提交后重启、真实 ENOSPC、主动遗漏任务阻止 ready，以及万级四档最终性能/资源结果。不能把单元测试矩阵描述成全部失败组合已穷举。
