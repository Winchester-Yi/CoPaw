# 会话迁移并行实现与验证记录

状态：进行中，尚未完成万级端到端四档验收。测试使用用户授权的隔离 MySQL schema 和本地目录；不代表业务 NAS 性能。

## 已提交实现

| 提交 | 结果 |
| --- | --- |
| `f9c74e9c0`、`ae9e05b98` | freshness 单次扫描去重，避免每 workspace 重扫共享配置父目录；每次重验重新扫描 |
| `5945326fc` | 旧 manifest 进度逐条解析到 SQLite 私有索引；journal 分页 hydrate、局部更新、流式镜像/CLI JSON |
| `945fd1b6c` | workspace Session/Chat 正文、epoch、artifact、share NAS 检查有界并行；SQL 固定领域采集仍在 owner task |
| `f6229737a` | retention 首分页覆盖 ID=0；header 预扫描索引采用独立 64 MiB 配额并清理 |
| `cdc95be39` | 原生 artifact 发布移入 workspace worker，保留 artifact→identity→Session→Chat 顺序及原始全局索引 |
| `b815d6108`、`1d825d562` | 确定性双服务万级夹具，修正首个 workspace 提交后中断点和非法 artifact 索引拒绝 |
| `4c01ab4de` | CLI stderr 计数进度与独立结构化失败报告；不记录正文、SQL 或参数 |
| `6cdbb52eb` | 关闭的 stderr 不覆盖业务异常；首个失败位置按 task 保留；计数单位明确为 task |
| `975d08409` | inventory/plan/inspect/preflight 及无 apply 预览统一有界 spool，补齐资源选项与导入 owner 锁健康检查 |
| `fd77d6a40` | discover/freeze CLI 有界语义盘点；只读命令不持久化源变更 blocker；verify owner 健康检查 |
| `64ebdd73b` | 新 discovery 保留空 fingerprints、不做全源 hash 契约；非空旧绑定的语义盘点仍保留前后检查 |
| `bea1b39e9` | 真实 MySQL 生命周期测试包装器透传锁健康检查，未绕过 guard |
| `4d5a69b86` | SQL RPC 容量满、正在 acquire、queued 与 active 的重复取消/失败均结清全部 caller/Future |

后续提交：`e162aedba` 增加清单 JSON 镜像规模测量；`8927ba439` 分享发布复用同一个 MigrationIO 和 owner 票据流水线；`c98fee187` 附加正文校验拒绝漏/重复调度；`1b16e6d6c` 完整 runtime 校验正文有界并行预取，原 adapter SQL/事务检查仍在 owner；`95b1fb067` 生命周期 fixture 对齐固定 cutoff 和真实并行提交后中断点；`cb9f88535` 基准失败时保留 SQL/IO 计数及阶段快照；`bac7f5694` 真实 MySQL KILL 持锁连接后排空、恢复和 finalize 验收。

各提交均经过提交钩子；提交后已启动独立子代理复核。复核发现的 ID=0、header 无配额、非法 artifact 索引、中断点退化、进度观测异常及失败位置问题已修复。

## 当前验证证据

