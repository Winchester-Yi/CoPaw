# 会话存储入口审计

2026-11-01，Asia/Shanghai。对象为迁移分支当前实现；本清单不是目标环境验收。
`SWE_CONVERSATION_STORAGE_AUTHORITY=mysql` 后数据库不可用即拒绝服务，不回退
旧文件。业务 workspace 与 NAS 根复用原有目录。

## 在线入口

| 入口 | SQL authority 分支 | 文件实现为何保留 / 验证 |
| --- | --- | --- |
| `_app.py` 生命周期 | `configure_runtime` 验证数据库、安装映射、精确 schema；关闭时 reset | legacy 不装配；readiness 无 DDL，`test_factory.py` / `test_factory_mysql.py` |
| `workspace/workspace.py` | 从可信 tenant/source/agent 解析 scope，要求现有目录并取得 `workspace_storage` | 正常 provision 的新空目录可接入；遗漏旧正文/非空 registry 必须迁移，不能静默收养 |
| `workspace/service_factories.py` | `storage.bind_workspace` 优先注入 SQL ChatManager、Session、archive | 仅 storage=None 的 legacy 分支构造 JsonChatRepository/ConversationArchiveStore |
| `runner.py:init_handler` | 优先 `storage.session_for` 并返回 | SafeJSONSession 创建在该 return 后，非 SQL 故障 fallback |
| `runner_integration.py` | 按 request.channel 选 Session，先持久化准入，再运行 Agent；终态提交后发终帧 | unknown 停止模型/工具推进，原 operation 对账；真实 MySQL Runner 专项 |
| `runner/api.py` | SQL ChatManager；详情/Turn/归档/context 的 `_indexed_history` 在旧 reader 前返回 | `_archive_store` 仅 legacy 使用；SQL/NAS 错误返回 unavailable，`test_api_mysql.py` |
| `routers/tracing.py` | 先解析目标用户 workspace，SQL detail/archive 分别走 `indexed.current` / `archive_page`，传入目标对象校验 | `3337db27c` 已修复 parity；SQL 分支在文件 Session reader 前返回，不使用当前登录用户 scope |
| `chat_sharing/router.py` / `service.py` | 可信 Chat.channel 选择 Session；全冷热候选、选中问答与 publication CAS | 永久 token 无 Chat 存活 JOIN；孤立分享保留原 publication scope；真实 API MySQL 测试 |
| `emit_wplus_sop_event.py` | 可信 runtime scope 调 `workspace_storage`，注入 SQL ChatManager | 仅 storage=None 才检查 chats.json 并构造 Json repo；配置错误直接失败 |
| `cli/chats_cmd.py` | 继续调用既有 /chats API，保留 Agent header | 无独立 file repo；Click→HTTP→SQL/NAS 契约测试 |
| Cron / managed receipts | 使用 workspace Runner 已注入的 Session adapter；完整 root 保留 receipts/task_run | 任务会话沿用 console Session；agent 请求进入统一 Runner wrapper，显式 channel 依请求身份选取；Cron 定义/执行库不迁移 |
| `reme_light_memory_manager.py` | 注入 `_conversation_archive`；prepare/create/recover/reset 使用 SQL façade | 旧 attach_conversation_archive 只在无绑定分支；archive_binding 原子提交完整 root/checkpoint/event/epoch |
| Hook transcript / 审批拒绝清理 | SQL 无 `_get_save_path`，transcript path 为空；清理用 execution/mutator | 私有文件路径调用有能力检查，不生成 SQL 假 JSON transcript |

## 离线与维护路径

