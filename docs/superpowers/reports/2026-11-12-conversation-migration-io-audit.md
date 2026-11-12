# Direct v3 迁移 IO 执行位置审计

审计日期：2026-11-12。基线 HEAD `3fcae193f`，同时读取当时工作区已落盘的 inventory 预检改动。范围是实际 direct v3 CLI：discover/freeze、spooled inventory、import/resume、verify/finalize。本文先做静态调用链审计，随后按独立任务最小修复 share parent freshness 去重；未运行远程数据库或作性能达标判断。其他代理仍在调整源预检的 IO 调度与进度，后续提交须重新核对对应条目。

结论：重正文读取和 bundle 发布已有有界线程并发，但并非全部 IO 都已并行。SQL 必须由进入 named_lock 的原 owner task 执行；本地 SQLite、spool 读取、manifest 镜像及部分源树完整性检查仍同步执行。发现 share freshness 可退化为二次扫描的路径，本次已做单次盘点局部缓存修复；仍不能把现状描述为所有 NAS IO 均不阻塞 owner。

2026-11-12 后续范围更新：`f5b8344a2`、`9724ec7c9`、`8e8ccc9a4` 已将新清单的 workspace 枚举收窄到明确会话入口，配置证据和分享父目录仅浅检查，已登记 workspace 的 discovery 覆盖扫描剪枝。下文关于共享父树递归和全 workspace 文件扫描的描述仅代表上述旧基线；旧非空 fingerprints 仍保留全源检查。显式枚举器的准入早停已由隔离提交 `23bee5b7d` 和9项回归验证，逐条计数包含目录；仍不能由线程池数量或准入预算推断进程 RSS 硬上限。

## 执行位置与调用链

这里的 CLI 主线程指同步 Click 命令/`run_v3_command` 的调用线程；SQL owner 指该线程上 `asyncio.run` 创建、进入持锁范围的原协程任务。benchmark 用 `asyncio.to_thread(run_v3_command, ...)` 只移动整个 CLI 到另一线程，不改变内部 owner 与 worker 的边界。