- 会话存储与 CLI 单元回归：**1,316 passed，55.35 秒**（guard 去重新增测试前）。
- 最新小规模真实 MySQL 生命周期：**1 passed，206.822 秒**，覆盖双服务、中断、resume、重复运行、verify、finalize、分享字段及 authority；结果为 `/tmp/conversation-latest-lifecycle.xml`，2026-11-12 13:36 更新，覆盖 artifact worker 与 owner health check。后续 runtime prefetch 改动尚未包含。
- 新进度/并发/spool/CLI 定向回归：**49 passed，0.60 秒**。
- 代表性样本合法 inventory 与跨独立目录内容摘要一致性：**2 passed**。
- 独立复核不替代运行验收。runtime reader 完整事务/SQL 检查仍在 owner 串行执行，但其正文由 worker 通过共享 MigrationIO 新鲜预取，严格 cache miss 失败，不回退同步 NAS。每 workspace 全部 committed/live publication 正文超出准入预算则明确拒绝；预算不是操作系统 RSS 硬上限，offline transient retention 不是持久 GC pin，仍依赖停写/停 GC。
- 真实 MySQL KILL 连接恢复：**1 passed，139.27 秒**，`/tmp/conversation-lock-loss-final.xml`。预先核验本测试连接 ID、schema 和命名锁，故障后停止接单并等待真实 IO 结束；同 manifest 恢复票据不变，verify/finalize 及 SQL ready 通过，隔离库清理成功。覆盖 prepared 正文发布期间失联，不代表 COMMIT 响应丢失场景。
- 最新双服务并行生命周期：**1 passed，200.18 秒**，`/tmp/conversation-latest-parallel-final.xml`，覆盖分享流水线和 runtime 预取。此前内部 executor fixture 未固定 cutoff 的失败已修复，不放宽生产 guard。
- `7107c6df2` 真实数据库 COMMIT 成功后丢客户端返回：**1 passed，140.63 秒**，`/tmp/conversation-commit-loss-final.xml`。生产 transaction/UnknownCommit 路径弃用连接并停写，服务器保留已提交行；同 manifest 恢复不重发布原 operation，ID/path/正文摘要不变，无重复丢失，最终 ready。
- `a6a8e2ba5` 文件部分写入后注入 ENOSPC，回滚 spool 文件、容量和 index 预留并可重试；**9 passed**。这不是填满真实磁盘或 NAS 故障测试。
- `41afd601b` 移除 guard 初始化后紧邻的重复锁查询；每次仍加锁及完整校验。**25 项 unit + 5 项真实 MySQL retention 测试通过**（33.88 秒），覆盖锁/备份/GC 行为。RTT 测量见 `2026-11-12-conversation-mysql-rtt-diagnosis.md`。
- discovery hash 契约与 lifecycle health-check 测试修复经独立复核，相关 **70 passed**；非空旧指纹在语义盘点前变更/后变更均拒绝。旧 discovery report 的重新 freeze 兼容性不因此扩大。
- 大清单进度条目 20k/40k/80k 的独立进程资源测量及方法边界见 `2026-11-12-conversation-manifest-scale.md`；不能替代完整迁移资源验收。

## 万级验收

`test_migration_10k_mysql.py` 对 workers=1/4/8/16 分别创建独立随机 schema 和本地 source/NAS。样本包括双服务、空 workspace、有效 Session/Chat、长尾 Session、归档、共享 Session 多 Chat、独立 Session 和每服务旧分享。Msg/归档身份与时间固定，配置目标根规范化后的内容 SHA256 用于比较数据一致性。

测量包含 discovery/freeze、preflight、导入中断/续跑、verify、finalize；记录源规模、阶段耗时、采样进程 RSS、同时存活 spool 总峰值、索引峰值。`SWE_MIGRATION_BENCH_IO_DELAY_MS` 可注入受控 NAS 延迟。记录运行前的夹具生成不计入阶段总耗时，RSS 每 10 ms 采样可能漏掉短暂峰值。

早期旧代码的万级运行不可作为成功证据：一次因共享目录平方级扫描在首次 inventory 阶段被有序停止；一次在旧全局 artifact 串行阶段遭遇 MySQL 连接丢失并产生 UnknownCommit，**1 failed、1 error**。另一次完整四档进程在 workers=1 输出 FE 后继续 workers=4，14:37 有序 Ctrl+C 结束，**1 failed、1 error、2 deselected，5400.01 秒**，`/tmp/conversation-10k-final.xml`：workers1 的 resume 数据库操作超时，fixture 清理在 acquire/session-state 剩余预算内超时。该进程启动早于分享流水线和 runtime 预取改动，不能算全量通过。

最新完整四档已以 `41afd601b` 启动，日志 `/tmp/conversation-10k-current.log`，结果预期 `/tmp/conversation-10k-current-final.xml`，独立本地目录 `/tmp/conversation-10k-current-run`。使用 `-x`：任一档失败即停，不继续执行无法整体验收通过的后续档；没有自动重连绕过未知提交。尚未通过，不能据此宣布性能目标达成。

## 未闭环事项

完整四档端到端与可控 IO 延迟的吞吐/资源/恢复验收；最终运行手册、设计状态、任务清单及放行证据更新。只有这些事项具有当前源码和运行证据后才能标记完成。

最新 workers=1 档开始运行。静态调用链核对显示 artifact 新发布执行数十次串行 SQL 往返，包括多个阶段重复的 target/guard/publication 查询；没有发现每 artifact 全表 hydrate 或全 JSON 写出。新增观测仅记录 cursor 调用次数、累计耗时及 bundle 方法计数，不记录 SQL/参数；SQL 不含连接池等待和驱动 BEGIN/COMMIT，bundle 方法嵌套耗时不可相加当作互斥墙钟时间。初始化/fixture teardown 失败由 pytest/JUnit 记录，verify/finalize 的 SQL/IO 快照合并。实际最新代码完整运行数据尚待收集，不能仅凭静态估算宣称瓶颈已消除。
