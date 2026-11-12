# 并行迁移逻辑断言复核

日期：2026-11-12。本轮按用户要求停止万级性能基线，聚焦逻辑正确性。本文不将先前性能采样、未完成的万级运行或有限故障注入描述为完整验收。

## 已复现并修复的提交准入竞争

修复提交：`8d78eece8`。

`MigrationCoordinator._serve` 原先在每轮开头观察 worker 结果，然后等待 owner task 的 SQL health probe，再直接执行排队的 domain handler。如果 worker 在 health probe 的等待期间失败或取消，health 成功返回后仍可能启动一笔新的事务。

新增可控 Event 时序回归 `tests/unit/app/conversation_storage/test_migration_parallel.py::test_worker_stop_during_health_probe_prevents_queued_commit`，分别注入 worker 异常与取消。旧实现两种情况均真正执行了排队 commit，结果 **2 failed**，并非超时模拟。修复在 health 成功返回且设置下一探测时间后回到循环顶部，重新观察 worker 的终态，再决定是否接收排队事务。

回归断言不再执行该 commit、保留原始 worker 异常、所有已创建 RPC Future 结清、队列清空及 semaphore 容量归还。新增 `test_worker_failure_preserves_inflight_transaction_and_rejects_next` 另行证明：已经被 owner 接受的当前事务仍正常完成，随后排队事务被拒绝，不将当前事务误作未提交，也不吞掉原 worker 异常。

修复后运行 `test_migration_parallel.py`、`test_migration_lock_guard.py`、`test_parallel_import.py`、`test_parallel_shares.py`，共 **42 passed**；指定两文件全部 pre-commit 检查通过。本修复未运行远程数据库测试。

同类等待窗口核对：domain handler 执行完毕后原有 `continue` 会回顶部观察 worker；队列唤醒等待与定时等待结束后也自然回顶部。上述路径不存在 health probe 返回后直接进入新 handler 的同一窗口。

## 正在补充的跨层断言

以下三个方向正在由其他代理补充，尚待独立复核与验收，不把现有局部测试扩大为已经覆盖整条 CLI 链路：

1. **首次 import 与 resume 的 cutoff**：现有 `test_tenant_sync_batch.py::test_all_tenant_cutoffs_persist_once_and_resume_keeps_existing_values` 验证 manifest API，`test_parallel_import.py::test_parallel_import_requires_fixed_tenant_cutoff` 验证缺失拒绝。跨层断言需证明包含多 service/tenant 与 share-only tenant 的 cutoff 在首个 workspace 执行前均已落盘，中断后同 manifest resume 全部保持不变。
2. **最终 ready 屏障**：已有 source freshness/recheck、辅助任务准确覆盖集合及 runtime root 拒绝的局部回归。跨层负例需证明 source 在最终重查前改变，或某个 service 的尾部验证失败/任务遗漏时，即使其他 service 已成功，job 仍不进入 ready，且不写出 completion/runtime 凭据。
3. **verify 的全链路只读性**：已有 `test_checkpoint_reader.py::test_offline_full_traversal_uses_read_only_adapter_without_sql_writes`、`test_simplified_batching.py::test_read_only_lease_does_not_expose_mutations` 和 readonly hydrate 回归。跨层验收需覆盖成功与后段失败的 CLI verify，逐值比较业务表、分享原有八字段、迁移日志及 manifest，而非仅比较记录数量。

分享字段与 scope 的现有直接证据包括 `test_parallel_shares.py::test_shares_use_private_stores_shared_bounded_io_and_owner_sql`、`test_share_resolution_preserves_linked_and_orphan_scopes`，以及真实多服务生命周期和 service isolation 测试；它们无需等待万级运行才能提供有限的逻辑证据，但不能代替上述最终 CLI 屏障与只读性负例。

## 真实小规模验收进展（后续更新）

以下为 2026-11-12 本轮实际执行状态，不将 collection 或 pre-commit 结果当作集成通过：

