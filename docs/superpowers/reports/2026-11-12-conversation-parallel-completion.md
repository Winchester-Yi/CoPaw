# 会话并行迁移实施收尾与剩余门禁

日期：2026-11-12。代码核对基线：`abe53af76b2ad008f91ed63c1d6edc81df8b6dd2`，包含已快进合入的 `160f4bca8`、`23bee5b7d`。本报告只新增文档，未运行数据库测试，也未修改生产实现。

**结论：并行实现与小规模逻辑证据已具备；原设计整体目标尚未完成，不作生产放行。** 固定源码版本360项逻辑suite已全部获得终态记录：**359项至少一次通过、1项跳过**。执行分散于多个进程；三个用例曾出现数据库操作超时/HTTP 503，随后单项复跑通过，未得到一轮无失败的全量结果，根因仍未定。此前1408项单元/CLI、98项续跑及4+2+1项专项通过均按各自版本和执行范围保留，不能合称当前HEAD一次性全量通过。用户已要求停止万级基线，本轮不得恢复该测试。

## 核对口径

下表按 [原设计第11节的六项实施顺序](../../../openspec/changes/parallel-conversation-migration/design.md) 对照。实现判断来自本轮直接阅读当前源码及测试断言；历史执行数字来自已保存的验收记录，不以“存在测试函数”代替实际执行通过。下文代码路径以 `src/swe/app/conversation_storage/migration/` 为基准，测试路径分别以 `tests/unit/app/conversation_storage/` 和 `tests/integrated/conversation_storage/` 为基准。

| 原始要求 | 当前文件/类与直接代码证据 | 对应测试与证据边界 | 状态 |
| --- | --- | --- | --- |
| 1. 固化串行语义、阶段耗时，审计同步NAS与全量重写 | `progress.py` 提供阶段/SQL/IO计数；`scripts/benchmark_conversation_inventory.py`、`benchmark_conversation_semantic.py`、`benchmark_conversation_manifest.py` 及 `test_migration_10k_mysql.py` 提供观测入口。直接核对仍存在同步 `DiskMap`/SQLite访问、spool回读、manifest写出和源控制面扫描；不能声称全部IO都已移出owner。 | `test_migration_progress.py`、`test_migration_benchmark_metrics.py` 校验观测字段；manifest脚本测的是JSON镜像生成/加载/流式输出，不是业务有效V3作业或SQL hydrate。缺少完成的1/4/8/16同冻结样本端到端基线。 | **部分完成；完整基线pending，按用户要求停止。** |
| 2. 有界解析、IO执行器、临时盘点、全局覆盖 | `parallel.py:MigrationIO` 同时限制线程数和已提交任务，以干净 `Context` 执行，取消后等待真实Future；`inventory_parallel.py:spool_workspaces` 按 `memory_bytes // workers` 预留额度、先预检再解析并写spool，保留按原index排列的摘要。`spool.py:InventorySpool` 使用私有目录、记录/总盘额和原子发布。`source_inputs.py` 在枚举时执行条目和文件字节准入。 | `test_inventory_parallel.py::test_parallel_workspace_spool_matches_serial_inventory`、`test_nonempty_session_roundtrip_and_real_parser_overlap`；`test_migration_spool.py::test_filesystem_enospc_rolls_back_record_and_disk_accounting`、`test_protected_root_and_symlink_rejected`；`test_source_input_capacity.py::test_preflight_rejects_empty_directory_tree_before_full_scan`。这些覆盖真实解析重叠、序列化往返、容量早停和清理，不能证明任意输入的RSS硬上限。 | **实现完成，有本地/定向证据；当前完整逻辑回归pending。** |
| 3. 同task SQL协调、私有上下文、workers=1兼容 | `parallel.py:MigrationCoordinator.run` 只为worker创建task，`_serve` 在调用方原task直接执行注册handler；队列槽位为 `2 * workers`。`parallel_import.py:MigrationDomainRouter` 只暴露固定领域操作，检查store私有性；`ParallelWorkspaceContext` 持有私有Session/Chat/Turn映射，worker通过request调用SQL。没有放松通用数据库任务归属。 | `test_migration_parallel.py::test_sql_commands_run_in_original_task_with_bounded_workers` 在handler内断言当前task等于owner且worker峰值为4；`test_single_worker_preserves_order_and_rejects_unknown_command`；`test_parallel_import.py::test_workspace_io_runs_concurrently_and_contexts_are_private`。实际完整生命周期和旧CLI回归另有历史通过，不能据局部owner断言宣称全部数据库路径已全量执行。 | **实现完成，有局部及生命周期证据；当前完整逻辑回归pending。** |
| 4. Publication SQL/IO拆分、私有store、增量journal与分页恢复 | `ParallelWorkspaceContext` 将prepare→NAS读写→commit明确分开；NAS阶段经共享 `MigrationIO`，domain router持有各workspace独立store。`direct_import.py:DirectImporter.run_parallel` 先完成workspace，再使用同一IO执行分享阶段。`journal.py:_hydration_entries` 用id游标分页；`hydrate` 逐票据与旧镜像比对，assignments/batches使用 `DiskMap`；运行期关闭 `persist_updates`，`refresh_batch` 仅查当前key，阶段末写镜像。`disk_manifest.py:load_disk_manifest/iter_json` 逐镜像记录读写。 | `test_parallel_import.py::test_parallel_workspace_import_keeps_legacy_stage_order`、`test_artifacts_precede_workspace_identity_and_keep_global_index`；`test_parallel_shares.py::test_shares_use_private_stores_shared_bounded_io_and_owner_sql`；`test_v3_journal.py::test_hydrate_reads_ordered_keyset_pages`、`test_hydrate_checks_mirror_after_all_pages_before_replacing_it`、`test_deferred_updates_keep_sql_authority_without_full_mirror_rewrites`。后者明确断言热路径不persist、不出现分页全表查询，重建后票据相同。 | **实现完成，有恢复/热路径证据；万级SQLhydrate与端到端资源验收pending。** |
| 5. 全量verify/finalize预算、错误/取消/续跑 | `verify.py` 使用 `ReadOnlyDatabase` 并把health传入验证阶段；`verify_parallel.py` 将SQL证据采集与IO比较分开；`runtime_prefetch.py` 有预算门禁、pin释放及磁盘覆盖集合，拒绝漏项/重复并按原index合并。CLI `_execute` 先要求完整import，验证成功后重新读取冻结源、比较旧分享/schema，再允许ready及输出凭据。`parallel.py` 停发、拒绝待处理RPC、结清worker和真实IO；health await后重新检查worker终态。 | 本地：`test_full_rpc_slots_cancel_settles_acquirers_queued_and_active`、`test_worker_stop_during_health_probe_prevents_queued_commit`、`test_worker_failure_preserves_inflight_transaction_and_rejects_next`（均在 `test_migration_parallel.py`）；`test_verify_parallel.py::test_auxiliary_verification_rejects_incomplete_dispatch`；`test_runtime_prefetch.py::test_runtime_prefetch_rejects_missing_or_duplicate_coverage`。真实MySQL专项见下节，覆盖cutoff、全服务屏障、verify只读、持锁连接丢失及COMMIT响应丢失。 | **实现及列明故障证据已具备；不宣称所有可能故障排列均已验证，当前完整逻辑回归pending。** |
| 6. 万级MySQL/本地目录/可控IO延迟，默认参数与手册/放行 | CLI已有workers/io-workers/memory-budget/work-dir，操作补充手册已记录输入范围、资源门禁、停写和恢复要求；有代表性万级fixture与观测代码。 | 用户停止万级后未重启。没有完整1/4/8/16档discover→finalize结果、可重复IO主导收益或真实NAS饱和点证据。旧超时失败/主动取消不可算成功。 | **未完成；user-stopped门禁，禁止生产放行。** |

