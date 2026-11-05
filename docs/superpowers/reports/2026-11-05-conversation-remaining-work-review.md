# B1a 提交后：会话迁移剩余工作复核

日期：2026-11-05。代码基线：`e7422898b`。工作区：`.worktrees/conversation-mysql-nas`。

本轮提交 B1a，并对照当前源码、[整体性能设计](../specs/2026-11-04-conversation-storage-performance-redesign.md)、[SQL/NAS 成本设计](../specs/2026-11-05-conversation-sql-nas-cost-design.md)复核待办。除本报告与入口索引外，没有新增运行时改动；以下影响为静态分析，未经现场压测量化。

> 2026-11-05 后续更新：第 2 项已改为并实现软租约＋代次校验，见[当前设计](../specs/2026-11-05-conversation-soft-lease-design.md)。下文保留当时严格租约的排查证据；“过期必须拒绝写入”不再是当前规则。

> B1b/B2 后续更新：已按简化展示读取方案实现，见[实现记录](2026-11-05-conversation-simplified-batching-implementation.md)。不新增展示 RR 快照或全量 Message 复核；下文旧 B1b/B2 表格为当时分析，当前待办以末节为准。

## 已完成的边界

- checkpoint 的 record/prefix/page/full 与完整遍历已实现；通用持久化 plan 最终 UTF-8 字节检查已实现。不能由此推断 compact 和扩展内存 plan 的容量闭环也已完成。
- context usage SQL-only、summary 新接口及 Console/Tracing 调用迁移、任务来源快照与轻量运行状态查询已有实现；旧完整接口仍保留。
- A：四个 Chat/Tracing 详情/归档入口取消重复完整 Chat 预读，提交 `19b77d542`。
- B1a：原事务内 projection/source 捕获、最终引用复核、plan 引用归属改为固定窄列批查，提交 `e7422898b`。没有批量重写所有 SQL、成员 INSERT 或 NAS IO。

提交前重新执行数据库、完整会话存储单元、两组 Tracing 路由与 B1a MySQL 模块：**987 passed、2 skipped，54.47 秒**；本包 pre-commit 通过。两项跳过仍是未配置专用库，不能算数据库验收通过。该命令不包括 checkpoint 的 16 项专用库验收。

## 发布前应优先闭环

### 1. Compact 全链路容量准入与错误终态

证据：`archive_runtime.py:_source_rows` 仍拒绝超过 4096 个源 occurrence；bundle 另有 4096 blocks、4 MiB 索引、256 MiB 文件、8 MiB 单 extent 非流式读取边界；持久化 plan 为 1 MiB。`projection_publication.py:effective_recovery_plan` 在读取 projection 后扩展 messages/prune；通用 serialize guard 不覆盖这个内存 plan 的完整资源准入。

`agents/hooks/memory_compaction.py:__call__` 当前在调用摘要后持久化 compact，最外层捕获 Exception 记日志后返回。因此存储容量拒绝可能发生在已消耗模型调用之后；异常分支本身也不保证阻止后续超上下文调用。这里是缺少统一契约，并非断言任意 compaction 异常都必须终止会话。

处理：生成候选、调用摘要前按源数量、确定性序列化开销及保守输出预算准入；摘要后按最终 plan/envelope/blocks/index/extents 再校验。暂保留一次原子 compact，无法安全提交时返回可观测的明确错误，并进入与当前上下文预算一致的终态/降级决策。必须保留工具交互完整前缀、CAS、未知提交对账，不能只记录失败后继续超预算模型调用。

退出条件：每类实际容量的边界测试；拒绝发生在昂贵步骤前；摘要超预算、取消、revision 冲突、提交未知均不产生部分安装或错误成功。没有 SLO 时先用存储协议的确定性边界，不编造吞吐阈值。

### 2. 续租的总 deadline 与控制容量

证据：`execution.py` 默认 lease 为 30 秒；`runner_integration.py:_renew` 每 10 秒才进入续租；`database/config.py` 默认 acquire_timeout 为 30 秒，之后还可能遇到 guard/行锁与 SQL 等待。当前 renew 路径没有覆盖整个操作的总 deadline。

影响：默认配置不能证明满池时健康执行仍能在租约到期前续租。此项关乎任务可用性，不只是平均延迟；旧 authority 在被明确撤销或代次变化后必须拒绝写入；软租约时间过期本身不再拒绝。