| 路径 | 权威边界 | 验证 / 处理 |
| --- | --- | --- |
| `migration/{legacy,inventory,exporter}.py` | 明确离线源 reader 或反向导出目标 | 不作在线 fallback；ledger/journal 为旧格式兼容与回退保留 |
| `cli/conversation_storage_cmd.py` | 显式 manifest/source/target，默认 preview，apply 才写 | 七表安装、verify、backup/restore/export；recover 另需精确 owner/generation 退出证据和审计 receipt |
| `cli/memory_storage_cmd.py` | group 入口 `require_legacy_storage` | mysql authority 下拒绝 NAS-only 命令，只供离线 legacy 维护 |
| `backup/worker.py` / `memory_storage.py` | Python backup/restore preflight 拒绝 mysql authority | 完整 SQL+NAS 备份走 conversation-storage，文件 zip 不代表完整备份 |
| `backup/shell_worker.py` / `scripts/backup/*.sh` | worker 外部 IO 前拒绝 mysql；executor、直接脚本同样拒绝 | 专项验证脚本不运行、不下载、不覆盖目标；拒绝不记为成功回滚 |
| `file_governance/archive_maintenance.py` / `routers/dream_logs.py` | bundles 为保留目录，扫描不跟 symlink；resolver 重验 resolved path | 真实 bundle direct/alias 不能当 orphan 或被归档/删除；引用回收只走专用 GC |
| `app/migration.py` / `routers/agents.py` / `bootstrap_state.py` | 旧布局迁移和正常 provision 可生成空 registry | 空文件非 SQL 在线源；此次迁移不初始化新业务 workspace |
| `archive_reader.py` | 仅复用旧类纯过滤/时间解析 helper | 正文从 SQL locator/pin + BundleStore 读取，不构造 file archive store |
| `runner/session.py` / `turn_ledger.py` / repo exports | legacy 和离线工具仍导出这些类 | SQL 工厂不创建旧 reader；符号保留不等于在线旁路 |

## 发现项与回归

1. 通用治理会把真实 bundle 当 orphan，且 alias 能绕过词法目录保护。用实际
   BundleStore fixture 复现后修复两处保留目录、扫描和 resolved-path 校验；
   `test_storage_governance.py` 覆盖 direct/alias 的扫描与直接请求。
2. Python BackupWorker 已拒绝 NAS-only，Shell executor/直接脚本曾遗漏。已补
   authority 前置拒绝；`test_memory_storage_backup.py` 验证无外部 IO/脚本/目标写入。
3. 分享曾读 console Session 而非 Chat.channel，列表曾用本进程 idle 覆盖 SQL
   活动状态；API 矩阵增量包含修复与真实 HTTP 回归，以最终 XML 为证据。

原 NAS 原语、两节点双 Pod、固定旧镜像继续写和生产规模演练仍是独立环境门槛。


## 2026-11-04 性能设计重整入口

最新约束及逐入口成本见[会话数据库迁移性能设计](../../docs/superpowers/specs/2026-11-04-conversation-storage-performance-redesign.md)。
保持现有 schema/存储格式，暂不设计列表并发更新下的稳定翻页或共享快照；非必要不访问 NAS。
注意 SQL authority 不等于 SQL-only：当前列表仍会 hydrate NAS metadata。
新方案按轻量查询、正文读取、语义写入、离线维护分流，已开始分工作包实施。
checkpoint 完整遍历改为逐页引用保护；固定 high、版本复核、取消排空与 GC 正确性仍须验收。

### 已落地首个工作包：context-usage

- 计划：[context-usage SQL 读取](../../docs/superpowers/plans/2026-11-04-conversation-context-usage-sql-plan.md)。
- `runner/api.get_chat_context_usage` 先选择 indexed reader，不再预先调用完整 `mgr.get_chat`。SQL 正常路径仍有身份查询、数值查询和 metadata revision/删除复核，不创建 reader pin，不读 NAS。
- 若 SQL 缺少 `source_id` 或 `agent_id`，且有 content metadata publication，必须回退现有带 pin 的完整 Chat 读取来执行历史授权。长度超过 1024 字节的身份字符串可能因现有分拆规则存于 NAS；不能把 SQL 缺字段等同于历史字段不存在。
- 错误 owner/已有 SQL 身份冲突在回退前拒绝；NAS 回退失败返回 503，pins 按原 reader 生命周期清理。404 detail、unavailable、stale 和 legacy 文件读取保持原契约。
- 回归入口：`tests/unit/app/conversation_storage/test_context_usage.py`；真实数据库用例：`test_api_mysql.py::test_http_context_with_content_metadata_does_not_hydrate_nas`。真实 MySQL 配置缺失时只能报告 skipped。
- 旧列表、详情的重复 hydrate、Stop 和其他内部身份入口仍待各自工作包处理。