## 会话输入与资源边界

当前 `source_inputs.py`、`inventory.py`、`inventory_parallel.py`、`inventory_v3.py` 共用明确入口：registry、sessions、dialog（支持清单配置的替代路径），以及审核过的Session文件和对应generation ledger。业务附件、代码、技能树不递归扫描或计量；分享按明确snapshot路径读取。配置证据与分享父目录仅浅检查，discovery在已登记workspace剪枝，仍检查未解释的workspace覆盖。

本轮核对 `_preflight_workspace` 实际传递配置的三个入口和 `entry_limit/max_file_bytes/max_total_file_bytes`。每workspace条目限额为 `(memory_bytes // workers) // 512`，包含去重的目录、文件及必要浅层祖先；文件累计原始字节另受单worker额度的1/16限制，并保留reader硬上限。目录不计入 `inventoried_source_bytes`。workspace摘要/源绑定索引仍随workspace数增长，spool占用随解析总量增长；预算是准入估算，**不是整个作业workspace数量上限或进程RSS硬上限**。

`inventory.py:_sessions` 复用选定文件清单拒绝未映射嵌套JSON；不能仅因目录名为 `.turn-ledger` 就忽略。默认ledger由原generation检查处理，显式映射Session仅解释它对应的generation。既有非空 `fingerprints` 仍保持原全源哈希契约，可能读取业务文件；不能清空旧指纹绕过。空指纹作业依赖全程强冻结及mtime辅助检测，不保证发现正文与全部相关mtime都被恶意保留的修改。

## 已有执行证据与本轮pending分开记录

以下是历史/定向执行，不能拼接成当前HEAD一次全量通过：

