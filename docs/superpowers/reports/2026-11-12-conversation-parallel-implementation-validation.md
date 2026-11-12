# 会话迁移并行实现与验证记录

状态：本轮逻辑与输入边界验证已完成；万级端到端四档验收按用户要求停止，未通过。测试使用用户授权的隔离 MySQL schema 和本地目录；不代表业务 NAS 性能。

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

## 此前阶段验证证据（最终结果见末节）

- 会话存储与 CLI 单元回归：**1,317 passed，56.32 秒**（含 guard 去重和锁顺序回归）。
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

最新完整四档以 `41afd601b` 启动后，用户明确要求停止万级基线、先验证逻辑正确性。已单次 Ctrl+C 并等待进程退出；该次记录为主动取消，不能记为通过或逻辑失败。日志 `/tmp/conversation-10k-current.log`、JUnit `/tmp/conversation-10k-current-final.xml`。取消时累计 934.253 秒（不含清理），9,419 次 cursor 查询、0 次查询异常，committed 部分数据按停止语义保留到 fixture 收尾；该次隔离子 schema 已只读确认清理。以上仅是未完成运行的观测，不是性能验收结果。

本轮最初使用用户指定远端 MySQL，收集 `tests/integrated/conversation_storage` 排除万级文件的 **354 项**逻辑集成测试；每项使用随机独立测试库。该旧进程现已结束，255通过、1跳过、1失败，失败及续跑结果见末节。日志 `/tmp/conversation-logic-full-mysql.log`，JUnit `/tmp/conversation-logic-full-mysql.xml`。没有启动本地容器数据库。

## 未闭环事项

完整四档端到端与可控 IO 延迟的吞吐/资源/恢复验收；最终运行手册、设计状态、任务清单及放行证据更新。只有这些事项具有当前源码和运行证据后才能标记完成。

静态调用链核对显示 artifact 新发布执行数十次串行 SQL 往返；没有发现每 artifact 全表 hydrate 或全 JSON 写出。新增观测仅记录 cursor 调用次数、累计耗时及 bundle 方法计数，不记录 SQL/参数；SQL 不含连接池等待和驱动 BEGIN/COMMIT，bundle 方法嵌套耗时不可相加当作互斥墙钟时间。初始化/fixture teardown 失败由 pytest/JUnit 记录，verify/finalize 的 SQL/IO 快照合并。万级性能验证按用户要求停止，不能宣称瓶颈已消除。

逻辑验证补充：`b8c3f8532` 将源预检扫描移入共享 MigrationIO，增加明确标注的本轮 inventory workspace/Session/Chat/源字节/blocker 计数；单次扫描复用字节统计，不新增扫描，提交后 **30 项定向通过**。`c38eac57d` 修复同目录分享 freshness 平方级重复扫描，缓存仅单轮 parent/cutoff，逐 snapshot 检查和最终全量重查保留，混合 cutoff/目录增删/软链接/只读边界 **40 项提交后定向通过**。当时全范围单元快照 **1,321 passed，55.21 秒**，早于后续边界测试；最终结果见末节。

## 本轮逻辑与会话输入边界收尾（2026-11-12）

用户要求停止万级基线并优先验证逻辑；万级保持停止。以下均为隔离测试，不使用业务 schema。