### 已落地第二个工作包：摘要查询与列表调用方

- 计划：[summary SQL 查询和调用方迁移](../../docs/superpowers/plans/2026-11-04-conversation-summary-query-plan.md)。
- 新路径为 `/chats/-/summaries` 和 `/tracing/chats/-/summaries`。不能使用 `/chats/summaries` 覆盖原详情路径：历史 Chat ID 支持任意 128 字节字符串，`summaries` 是合法旧 ID。
- `MySQLChatRepository.list_summaries` 显式读取 SQL 列、复用 active Turn 的运行状态 CASE；固定上限 200，按 `updated_at/external_chat_key` 排序，读取 limit+1。每页一条业务 SELECT，无 COUNT、NAS IO、reader pin、完整 metadata hydrate；连接池健康检查不计入该业务查询数。
- 摘要为独立 `ChatSummary/ChatSummaryPage`，只有 `display_metadata` 白名单与 typed `expert_binding`，不是完整 ChatSpec。SQL 中缺失的正文 metadata 不补读 NAS；旧完整数组、页码、v1/v2 cursor 和详情接口均保留。
- Console 初始和后续列表改读 summary，以“已加载数量”展示，不提供假 total。普通 ID、本地已解析 ID、applyChatSnapshot 都合并完整详情字段；本地未冲突草稿字段保留。
- 列表请求发出后若完整详情替换了 metadata，对象引用快照会让这些新值优先于迟到的 summary。专门覆盖 plan/goal false、清空专家绑定；普通刷新仍允许 SQL 摘要更新未被改动的字段。
- Tracing 弹窗只对选中 Session 查询摘要，遍历该 Session 的 cursor 页，保持原按最近更新排序后最后一个 Chat 胜出的映射。切换 Session/user 或关闭时，旧请求不能写回。
- 文件 repository 为新接口提供兼容投影；SQL 以可信 workspace scope、请求 user、channel 和可选 Session 条件过滤。摘要不能用作正文访问的授权凭据。
- 回归入口：`tests/unit/app/conversation_storage/test_summaries.py`、`console/src/pages/Chat/sessionApi/index.test.ts` 和 UserDetailModal 测试；专用数据库用例 `test_api_mysql.py::test_http_summaries_keep_content_metadata_on_nas_and_scope_pages`。
- 未改动：详情与归档的重复预读、Stop/Goal/SubAgent 内部身份查询、批量 NAS extent 读取、checkpoint 和 plan 容量检查。真实数据库和 Chromium 108/360 验收需独立记录。

### 2026-11-05 提交复核：列表链路后续优化

- Console 的 `getSessionList/loadMoreSessions` 每次仍并行调用 `listTaskSources`，后者读取旧 Cron 完整清单并遍历新版调度器清单。旧 `/cron/jobs` 还逐项确认绑定、刷新 next_run_at；JSON repository 会加载整份任务文件。因此 summary API 零 NAS 不能等同于整个侧栏请求链零 NAS。优先将任务存在性查询与会话翻页解耦，复用当前身份的任务快照，任务变更/显式刷新时失效；失败保留未知状态，不能当成任务删除。
- Tracing 为保持原多 Chat 映射语义，会遍历选中 Session 的全部摘要页后取最后一个 Chat。后续应提供可信 scope/user/Session 下的单对象 SQL 映射查询，按原排序的反向顺序取一条，保留相同更新时间的 external_chat_key 决胜规则，避免搬运全部摘要。文件模式继续保留兼容路径。
- 这两项是后续设计建议，本提交没有实施。继续保持现有 schema/索引/格式；在专用库上确认实际查询计划、扫描行数及请求成本，再决定实现和批量参数，不能把 LIMIT 或查询次数断言当作延迟验收。

### 2026-11-05 任务来源快照实现