处理：统一剩余租期、池排队、锁等待、SQL 与清理的总预算，设计受总连接数约束的控制请求容量/优先级；是否拆池须依据测量，不能无上限加连接。验证满池、慢 SQL、同 scope guard 竞争、事件循环延迟与停止/结算并发。

### 3. 真实环境验收与版本一致性

当前再次核实：`pyproject.toml` 锁定 mysql-connector-python **26.7.0**，共享 venv 安装 **9.4.0**。本轮没有擅自升级共享环境。应在隔离且匹配部署的环境验证驱动、packet、事务、取消和故障语义。

B1a 两项目标库用例尚未执行；真实小 packet 缩批、writer/GC 交错、EXPLAIN/扫描与锁等待仍须验收或补充。checkpoint 的实际 4095/4096/4097/8193/16385、共享/独立 active、GC/取消测试也待专用环境运行。NAS 冷热、慢 IO、跨 Pod、滚动退出、迁移/备份恢复演练未完成。此前前端改动还欠真实 Chromium 108/360、当前浏览器和 hideMenu=true 验收。

## SQL/NAS 优化主线

| 顺序 | 待处理项及当前证据 | 实施要点与退出条件 |
| --- | --- | --- |
| B1b | `history._revalidate_rows`、`chat_repository._revalidate` 仍构造无界 IN；anchor/source-key、share proof、批量删除等也有集合查询/更新；`SourceMessages.lock_publications` 仍逐 publication SELECT * FOR UPDATE | 分开捕获、NAS 后完整复核和提交锁阶段；多批读取不能机械拆循环而混入不同快照。短只读一致快照、最终当前 head/权限复核、baseline 恢复与原错误须明确；publication 依赖窄列分批，current 保留 _matches 所需数据。UPDATE 仍在原原子事务，真实双连接验证锁顺序及 GC/fence。 |
| B2 | `RetentionStore.pin/add_dependencies` 逐成员 `_insert`；`_validate_members` 仍一次返回全部成员；`install_messages` 逐 Message INSERT；projection cache refresh/removed_occurrences 仍逐 block 点查 locator | 复用 PreparedInsert，行数/字节/参数/packet 同时有界；保留数据库时间、完整成员集合和额外成员拒绝、幂等及 released tombstone。Message 按原顺序分配高水位，不推算连续自增 ID；提交后批取精确 occurrence 映射。验证首中末批失败全回滚。 |
| C1 | `BundleStore.iter_extent` 每 extent 先 inspect 打开文件，再打开读正文；history/source/projection 等按 extent 调用；目前没有 read_extents | 在一个安全 fd 上校验 header/index、读取预算内必要 extents；按 bundle/窗口分组、去重物理读取，保留业务 occurrence。验证路径/symlink、长度/坏尾部、extent 越界、跨窗口、冷读与取消；不借分组预读无关正文。 |
| C2 | `PinBatch.read` 与 `PublicationStore._nas_io` 用共享 asyncio.to_thread；没有 NAS 专用的有界任务/字节准入 | 进程级 worker、queued task/bytes 同时有界，给读写及后台任务清晰的排队与关闭协议；queued 取消和 running drain 分开。实际 IO 排空后才释放 pins，关闭 SQL 前仍能做清理；仅限制线程数或在等待外套 timeout 不足以解决问题。与 C1 同期设计，独立提交。 |
| C3 | `projection_cache` 只有 request-local 单项映射，但该项按整个当前状态增长且反复 deepcopy，没有字节预算；未见跨请求不可变正文 LRU | 冷路径先正确，再做有界 LRU/超大绕过和 execution-local 映射；键含完整 scope/storage/不可变 locator，返回对象防污染。warm hit 不能跳过当前 SQL、授权、pin；离线校验和新 publication 首次 durable 读回必须 cold。 |
| D | `runner_integration.root_provider`、`semantic_runtime._root/_publish` 重复 deepcopy；`session_adapter._save_state` 编码完整 root，projection 准备又解析/编码 | 明确 detached 快照所有权，由稳定状态直接生成原格式 projection；先测复制/编解码次数和 event-loop lag，禁止将活动可变 Agent 对象直接交给线程。保留未知模块、工具/审批/compact 语义边界、持久化读回与未知提交重启恢复。完整引用清单和依赖成员仍有 O(N) 下界。 |

## 其他入口与共享资源

这些应单列工作包，避免在 B1b 中混入所有优化：