| 阶段 / IO | 实际入口 | 执行位置 | 判断与限制 |
| --- | --- | --- | --- |
| discover 的服务配置、agent.json、目录覆盖、share snapshot JSON | `discovery.discover_services` → `_profiles`、`_shares` 等；`conversation_storage_discovery_cmd.discover_command` | CLI 同步调用线程 | 独立发现阶段尚无作业 SQL owner；仍串行且会等待源存储。不能声称 `--workers` 并行整个 discover。 |
| 全源只读权限、目录、文件 SHA256 指纹 | `discovery.fingerprints` | 调用者同步；旧manifest的非空fingerprints触发时：`inventory_v3_spooled` 在 async owner | 新 discovery 报告 fingerprints 默认为空，不主动计算此全树hash；旧报告带非空fingerprints时仍完整读取校验。去掉嵌套根重复覆盖，但触发时每次仍读取全源文件并扫描目录。是强一致性屏障，不应因性能直接删除。当前会阻塞 async progress/health 调度，允许保持语义不代表已满足性能目标。 |
| freeze 重新发现、语义预检、manifest 原子写 | `freeze_discovery` 再调用 `discover_services`；`manifest._persist` | 发现外层同步；语义预检内部另起 asyncio inventory；持久化同步 | 重新核对 frozen evidence 是刻意重复校验；不是每批导入都 freeze。写临时文件、fsync、replace、父目录 fsync 必须保留。 |
| 单 workspace stat/scan、registry/session/archive JSON 解析 | `inventory_parallel.spool_workspaces` → `_preflight_workspace` / `inventory_workspace` | 每个 `MigrationCoordinator` workspace worker 提交 `MigrationIO.call` 线程 | 审计当时版本预检也经 `io.call`；先预留 `memory_bytes // workers`，文件/总字节/文件数准入后解析。`inventory_workspace` 传单 workspace 且清空 fingerprints/shares，不会每个 workspace 重扫全服务指纹。 |
| 共用 evidence parent freshness | `inventory_parallel._shared_freshness` | inventory 前一次 `io.call` 线程 | 按 `(parent, cutoff)` 预扫描复用；缓存仅本次 inventory 有效，不跨运行跳过校验。独有 workspace 的检查仍在 parser 线程。 |
| spool 编码、digest、写临时记录、replace | `spool_workspaces` → `spool.write` | `MigrationIO.call` 线程 | 有盘额及单记录上限，worker 数和 IO 提交数有界。不是先把全 inventory JSON 放内存。spool 生命周期创建与清理本身仍同步。 |
| spool 回读、digest 核对、JSON 解码 | `spool_report.WorkspaceRecords._record` → `InventorySpool.read` | 访问 Sequence 的调用任务同步执行；常见为 owner，部分为 workspace worker 协程 | 单条缓存，换 workspace 会同步读本地临时盘并解码；`async worker` 并不自动等于 IO 线程。当前牺牲事件循环延迟以兼容同步 Sequence API，应单独测慢临时盘和大记录，不应以“本地盘”当作无成本。 |
| share 准入 stat、归属索引、snapshot JSON | `inventory_v3._preflight_share_budget` / `_inventory_shares` | `inventory_v3_spooled` 的 async owner；discover semantic async 调用同样同步 | 先做总字节/数量预算，但 shares 仍全集列表；归属索引一次扫 workspace/chats。snapshot read 与 freshness 目前未进入线程，仍可移入有限线程；parent去重本次已修复（见下）。 |
| 初始/结束 source freshness、runtime source 可导入检查 | `DirectImporter.run_parallel` → `_assert_source_fresh`、`_validate_runtime_sources` | 当前基线 owner 同步 | freshness 的 stat/walk 与访问 spool 都可阻塞；runtime 检查还含 recovery JSON 序列化/消息提取。共有 `checked_by_cutoff` 去重，不能错误按每 workspace 递归父树重复。其他代理在拆该路径，本文不宣称已完成。 |
| 保留原始 artifact 读取与确认 | `ParallelWorkspaceImporter.run_workspace` → `_retain_source_artifacts` → `_read_source_artifact` | 每 workspace 内 artifact→identity→Session→Chat 顺序；原始读取经 `context.io.call` | 与不同 workspace 重叠；保留原全局 workspace index。旧串行 API 仍保留，不能把旧 `_retain_source_artifacts(report)` 路径等同本 CLI。 |
| bundle publish / fsync / confirm / inspect / body read | `parallel_import` context → `offline_write` / `offline_commit` / `offline_verify`、`bundle_write` | NAS-only 证据/发布阶段经受限 `MigrationIO`；固定 SQL prepare/commit 命令在 owner | 不用 worker 执行通用数据库协程。guard/票据/locators/状态重新核对保留；已提交批次仍完整读证据验证，不能只因已有文件就放行。 |
| share 发布 | `parallel_shares.run_parallel_shares` | 全 workspace 屏障之后独立有界 worker；scope/目标映射 owner，bundle IO 线程 | shares 列表的源 JSON 在 inventory 阶段已经串行读入；并发发布不代表前面的 share 源读也并发。 |
| 显式 Session/Chat message、epoch/artifact/share 语义验证 | `verify_parallel.verify_workspace_messages` / `verify_auxiliary_bodies` | owner 固定 SQL 采集；worker 比较、`MigrationIO` 读 NAS | 确定性合并，覆盖集合必须完整。artifact 源文件比较也进入 IO 线程。 |
| runtime Session/Chat/History/Turn/Archive/share reader 全验证 | `verify.runtime_workspace/runtime_share` → `runtime_prefetch` | owner 采集 locators/transient retention；worker 经 IO 线程读完整新鲜 bundle；原 adapter 在 owner 用严格 cache 读取并执行 SQL/比较 | 原 runtime reader 内部 `PinBatch.read` 会用线程调用 cache，但不再回落 NAS。miss/长度/extent 不匹配失败。每 workspace 预算含证据/解码预留；超过失败，不无界缓存。SQL 与校验 CPU 仍串行。offline retention 非持久 GC pin，依赖停止 writers/GC 的原契约。 |
| 源复核屏障 | CLI `_recheck_spooled_inventory` → 再次完整 spooled inventory / `inventories_equal` | parser/写盘线程；外层指纹/share/比较与两个 spool 读取仍在 owner | 是每命令结束前的完整再读，verify/finalize 也有；不能把一次 inventory 成本当完整端到端 IO 成本。两个 inventory spool 会同时存在，需合计临时盘占用。 |
| 本地 manifest/coverage/票据 SQLite 索引 | `disk_manifest.DiskMap`、journal `hydrate` / `refresh_batch`、runtime coverage | SQLite 查询/JSON 编解码同步在调用任务；SQL journal 始终 owner | 64 KiB cache、临时索引盘策略，控制内存而非消除 IO 等待。`hydrate` SQL 分页；`refresh_batch` 定位单批，不每次重新扫描全部 SQL entries。 |
| manifest load / 阶段镜像 / completion 输出 | `V3Manifest.load` → `load_disk_manifest`；`manifest._persist`；CLI `_write_outputs` | CLI 同步 load；持锁 owner 中同步持久化/输出 | load/写出流式处理映射避免复制全部 assignments，但仍完整走一遍文件与索引；阶段 fsync/replace 需保留。completion 每服务 receipt 与总 receipt 写文件，不应纳入“NAS正文完全在线程中”的保证。 |