- 见[Console 会话分页与任务来源解耦设计](../../docs/superpowers/specs/2026-11-05-chat-task-source-snapshot-design.md)。用户确认外部任务变更可在返回聊天页或手动刷新时感知；本页操作后确认对应来源。本工作包已实施，详见[实现与验证报告](../../docs/superpowers/reports/2026-11-05-chat-task-source-snapshot-implementation.md)，真实浏览器和部署验收尚待完成。
- 聊天页任务 hook、30 秒轮询与会话分页各有完整任务读取；legacy 的 get_job 签名快照不能消除 list_jobs 的整文件 load。方案让分页只调用 summary，任务生命周期维护一个当前身份的共享快照。
- 必须分开“失败时保留展示”和“可断言不存在的完整任务集合”。部分页、失败、取消、动作后的旧响应均不能作为删除依据；还须防 A→B→A 和不同来源同 ID。
- SessionApi 先过滤后保存会丢掉已加载记录；新设计保留原始缓存，只投影视图。mergeConcurrentSessions 的 currentOnly 还可能复活已隐藏任务，须在合并消息/本地状态后最终过滤。任务快照变化仅触发内存重算，不重查 summary 或正文。
- 运行状态轮询的全量 L+S 成本仍在，后续需单独评估轻量批量状态协议，不能把去除翻页重复查询描述为整个侧栏零 NAS。

- 排查实现从 `chatTaskSourceStore` 的 key/epoch/source revision、`useChatTaskSources` 的生命周期和 `SessionApi.getLoadedSessionView/projectSessionView` 开始。认证清除必须暂停激活，ready 后恢复；创建事件须废弃动作前在途来源请求。Sidebar 合并按身份索引，避免快照发布导致平方级扫描；共享 UI 保留隐藏会话的消息引用，unknown 恢复不重读 NAS。

### 2026-11-05 运行状态查询实现

- 见[聊天任务运行状态轻量查询设计](../../docs/superpowers/specs/2026-11-05-chat-task-runtime-query-design.md)，已实施，详见[实现与验证](../../docs/superpowers/reports/2026-11-05-chat-task-runtime-query-implementation.md)。新增双来源 `jobs/-/runtime-statuses`，原列表、详情、state 和动作接口保留。
- Scheduler 热路径从 managed repository 的 `_SELECT`/`job_view` 排查：当前 `d.*` 和 `spec_json` 是传输/解析定义的来源；新查询应显式 JOIN 标量，沿用日期和排程推导，不能只凭 state_version 判断无变化。
- Legacy 的未读、预览、暂停仍以 jobs.json 为准，即时执行状态仍在实例内 _states。新 API 不能循环调用 get_job_state 或把 SQL 镜像当完整权威；热读按已验证文件签名命中紧凑只读投影，文件变化仍整文件重读。
- 缓存须防读旧内容后标记新签名、atomic replace 同大小同 mtime、可变模型污染和旧请求覆盖新版本；批量状态不生成完整 membership，异常只确认对应来源一次。

- 实际入口为 `cronRuntime.loadRuntimeStatuses` → `ChatTaskSourceStore.readRuntime`、`runtime_snapshot.RuntimeSnapshotCache` 与 Scheduler `runtime_query.runtime_statuses`。周期只收集 `visible_in_my_tasks` 和当前打开 Chat 的任务，未请求项保留原引用；切勿恢复为对 full items 全量取 ID，否则 legacy 非本人任务的 unavailable 会反复触发完整来源确认。
- legacy 缓存不能固化创建者相关的隐式 manual 暂停；应在本次调用者视图中推导。前端长度校验与 Python 一样按 Unicode 字符数，不能用 UTF-16 `.length` 拒绝合法 emoji 摘要。

### 2026-11-05 checkpoint 契约与 plan 可读性修复