- 新 global barrier 集成中，**多 service 与 share-only tenant 的 cutoff 落盘/中断/resume 用例已通过**，**`source_after_verify` 用例已通过**，证明该构造下真实验证成功后源变化会在最终重查阻止 ready 和完成凭据输出。
- **`service_b_tail_body` 用例真实失败：`DID NOT RAISE`。** 故障注入修改了 service B 的 `legacy_root` 保留正文，而 runtime recovery 正文仍完好。旧版本验证因此未拒绝；`legacy_root` 还承担旧格式 export 的来源，不能把它解释为无用正文或降低测试检查。这是已确认的保留正文校验缺口。已在 `14b98be94` 补完整源 root 比较；真实远端隔离 MySQL 四项屏障用例 **4 passed，396.77 秒**，分别覆盖 cutoff/resume、verify 后源变更、明确 B Session 的 legacy_root 与 recovery 损坏阻断。独立提交后复核通过。
- readonly 两个真实 CLI verify 用例在 canonical 修复前 **2 passed，346.58 秒**，已提交 `cee543980` 并独立复核；canonical 修复后第二轮 **2 passed，294.42 秒**，全部表/分享原字段/manifest 对比通过，结果 `/tmp/conversation-canonical-readonly.xml`。
- 此前本地单元回归 **1330 passed**，该结果早于本次保留正文验证修复，不能作为新修复的验证证据。
- 完整逻辑 suite 的一个 skip 已由最终 JUnit 确认为 `test_app_bootstrap_mysql.py::test_real_app_auth_bootstrap_uses_existing_workspace_and_sql`：缺少 `SWE_TEST_EXISTING_WORKSPACE` 或 `SWE_TEST_EXISTING_SECRET` 时跳过。因此既有真实 workspace/secret 的 HTTP 认证、租户启动及 SQL 路由链路未在本轮验收，不能扩大为此链路已通过。

万级性能基线保持停止；上述状态仅涉及逻辑验收。尚未通过或仍在运行的用例必须等待修复后的真实结果再闭环。

## 会话输入枚举边界

`f5b8344a2` 新增统一显式入口和 legacy 选定工作副本；`9724ec7c9` 将容量、文件清单、只读/路径安全、freshness 和 discovery 覆盖剪枝统一到会话输入。普通附件、代码、技能不递归扫描或计入预算，分享按明确路径；旧非空 fingerprints 仍保留原全源哈希契约。两次提交后独立复核已执行。

复核发现自定义 registry/sessions/dialog 入口预检漏算：旧提交对超限65字节返回0，`8e8ccc9a4` 已修复并补三参数回归。新增/删除/替换/修改的实际 inventory 检测及 root 祖先 symlink 拒绝共5项通过；保留mtime及父目录mtime的恶意替换仍依赖强冻结，不声称全源哈希保护。

中间全范围单元快照 **1394 passed，55.99 秒**，含自定义入口及本地 CLI 枚举补测。万级保持停止。

完整旧进程结果为255 passed、1 skipped、1 failed（混合加载旧freshness/新inventory_parallel接口，发生于commit-loss前置inventory）。固定代码的新进程重跑失败及未执行范围，最终 **98 passed，3239.75 秒**，无error/failure/skip。skip已确认是既有workspace/secret缺失的app bootstrap测试，不计通过。


隔离工作树补验：`160f4bca8` 拒绝未映射的嵌套 Session/未知 ledger JSON（旧实现3项错误放行，修复后负例及可信映射正例通过）；只复用已选文件清单，不新增扫描。`23bee5b7d` 将容量准入前移到枚举阶段：去重entry含目录/必要祖先，scandir逐条检查后才累积待处理路径，单文件和总字节同时早停。旧预检对参数透传与100空目录早停两项均失败，新9项通过。两个提交已独立复核，隔离树最终组合回归 **1408 passed，55.43 秒**，JUnit `/tmp/conversation-input-capacity-final.xml`，含旧CLI与新增真实枚举CLI。数据库续跑已结束，两个提交已快进合入目标工作树，提交内容与隔离测试版本相同。


本轮完成结论：会话输入枚举边界、容量准入早停、未知嵌套Session候选阻断及canonical保留根校验已实现并小步提交，提交后均完成独立复核。最终版本单元/CLI **1408通过**，专项真实MySQL屏障与只读性 **4+2通过**，最终版本双服务完整生命周期 **1通过**，旧完整进程失败项及未执行范围新进程续跑 **98通过**。旧进程的255通过与1skip仍按各自加载版本记录，不能合称当前HEAD的一次性全量通过；既有工作区/secret真实app bootstrap仍未验收。万级四档及真实NAS性能未验收，不作生产放行结论。
