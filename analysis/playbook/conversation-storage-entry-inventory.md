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

### 2026-11-05 SQL/NAS 成本优化（A 已实现，其余设计中）

- 见[专项设计](../../docs/superpowers/specs/2026-11-05-conversation-sql-nas-cost-design.md)。排查按路由重复 hydrate、projection/source 逐引用 SQL、RetentionStore 成员逐行 INSERT、BundleStore 每 extent 两次打开、写入 root 编码/解码与重复深复制展开。
- 推荐依次拆为去重复 hydrate、有界引用查询/复核、pin/dependency/Message 批量写入、同 bundle 冷读分组、进程 NAS 背压、有界缓存、detached 快照准备。已有 request-local projection cache 不等于字节有界；全量接口分批 pin 后仍需修复无界 IN。
- 本地真实 BundleStore 临时文件实验：同 bundle 64/256 个 extent 分别触发 64/256 次 inspect、128/512 次文件打开。只是调用次数，不代表 NAS 耗时或生产加速倍数。
- SQL 批次不等于减少成员行数/事务总锁；cache hit 不省当前授权/版本/pin；新 publication 首次 durable 校验不能用准备内存冒充读回，离线目标验证必须 cold。NAS 运行中 worker 完成前不得释放 pins，进程关闭需先 drain 再关 SQL。
- 首批[单次 hydrate 工作包已实现](../../docs/superpowers/reports/2026-11-05-conversation-single-hydrate-implementation.md)：四个 indexed 详情/归档入口先判断 reader，取消路由前置完整 Chat 读取，授权在 reader 内完整执行；Tracing 的 lookup 阶段测试迁为 reader 授权前后。四项禁止重复读取的测试先失败后通过，相关回归 98 passed。真实 HistoryStore 组装测试验证每请求一次 metadata hydrate，但数据库与 NAS payload 使用替身，不代表现场延迟收益。
- [引用批量化细化](../../docs/superpowers/specs/2026-11-05-conversation-reference-batching-design.md)区分原事务内的 B1a 与全量一致性/联合锁 B1b。publication 批查不能 SELECT * 带回大 plan；256 IDs 是行数上限，实际按物理连接已知 packet 预算缩小批次。两包均未实现。