- 见[实现与验证](../../docs/superpowers/reports/2026-11-05-checkpoint-read-contracts-implementation.md)与[计划](../../docs/superpowers/plans/2026-11-05-checkpoint-read-contracts-plan.md)。固定 4096 窗口已移除：无 limit 的 checkpoint 为预算内完整读取，record-only 不查事件，prefix/page 显式 has_more/typed cursor，迁移和恢复必须正常穷尽 iterator。
- 核心入口 `checkpoint_reader.CheckpointReads`；current snapshot 固定 epoch/revision/active/high，查询按 event_last,id 续页并多查一个 locator 防重叠。每页短事务选取并通过 ReaderPins pin，事务外 NAS，取消排空后释放；active 读完即释放，不跨完整遍历持有。
- 摘要投影保留完整尾部校验，但只存 deque 最后 5 条；compact 用源消息数量的 prefix 并比较准备时 metadata；degraded 使用 record。若错误发生在页尾/耗尽阶段，不得捕获后当成功。
- full 数组默认 65536 事件与 256 MiB extent 读取字节安全阈值，可显式调整，不是生产 SLO 或堆内存上限；流式完整遍历不受累计数组事件上限。
- CommitPlan.serialize 已检查最终 UTF-8 JSON <=1 MiB；reserve 在事务/NAS 前调用。effective projection plan、compact 模型前后及 bundle 的容量闭环仍待下一包，不能将此通用 guard 当作已全部修复。
- 自动化扩大回归 865 passed、16 skipped；新增真实 MySQL 规模/并发/GC 用例因专用库缺失未执行，尚不能验收真实数据库或 NAS。

### 2026-11-05 SQL/NAS 成本优化（A/B1a 已实现，其余设计中）

- 见[专项设计](../../docs/superpowers/specs/2026-11-05-conversation-sql-nas-cost-design.md)。排查按路由重复 hydrate、projection/source 逐引用 SQL、RetentionStore 成员逐行 INSERT、BundleStore 每 extent 两次打开、写入 root 编码/解码与重复深复制展开。
- 推荐依次拆为去重复 hydrate、有界引用查询/复核、pin/dependency/Message 批量写入、同 bundle 冷读分组、进程 NAS 背压、有界缓存、detached 快照准备。已有 request-local projection cache 不等于字节有界；全量接口分批 pin 后仍需修复无界 IN。
- 本地真实 BundleStore 临时文件实验：同 bundle 64/256 个 extent 分别触发 64/256 次 inspect、128/512 次文件打开。只是调用次数，不代表 NAS 耗时或生产加速倍数。
- SQL 批次不等于减少成员行数/事务总锁；cache hit 不省当前授权/版本/pin；新 publication 首次 durable 校验不能用准备内存冒充读回，离线目标验证必须 cold。NAS 运行中 worker 完成前不得释放 pins，进程关闭需先 drain 再关 SQL。
- 首批[单次 hydrate 工作包已实现](../../docs/superpowers/reports/2026-11-05-conversation-single-hydrate-implementation.md)：四个 indexed 详情/归档入口先判断 reader，取消路由前置完整 Chat 读取，授权在 reader 内完整执行；Tracing 的 lookup 阶段测试迁为 reader 授权前后。四项禁止重复读取的测试先失败后通过，相关回归 98 passed。真实 HistoryStore 组装测试验证每请求一次 metadata hydrate，但数据库与 NAS payload 使用替身，不代表现场延迟收益。
- [引用批量化细化](../../docs/superpowers/specs/2026-11-05-conversation-reference-batching-design.md)区分原事务内的 B1a 与全量一致性/联合锁 B1b。publication 批查不能 SELECT * 带回大 plan；256 IDs 是行数上限，实际按物理连接已知 packet 预算缩小批次。B1a 已按[实现记录](../../docs/superpowers/reports/2026-11-05-conversation-reference-batching-implementation.md)完成：五种固定窄列模板、packet 预算与 256 IDs 双重切批；捕获和复核保持原逐字段检查、Message FOR SHARE 与 Turn 普通读。4,097 个 locator 在预算足够时为 17 次查询；该轮尚未实施 B1b，后续进度见下节。专用库未配置，新增两项真实 MySQL 用例跳过，不能把替身成本等同现场性能。

### 2026-11-05 B1a 提交后的剩余工作复核