## 重复扫描与复杂度

1. **已消除的主要乘积扫描**：`direct_import._workspace_inputs` 对 spool files 使用 `for_workspace(index)`，普通列表也仅先归组一次；不再每 workspace 筛一次全部 files。`inventory_workspace` 不执行全服务 fingerprints。journal 热路径 `refresh_batch` 只查单批，hydrate/retention capture 分页。
2. **本次发现并修复的 share freshness 二次扫描**：`inventory_v3._inventory_shares` 每个 share 调用 `_source_change([snapshot, snapshot.parent], cutoff)`。`_source_change` 各路径独立调用 `changed_source`，未传跨 share 的 `checked`；`inventory_v3_spooled` 传入的是原 data，`spool_workspaces` 内生成的 `_freshness_cache` 不回传，且 `_shared_freshness` 只收 workspace evidence_refs。若同目录有 K 个 share snapshot，K 次 parent walk/stat 可达 O(K²)，且在 owner 执行。字节预算只限容量，不能消除此时间复杂度。本次最小修复在一次 `_inventory_shares` 调用内按 `(parent, cutoff)` 去重，仍逐个确认 snapshot；函数返回即丢弃缓存，不能跨命令缓存。旧manifest非空fingerprints分支保持原样。新增回归核对同parent计数、目录mtime变动阻断、CLI最终spooled recheck重新扫描及不持久化blocker。
3. **有意常数倍全源复核**：新发现不自动生成fingerprints；freeze会再发现并语义解析，命令结束source recheck再次解析。旧清单若有非空fingerprints，spooled inventory 前后/分享后还会全树hash。它们不是 W 次全树遍历，但常数大，且源目录如果在 NAS 上会显著串行阻塞。可把无 SQL 的扫描移入有限线程并保留阶段屏障；先测总字节/调用数，不删除校验。
4. **服务/根数量相关乘积**：`fingerprints` 的嵌套根消重比较为 O(R²) 路径关系比较；discover 服务相互边界校验有服务对循环；share snapshot 定位按每 share 遍历 service。通常 R/服务数远小于 workspace 数，不是 W² 全内容扫描，但大量 service 时仍需单独量测。
5. **spool 反复解码不是全局常驻缓存**：单记录缓存会在验证多个 passes、交错索引访问和遍历 files 时重复解码同一 workspace。扫描本身一般是 O(W+F) 次访问；某 workspace 的 Fw 个 artifacts 若交错导致其大记录被重复解码，可产生 O(Fw×record_size) 成本。不能从“单条缓存”推导出磁盘访问次数也最小；需要实际读调用指标再决定改同步 API/增加有界预取。
6. **源 `changed_source` 的内存行为**：对单目录先收集 `candidates` 再 lstat，而非真正逐条 streaming。路径数大的共享父树仍可产生大临时列表；parser 的源字节/文件数准入只覆盖 workspace，不能自动约束全部共享 parent。

## 放行口径与后续切点

当前可证明的是正文重 IO 的受限并发与 SQL 单 owner；不能证明全部 IO 已并行、owner 永不阻塞、RSS 是硬上限，或 10k 四档已全部通过。需要把源扫描/解析、spool 本地 IO、SQLite/镜像、bundle IO 和 SQL 往返分别量测。

优先级：share parent 跨 share 重扫已局部修复；随后把 inventory 全局 fingerprint/share 源读及 importer source 检查拆成有限 IO 票据，并使 progress/health 在长扫描期间可继续运行；本地 SQLite 和 manifest 持久化留 owner 的理由是单点控制一致性，若量测成为瓶颈再做固定阶段或独立本地 IO 通道，不能把 raw DB/manifest 可变对象下放 worker。finalize 的持久输出与 ready 状态顺序、fsync/重复输出一致性也不能为并发改变。

本报告对应工作未执行远程 DB 测试。除新增报告外，仅最小修改 `_inventory_shares` 的本次盘点缓存与相应本地回归；其余所列同步IO未改动。当前存在的失败验收必须以实际 pytest/JUnit 为准；观测工具通过不等同迁移通过。