- **身份查询收尾**：`console._resolve_console_stop_chat_id` 在只有 session_id 时仍 list_chats 全量 hydrate，再筛选和逐项检查活跃身份；SubAgent 和部分分享路由仍完整 get_chat。按实际授权字段设计轻量 scoped lookup，保留多 Chat 歧义、source 及历史兼容语义，不能默认省略 NAS 中授权依赖的字段。
- **guard 热点**：`ensure_guard` 做 upsert 和 FOR UPDATE，`lock_guard` 再查同一行；同 scope 不同 Session 的受保护写事务仍争同一 guard。先测等待、消除同事务重复检查并缩短持锁时间；不能直接删 guard 或改成 FOR SHARE，首次初始化、backup fence 可见性与锁顺序须一起设计。
- **连接检查往返**：获取租约 ping，归还 get_autocommit + session baseline。B1a 只是在已有事务内复用租约，未优化独立 CRUD 的检查往返。评估短 SQL 阶段复用、归还检查合并和探活策略，保留连接污染/失效隔离，不跨 NAS 长占连接。
- **维护成本**：`GC.report` 仍逐候选计算多类保护；backup export/restore 元数据对比部分 fetch_all 累计完整结果。可有界预筛/扫描，但 mark/reclaim 要重新验证，旧备份格式仍可能要求最终累计；不能把报告当删除授权，也不能降低完整恢复验证。
- **上线基线**：记录每入口 SQL/prepare/检查/池等待、扫描/返回行数、锁时长、pin/dependency 行数、NAS open/inspect/read/write/fsync 与字节、队列峰值、RSS 和 event-loop lag。当前没有业务 SLO，不承诺未经测量的 P95/P99 或加速倍数。

## 建议执行顺序

如果继续当前 SQL 成本工作，下一步是 **B1b → B2 → C1/C2 → C3 → D**，每包独立证明成本和兼容语义；C2 不能拖到缓存之后。

发布前同时优先闭环 compact 容量/终态与续租可用性，并建立匹配锁定驱动的真实环境证据。summary、checkpoint、A/B1a 不需要推倒重做；列表并发稳定翻页仍按用户决定暂不纳入，继续保持既有 schema、索引和存储格式。


## B1b/B2 完成后的当前待办

本包已完成展示读取简化、固定集合查询、publication 同事务锁复用，以及 pin/dependency/Message 批写。checkpoint 四种读取契约、通用 plan 字节 guard 和软租约＋代次校验均已实现，不重复列为开发待办。没有引入 schema、索引或 NAS 格式变更。

| 优先级 | 待办 | 处理重点 |
| --- | --- | --- |
| 高 | Compact / effective plan 全链路容量与错误终态 | 模型前预检源数量、plan/envelope、bundle/index/extent 预算，模型后最终复核；容量失败应可观测并阻止继续超预算调用，保留原子提交及 unknown 对账。通用 serialize guard 不能替代此闭环。 |
| 高 | 真实环境与迁移验收 | 专用 MySQL、匹配部署的驱动、EXPLAIN/扫描/锁等待、小 packet、批写回滚、GC/备份交错、checkpoint 边界、慢 NAS/跨 Pod/退出，以及迁移/备份恢复演练；此前 Console 仍需真实 Chromium 108/360 和 hideMenu 验收。当前 1172 passed、287 skipped，真实库验收未完成。 |
| 中高 | C1：同 bundle 批读 | 在预算内复用一次安全打开和 index 校验，按 bundle/窗口读取必要 extents；物理去重保留业务 occurrence，不额外预读无关正文。 |
| 中高 | C2：进程级 NAS 背压 | 有界 worker、队列和在途字节；等待不占 SQL 事务；取消及停机须先 drain 实际 IO、释放 pins，再关闭 SQL。与 C1 分包但衔接推进。 |
| 中 | C3：有界缓存 | 冷路径优化后增加按字节限制的缓存；命中仍做当前授权、版本与 pin 检查，离线验证强制 cold，防可变返回值污染。 |
| 中 | D：写入快照准备 | 明确私有快照所有权，减少重复深复制、编码/解码和旧内容读取；保留未知模块及 durable NAS 读回证据。依赖容量校验和读取接口稳定。 |
| 较低 | 剩余 SQL 点查与内部预读 | 旧无 proof 分享逐 key 查询、历史 projection locator 点查、Stop 回退身份查询、SubAgent/分享预读、guard/连接归还检查等；先测量热点再独立优化。 |

建议下一开发包先处理 Compact 容量与错误终态，同时准备专用环境验收；随后按 C1 → C2 → C3 → D 推进。继续遵守不改 schema/存储格式、非必要减少 NAS 交互、保留旧接口兼容、不增加展示跨批一致性设计的约束。