- B1a 已提交 `e7422898b`，提交前扩大回归 987 passed、2 skipped，pre-commit 通过。参见[剩余工作报告](../../docs/superpowers/reports/2026-11-05-conversation-remaining-work-review.md)。
- 该轮规划的 B1b/B2 已由下节简化版实现替代，全量复核一致快照不再作为需求；后续为 C1/C2 批读与背压、C3 有界缓存、D detached 快照。compact 容量/错误终态、续租总预算和真实环境验收仍应优先闭环。
- 复核时不要把 A/B1a 当作所有接口零 NAS、所有 SQL 已批量化。Stop 的 Session 回退查找、SubAgent/分享身份预读、guard 重复锁定、池归还检查和 GC/备份扫描仍有独立优化空间。manifest 锁定驱动 26.7.0，而当前 venv 为 9.4.0；需隔离环境验收，不能用替身测试宣称上线性能达标。

### 2026-11-05 B1b / B2 简化版实现

- 见[简化读取与 SQL 批量化设计](../../docs/superpowers/specs/2026-11-05-conversation-b1b-b2-batching-design.md)。用户选择展示不追求跨批一致；此前 NAS 后 RR 快照及全量 Message 复核方案被替代，不新增展示快照或 Message 行锁。
- 新固定集合模板入口为 `collection_queries.py`，每批 256 项/64 KiB，并受实际连接 packet 缩小；binary key 按转义字节预算。覆盖 anchor suppression、boundary、Chat 删除、share selection/proof 和 prune；分享结果恢复跨批全局排序，删除冻结候选后先锁全部 Session 再锁 Chat，保留一个外层事务。
- History/Tracing/archive/answer-turn 移除 NAS 前后全量 Message 二次复核，保留 scope、payload、授权与轻量 Chat 检查；完整 Chat 复核改窄列批查普通 SELECT。写入 Source/Projection 最终来源复核继续保留。
- publication 沿用 FOR UPDATE，依赖窄列批锁，current 完整锁；commit 内锁验一次并立即复用于 dependency 安装，公开 add_dependencies 独立调用仍自行锁验，不跨事务保存锁证据。GC、Stop、fencing、UnknownCommit 保护保留。
- 普通 pin/dependency 复用 PreparedInsert，guard/fence 不变；单 pin/backup 采用 expected+1 有界窄列完整检查，dependency 大集合按 member_key 分页并由 source 排他锁协调安装/释放。多余成员、released、scope 和 backup snapshot_ref 仍校验。
- Message 按原高水位顺序批写，单事务回滚，不推算连续自增 ID；单行避免新增 prepare/时间查询。4096 单 pin/Message 与 plan 字节边界不变。推荐 S1 简化读 → S2 publication 消重 → S3 retention 批写 → S4 Message 批写 → S5 其他集合写。代码已按该顺序实施；实现与测试明细见[实现记录](../../docs/superpowers/reports/2026-11-05-conversation-simplified-batching-implementation.md)。本轮回归 1172 passed、287 skipped，任务文件 pre-commit 通过；真实数据库验收仍待专用库，SQL 次数不等于延迟验收。

### 2026-11-05 续租总预算与控制容量（历史严格租约，已由下节替代）

本节记录前一轮实现；用户随后选择软租约，本地截止和过期拒绝不再生效，以下作为历史排查依据。

- 见[实施记录](../../docs/superpowers/reports/2026-11-05-conversation-renewal-budget-implementation.md)与[计划](../../docs/superpowers/plans/2026-11-05-conversation-renewal-budget-plan.md)。入口为 `IndexedQuery.admit/_admit/_renew` → `ExecutionStore.renew` → `DatabaseConnection.control_priority/reserve_control_capacity` → `ConnectionPool.acquire`。
- 默认租约 30 秒，续租操作总预算 10 秒，周期硬截止为保守锚点后 25 秒；锚点取 reserve/成功续租尝试开始时间，不能取 SQL/归还完成时间。admission NAS 发布前已经启动 renewer，reserved 可续租但不能执行模型；过慢 admission 在 NAS/模型入口前拒绝。
- 同池动态共享一个控制位置，所有物理连接和隔离 IO 仍受 max_connections 限制。普通业务不能占用最后预留位置；单连接退化共享，已有满占和 guard 锁不被抢占，不能用预留承诺所有负载下都可续租。取消后先退出注册，连接自身仍按原隔离排空协议清理。
- SELECT 的 lease_valid 不能跨后续等待作为续命依据，UPDATE 要再次检查数据库当前租期和 owner/generation/active turn，影响行数必须为 1。超时不推测 COMMIT 成败，不重试执行；精确 terminal 对账使用控制容量且共享周期截止，外部取消不能吞掉。
- 完全阻塞 event loop 时 timer 可能迟到，必须补查单调时间；请求等待结束不等于取消抵抗的数据库 IO 已排空。真实 MySQL 的锁/过期用例已增加，仍需专用库和匹配 manifest 的驱动验收。
- 本包最终扩大回归 1011 passed、38 skipped（真实 MySQL 专用库未配置），pre-commit 通过；部署版本驱动与真实数据库验收仍未完成。