- `cee543980`：真实 CLI verify 成功与最终源重检失败两路径，9 张非空表 SELECT * 全列逐值比较、分享原八字段和 manifest 原字节/JSON 不变；修复前 2 passed，346.58 秒。
- `14b98be94`：补 canonical legacy_root 校验。此前真实故障注入破坏该保留块时 finalize 未拒绝，确认旧格式导出证据漏验；新版本比较完整冻结源 root，校验 publication 作用域、票据、路径、committed/live 及只读 reader 生命周期。真实 MySQL 屏障 4 passed，396.77 秒，覆盖首次全租户 cutoff、中断恢复保持、最终源变更以及 B 服务 legacy_root/recovery 损坏，均阻止错误 ready/凭据。
- canonical 修复后真实 CLI 只读性复验 2 passed，294.42 秒，结果 `/tmp/conversation-canonical-readonly.xml`。上述两提交均完成提交后独立复核。
- `f5b8344a2`、`9724ec7c9`：统一显式会话输入枚举；容量、文件清单、路径安全、只读及 freshness 共用包含范围，legacy 副本从选定 Session/generation 或 Chat 入口复制，discovery 在已登记 workspace 剪枝。附件、代码、技能不递归或计量；旧非空 fingerprints 原全源哈希契约保留。
- 提交后独立复核发现自定义 registry/sessions/dialog 入口容量漏算，旧提交对 65 字节/64 字节限额返回 0；`8e8ccc9a4` 修复，三参数回归通过，提交后独立复核及 42 项相关回归通过。
- 输入新增/删除/替换/修改及祖先 symlink 负例 5 passed，明确禁止遍历无关业务目录。无关 20 MiB 附件、深层代码、未选 symlink 不影响容量；真实会话文件超限、选定 symlink/FIFO、未知 Session/ledger/归档仍拒绝。
- 全范围单元（conversation_storage、两个旧 CLI 契约文件及新增 CLI 边界用例）中间快照 **1394 passed，55.99 秒**，JUnit `/tmp/conversation-input-boundary-final.xml`；额外测试权限清理改动后单项再次通过。

完整旧会话存储逻辑集成进程早于本轮输入边界及 canonical 修改，不能以该运行替代上述新代码定向验收。该进程结果与已确认的既有workspace/secret缺失skip见下文。当前不宣称完整规模性能或生产放行。

本地 CLI 枚举补测 `aa673ae50` 已提交并独立复核：真实 discovery/freeze/inventory/plan 与 preflight 语义 spool 不读取无关20 MiB附件、20层代码目录和无关symlink；精确统计两项会话文件。仅 preflight 最后的 SQL `_execute` 被截获，故此项不是 MySQL preflight 验收证据。

完整旧进程已结束：255 passed、1 skipped、1 failed，5147.43 秒。失败为 commit-loss case 的 `changed_source(recursive=False)` 参数不匹配：进程早先加载旧 freshness，随后在源码改动后延迟导入新 inventory_parallel，形成混合版本；未进入 COMMIT 故障注入，不能当作此场景通过。新进程已固定当前代码重跑该失败项和后续未执行范围，共98项（已单独验收的新6项不重复），日志 `/tmp/conversation-logic-remaining-mysql.log`，最终 **98 passed，3239.75 秒**，`/tmp/conversation-logic-remaining-mysql.xml`，runner退出0，隔离库清理成功。旧255通过仅对应各自加载版本，不宣称全部当前代码完整回归。skip已由JUnit确认既有workspace/secret未配置的app bootstrap测试，当前未验收该真实应用启动路径。


隔离工作树补验：`160f4bca8` 拒绝未映射的嵌套 Session/未知 ledger JSON（旧实现3项错误放行，修复后负例及可信映射正例通过）；只复用已选文件清单，不新增扫描。`23bee5b7d` 将容量准入前移到枚举阶段：去重entry含目录/必要祖先，scandir逐条检查后才累积待处理路径，单文件和总字节同时早停。旧预检对参数透传与100空目录早停两项均失败，新9项通过。两个提交已独立复核，隔离树最终组合回归 **1408 passed，55.43 秒**，JUnit `/tmp/conversation-input-capacity-final.xml`，含旧CLI与新增真实枚举CLI。数据库续跑已结束，两个提交已快进合入目标工作树，提交内容与隔离测试版本相同。

隔离最终代码（含nested与entry早停）真实MySQL双服务生命周期：**1 passed，163.15 秒**，`/tmp/conversation-input-capacity-lifecycle.xml`，覆盖import/resume/reentry/verify/finalize及分享/authority，runner退出0且隔离库清理成功。该测试使用本地目录，不是NAS性能验收。


本轮完成结论：会话输入枚举边界、容量准入早停、未知嵌套Session候选阻断及canonical保留根校验已实现并小步提交，提交后均完成独立复核。最终版本单元/CLI **1408通过**，专项真实MySQL屏障与只读性 **4+2通过**，最终版本双服务完整生命周期 **1通过**，旧完整进程失败项及未执行范围新进程续跑 **98通过**。旧进程的255通过与1skip仍按各自加载版本记录，不能合称当前HEAD的一次性全量通过；既有工作区/secret真实app bootstrap仍未验收。万级四档及真实NAS性能未验收，不作生产放行结论。