- 含最终输入边界两个提交的隔离版本单元/CLI：**1408 passed，55.43秒**，`/tmp/conversation-input-capacity-final.xml`。该提交内容随后快进合入；本报告未重新运行。
- 同一最终输入边界版本的真实MySQL双服务生命周期：**1 passed，163.15秒**，`/tmp/conversation-input-capacity-lifecycle.xml`，使用本地目录，包含import/resume/reentry/verify/finalize、分享与authority。
- canonical修复专项：`test_migration_global_barriers_mysql.py` **4 passed，396.77秒**；`test_migration_verify_readonly_mysql.py` **2 passed，294.42秒**。前者在真实CLI入口观察全部service及share-only tenant的cutoff，提交后注入中断并resume；尾部源变化/保留正文损坏阻止ready和凭据。后者先断言9张快照表非空，再逐值比较所有行、原分享八字段和manifest字节，覆盖成功与最终源重查失败。它们早于最终输入容量提交，不称为最终HEAD整套复验。
- 真实连接丢失：`test_migration_lock_loss_mysql.py::test_killed_owner_connection_drains_io_then_resumes_same_tickets` 曾 **1 passed，139.27秒**；只KILL本测试核对过schema/connid/命名锁归属的连接，等待实际IO排空，同manifest恢复、票据不变并完成验证。真实COMMIT响应丢失：`test_migration_commit_loss_mysql.py::test_lost_driver_commit_response_resumes_original_manifest` 曾 **1 passed，140.63秒**。均为特定故障点证据，不代表断网后可换连接继续写。
- 旧完整逻辑进程：**255 passed、1 skipped、1 failed，5147.43秒**。失败发生于故障注入前，原因为长进程混合加载旧freshness与新inventory接口。固定当时代码的新进程续跑失败项与未执行范围：**98 passed，3239.75秒**，`/tmp/conversation-logic-remaining-mysql.xml`。旧255通过仅对应各自加载版本；98项也不是当前完整suite。
- 已确认skip为既有workspace/secret未配置的app bootstrap路径。当前真实环境HTTP认证/租户启动的这一既有资源路径未验收，不能记通过。

**本轮当前固定源码完整逻辑验收：未完成。** 首轮两个分组均遇首个失败停止：

- shard 1：**11 passed、1 failed、1 error，270.16秒**。`test_chat_repository_mysql.py::test_atomic_rebind_and_busy_target_rejection` 在 `get_autocommit` 超时，teardown连接丢失；日志 `/tmp/conversation-current-shard-1.log`。
- shard 2：**20 passed、1 failed，272.85秒**。`test_api_mysql.py::test_http_context_numeric_stale_and_sql_failure_are_distinct` 的context-usage期望200、实际503；日志 `/tmp/conversation-current-shard-2.log`。
- 两个失败项随后在单进程复跑：**2 passed，23.548秒**，`/tmp/conversation-current-failure-repro.xml`，runner退出0，隔离库finally清理成功。复跑通过不抹去首次失败，也不能据此认定为环境问题；初次故障根因尚未确定。
- 首轮两个分组共31项通过；两失败项在单进程复跑2项通过。余下327项单进程运行至首个失败，报告 **289 passed、1 skipped、1 failed，4191.15秒**，日志 `/tmp/conversation-current-remaining.log`、JUnit `/tmp/conversation-current-remaining.xml`。skip仍是缺既有workspace/secret配置的app bootstrap用例。
- 该单进程失败为 `test_rollback.py::test_new_compact_and_stop_export_preserve_old_archive_and_complete_root`，在 `retentions.pin` 的 `fetch_reference_rows` 查询中触发30秒数据库操作超时；没有进入测试后续归档断言。相同用例随后单独运行 **1 passed，50.81秒**，日志 `/tmp/conversation-current-third-failure-repro.log`、JUnit `/tmp/conversation-current-third-failure-repro.xml`。复跑通过不消除首次超时，也不能证明其为环境故障。
- 原360节点集合尾部36项已单进程完成：**36 passed，999.60秒**，JUnit `/tmp/conversation-current-final-tail.xml`，日志 `/tmp/conversation-current-final-tail.log`，隔离测试runner正常退出并执行父schema清理。
- 核对六份JUnit中的node id后，360项全部有终态记录，359项至少一次通过，1项跳过（既有workspace/secret样本未配置）。三个不同用例曾在先前运行中失败，之后均单项通过。**这是跨多次运行的覆盖汇总，不是一轮全量零失败验收**；数据库操作超时/503根因仍不确定。

完整逻辑suite节点覆盖已收齐；剩余逻辑验证工作是解释间歇性数据库路径失败，并在根因处理后取得固定版本的一轮全量结果。不得把多轮覆盖汇总表述为单次全绿，也不得用collection、pre-commit或以前版本数字替代当前全量验收。

## 剩余完成门禁

1. 收齐当前固定源码完整逻辑suite的分组结果，保留实际skip条件和版本信息；失败必须先定位、修复、重验，再更新结论。
2. 万级四档与可控IO延迟演练仍未完成，按用户要求保持停止。后续只有用户重新授权该验收范围才恢复，不能以完成逻辑回归自动解除停止要求。
3. 真实NAS的延迟、独占发布/fsync语义、临时卷容量及现场参数需上线前验证；本地目录成功不能替代这些证据。
4. 停旧writer、强冻结、旧进程退出且IO排空、单schema协调作业等运维前提仍成立。命名锁不能充当跨进程NAS fence，断连接不得自动换连接接续写入。

当前只可陈述“实现已具备列明的逻辑与资源准入能力，并有对应定向证据”；原六项设计的规模门禁尚未达成，**不提供生产迁移放行结论**。