### 2026-11-05 Compact 当前流程梳理

- 2026-11-06 用户决定：Compact 容量、错误终态及摘要生成/组装优化延后到 NAS 迁移功能完成后处理；当前仅记录并提交分析，未实施修复。恢复时先复核代码与依赖版本。
- 见[当前流程报告](../../docs/superpowers/reports/2026-11-05-conversation-compaction-flow-review.md)。自动入口为 `MemoryCompactionHook`；后台 candidate 当前追加事件事实，不调用摘要模型；degraded 写引用投影但不归档在线消息；真正语义摘要走 ReMe `compact_memory`。手动 `/compact` 直接传入全部在线消息，与自动保留近期消息的选择不同。
- 检查摘要覆盖时必须追到依赖 `AsMsgHandler.format_msgs_to_str`：它可能省略所选集合内较旧的消息，而归档仍使用完整所选集合。模型前容量、输出预算与最终请求预算不能只依赖 plan serialize guard。
- 模型组装由 `ReMeInMemoryMemory.get_memory` 将长期记忆和当前摘要合成一条 user 消息，前置于原始消息；`prepend_summary=False` 仍可能注入长期记忆。ReMe 字符串摘要与 checkpoint record 的语义字段并未自动互相转换。聚焦回归 70 passed；本轮为现状分析，未修复运行时行为。

### 2026-11-05 软租约＋代次校验（当前规则）

- 见[设计](../../docs/superpowers/specs/2026-11-05-conversation-soft-lease-design.md)与[实施记录](../../docs/superpowers/reports/2026-11-05-conversation-soft-lease-implementation.md)。`lease_until` 只表达心跳新鲜度；过期既不自动 fence 也不允许抢占。执行权以 owner/generation/active Turn 和完整关联、revision/epoch、删除及终态校验为准。
- `ExecutionStore.start/renew/finish`、`PublicationStore.reserve/commit` 共用当前身份检查，原租期时间谓词和 `lease_valid` 计算列已移除。同身份可过期后续心跳、提交迟到结果；旧代次和错误 owner/Turn 仍拒绝。备份/写屏障、Stop、NAS durable 读回与 UnknownCommit 对账照旧。
- `IndexedQuery._renew` 重试规范化心跳 StorageUnavailable（明确排除 UnknownCommit），不因过期或数据库暂时不可达取消计算；首次失联 warning、恢复 info，失败按固定间隔等待，成功跳过错过的调度，不积压补发。明确冲突、完整性错误和编程错误仍取消；精确 terminal 查询独立总预算。
- 原单次 lease/3 SQL 总预算、同池一个动态控制位置与取消隔离保留；移除本地周期 25 秒停止和 admission 时间拒绝。普通业务 SQL/模型发布错误不能套用心跳重试策略。故障期间业务保存仍可能失败，恢复仍需 stopped/drained 证据；数据库 fencing 无法撤销外部工具副作用。
- 不改 DDL/NAS/HTTP 格式。旧代码仍按严格租期拒绝，部署需使用一致版本；真实 MySQL、驱动版本和跨 Pod 验收仍需专用环境。
- 当前软租约包扩大回归 1084 passed、51 skipped（专用 MySQL 库未配置），pre-commit 通过；按用户指令提交本包，真实环境仍待验收。
