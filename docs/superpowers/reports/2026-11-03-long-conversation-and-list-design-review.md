# 长会话正确性与轻量会话列表：实现分析及后续方案草案

> **2026-11-04 合并后复核：** 本文保留原基线分析与证据；当前实施建议以[合并后重新分析](2026-11-04-long-conversation-and-list-reanalysis.md)为准。新增确认 plan 写入/解析字节限制不对称，并补充迁移/备份调用方、Tracing 兼容和列表快照的资源及失效边界。

日期：2026-11-03。基线：`436a105056ecb9ca3c692958a0b914859584c2aa`，worktree：`.worktrees/conversation-mysql-nas`。
本轮仅分析和设计，不修改运行时代码，不连接业务库，不执行迁移或部署。

用户已确认：列表必须保留最近更新排序；翻页期间其他会话更新不能造成重复或漏项，采用有期限的列表快照。
以下容量值、接口命名和 compact 策略是待实施评审的建议，不代表现有能力或已经通过验收。

## 1. 结论及优先顺序

1. 先关闭 checkpoint 的截断与 pin 边界缺陷，再处理 compact 的上游容量契约。这些属于正确性问题。
2. 保留现有 HTTP 的数组、页码、旧 cursor、完整 metadata 和详情语义；新增独立的轻量列表接口，让 Console 和内部查询逐步迁移。
3. 新列表通过 MySQL 展示字段和共享 MySQL 列表快照分页，不进行 NAS IO、不创建 publication reader pins。
4. 保留的全量接口必须限制单条 SQL、聚合条数和返回字节，超限显式失败；不能返回一个看似完整的截断结果。

不建议只调大 4096，也不建议直接让现有 `GET /chats` 丢弃 content metadata：前者没有解决快照与资源生命周期，后者改变现有成功响应语义。

## 2. 当前实现核查

### 2.1 已有分批修复的实际范围

`retention.py` 的 `MAX_PIN_BATCH=4096` 限制一次 pin 的去重 publication ID 数量，不是全会话消息上限。
提交 `a11132742` 已引入 `ReaderPins`，`projection_publication._existing()` 与 `projection_references.snapshot_projection()` 已使用它；`add_dependencies()` 也已分批锁 publication。
因此旧审计 R2 的两个 projection 入口已有修复，不能继续笼统描述成“projection 仍一次 pin 全量”。

但这没有覆盖所有读取入口：

| 路径 | 当前行为 | 剩余问题 |
| --- | --- | --- |
| recovery projection 发布、恢复 | `ReaderPins` 分批，依赖分批加锁 | 真实测试只把批次阈值改成 2；未验证实际 4096 边界 |
| Chat repository `_rows` | content metadata 的引用分批 pin | 仍逐 Chat NAS hydrate；`_revalidate` 单个无界 IN |
| HistoryStore `_pins` | pin 分批 | `_revalidate_rows`、anchor 去重等仍有无界 IN；current 全量正文无聚合预算 |
| ArchiveReader.checkpoint | 单次 pin，SQL 固定 LIMIT 4096 | 独立 active checkpoint 额外占一引用；完整读默认截断 |
| archive_runtime._source_rows | 单次 pin，显式消息数 <=4096 | 只在底层抛 ValueError；没有上游调度和用户处理契约 |
| source_messages.snapshot_sources | 单次 pin | 受 plan 数量限制；上游引入分段前须统一生命周期，不能漏改 |

`ReaderPins` 保留所有批次至 IO 排空的思路可复用。新调用点需在选择 locator 前进入 try/finally，覆盖获取中途失败、事务提交失败、NAS 异常和取消，不仅覆盖 hydrate 之后。

### 2.2 checkpoint 的两个不同错误

`archive_reader.py:checkpoint(chat_id, limit=4096)` 同时把 limit 用作事件窗口，又在 SQL 中写死 `LIMIT 4096`。
`archive_runtime.read_checkpoint_state()` 不传 limit，名义上完整，实际最多返回 watermark 后 4096 个事件。
它的完整性检查目标同样是 `min(high, watermark+limit)`，所以超过该窗口不会触发“未读完整”的错误。

如果当前 checkpoint 与后续事件来自不同 publications，4096 个事件 publication + 1 个 checkpoint publication 会在 NAS 读取前超过 pin 上限。
事件数、publication 数、消息数三个维度不能混为一谈；一 publication 可以包含多个事件。

`read_checkpoint_prefix()` 验证 limit <=4096，随后复用同一函数；`read_checkpoint_record()` 则为了取 record 先读完整 state，造成不必要的事件读取和同一边界暴露。
读取后的复核检查 Chat/session/epoch/checkpoint_revision，不比较 event_high。追加事件可以属于快照之后，但必须明确返回的 high，不能把“截至捕获 high 的完整读”与“实时读到最新”混用。

### 2.3 compact 不是仅有一处 4096

调用链：`MemoryCompactionHook` → checkpoint candidate 或 ReMe fallback → `ArchiveBinding.compact` → `MySQLArchiveStore.compact` → immutable bundle + SQL publication commit。

底层 `_source_rows()` 对超过 4096 个 source occurrence 直接抛 ValueError。precompaction 把 `len(messages)` 交给有界 prefix，也会提前触发同类边界。
Hook 最外层捕获普通 Exception 后只记日志并返回，随后 reasoning 可能仍携带未压缩的超预算上下文。不能把这种行为当作明确的容量处理策略。

容量还有：CommitPlan 的 message/prune/result 数量、projection additions/pruned 数量（均有 4096 检查）、plan 解析字节预算 1 MiB、bundle block 数 4096、bundle 总长 256 MiB、单次 read extent 8 MiB、索引字节预算。
compact 的 source locator 可复用旧 NAS 正文，不意味着每个 source 都新增一个 bundle block；须根据最终有效 plan 和 projection 实际计算，不能统一用“消息数减几个预留 block”推算。

现有 `ArchiveBinding.compact()` 在确认持久化成功之后才替换 memory 与 execution state，这个原子性边界应保留。

### 2.4 列表和详情实际没有分开

`MySQLChatRepository._rows()`：MySQL 选择 Chat → 批量 pin content metadata publications → `_hydrate()` 每项 `read_extent` → `_revalidate()`。
所有 list/filter、OFFSET 分页、cursor 分页和单项 get 共用这条链。写 metadata 时即使 content 为空也发布 content_meta block，因此仅含轻量字段的 Chat 也可能触发 NAS 往返。

数据库已具备标题、身份、时间、轻量 metadata、typed expert binding、状态投影等字段。
`LIGHT_METADATA_TYPES` 已包含 plan/goal/session_kind/task_job_id/owner/creator/source/agent 等；未知字段、类型不匹配和过长字符串会保留在 NAS。
新 summary 不能假装拥有完整 meta；对不满足轻量类型约束的旧值，应按 summary 契约表示缺失，详情仍恢复原值。

`GET /chats` 现在有三种成功响应：

| 请求 | 现有契约 |
| --- | --- |
| 无分页参数 | `list[ChatSpec]`，完整 meta，registry_order 顺序 |
| page + page_size | `ChatPage`，updated_at 降序、Chat ID 降序，OFFSET |
| cursor + page_size，首屏 cursor 为空串 | `ChatPage`；v2 按 updated_at，v1 继续 created_at 排序 |

旧 cursor 是 Base64 JSON，不绑定 scope/过滤条件，也没有过期语义；旧接口文档明确写的是 live best-effort。
仅增加同时间 ID tie-break 或首屏时间 high，不能阻止 updated_at 改变后跨越分页边界。

`GET /chats/{id}` 返回 `ChatHistory`，其中 `chat` 已提供完整 ChatSpec；`/{id}/history` 是已有的归档正文分页。
现有详情入口先调用 manager.get_chat 读取完整 metadata，随后 HistoryStore 又 hydrate，有重复读取空间。鉴权所需的 typed 身份/展示字段应先查 MySQL，再进入正文读取。

Console 已有 cursor 加载和详情按需读取，但会把列表 meta 保存为 Session meta，部分详情转换继续用 fromList.meta。因此迁移 summary 时必须使用详情的 `history.chat.meta` 替换完整 metadata，不能把 summary 当完整对象覆盖保存。
Tracing 的 `GET /tracing/chats` 仍是完整数组；Console stop 的按 session 寻址仍会全量 list。只替换侧边栏接口不能消除这些内部全量读取。

## 3. 长会话读取契约

### 3.1 分开四种读取

| 内部 API | 明确语义 |
| --- | --- |
| `read_checkpoint_record(chat, snapshot)` | 只读该快照 record；不读取事件 |
| `read_checkpoint_prefix(chat, limit)` | record 之后最多 limit 个事件；返回捕获 high、read_through、has_more |
| `read_checkpoint_page(chat, cursor, limit)` | 在同一 epoch/revision/high 下继续；游标推进使用 event sequence，不是 publication ID |
| `read_checkpoint_state(chat)` | 返回捕获 high 之前所有未应用事件；有聚合预算，完整成功或明确失败 |

可给现有 state 结构添加带默认值的内部快照字段，保留现有 record/events/current_epoch 使用方式。不能要求旧调用方猜 events 是否已截断。
prefix 的 limit 可继续限制 1..4096，但这是单页事件上限。完整读取不再隐式传 4096，而是在内部迭代分页或固定 SQL 批次。

### 3.2 快照和 pin 协议

1. 在一致数据库快照中捕获 Chat/session、epoch、checkpoint_revision、active publication、event_high。
2. 按事件范围分批选 locator，校验范围没有缺口或重叠；publication 可能跨越页边界，正文可完整校验再按 sequence 截取。
3. 所有需要读取的 publication ID 去重后分批 pin，包含 active checkpoint；每批 <= MAX_PIN_BATCH。locator 选择与 pin 属于同一外层事务，事务结束后才做 NAS IO。
4. 使用统一 ReaderPins 生命周期保护全部批次。失败/取消时先等待所有 NAS worker 排空，再释放所有批次；没有执行 NAS IO 时也覆盖获取阶段的清理。
5. 返回前复核身份、epoch、checkpoint_revision/active locator；改变则 Conflict。捕获 high 后的正常 append 不要求此次读追到新 high。

完整读取可以分批 SQL，但仍持有全部 pins 直到完成，聚合内存/事务时间因此必须有预算；“SQL 分批”不等于可以无界读取。
跨 HTTP/内部调用的分页不长期持有 reader pins：cursor 绑定 epoch/revision/high，每次页内重新选 locator+pin；checkpoint 或 reset 改变时明确过期/冲突，要求重启读取。
不能在 locator 与 pin 之间另开事务，或读取 NAS 前先释放早期批次。

### 3.3 容量与错误

单页事件数、单批 SQL ID 数、完整读取事件总数/累计正文解码字节/耗时分别定义，禁止共用一个 4096。
建议首版完整 checkpoint budget：100,000 个事件、64 MiB 解码前正文总量；page 默认 512、上限 4096。它们是待真实规模测试校准的运维上限，不是正确性边界。
超预算返回 typed `ConversationCapacityExceeded`，带 operation、reason、observed、allowed 和建议的 paged API；不返回 partial state，不伪装空列表或数据损坏。
单个 extent 的限制必须在写入前预检，避免发布“允许写但以后读不出”的事件/恢复根。

## 4. 单次 compact：首版选择显式限制，后续再做安全分段

有三个选择：

| 选择 | 收益 | 代价 / 判断 |
| --- | --- | --- |
| 只扩大底层阈值 | 改动少 | 仍会碰 plan、bytes、block 和 IO 上限，不采用 |
| 上游有界 compact + 明确阻断 | 保留当前单次原子提交，修复最小 | 超大请求需明确处理；首版推荐 |
| 多次安全分段 compact | 自动完成超长前缀压缩 | 需要逐段摘要、候选、recovery、恢复协议；单独后续阶段 |

首版在 Hook 选择候选之后、创建 candidate/运行模型摘要之前检查预算；store 入口保留相同检查，防止其他调用者绕过。
容量检查覆盖 source occurrence 数、有效 plan message/prune 数、实际序列化字节、bundle 和可读 extent 预算。摘要完成后再作最终预检，之后才 reserve/write/commit。
共享预算定义与错误类型，底层约束继续保留作为最后防线，不靠捕获底层 ValueError 分类业务原因。

超限时：

- 不创建半个 candidate，不移动 event watermark，不删除在线消息，不覆盖 summary，不声称压缩成功。
- 对自动 pre-reasoning 路径，发送结构化 `compaction_capacity_exceeded` 状态并终止这次 reasoning 的模型调用；提示新建会话或管理员处理超大前缀。仅 warning 然后继续调用超预算模型不可接受。
- 接入现有 turn terminal/error 结算流程，HTTP/SSE 使用现有终态封装增加稳定 code，不能在流已建立后另改 HTTP 状态。
- 对尚未开始流的新增管理操作，使用明确 422 容量错误；正常 compact 成功响应保持原状。
- 记录 reason、source_count、distinct_publications、plan_bytes、允许值与 scope/request/operation 标识，不记录正文。

如果后续选择自动分段，不能把同一批 messages 简单切成几个 store.compact 调用：每段必须是完整交互前缀，不能拆 tool_use/tool_result；基于上一段已确认 summary/root/revision 重新计算下一段；candidate 必须重新绑定；UnknownCommit 先对账，失败只保留已提交段的进度。不能让预先针对全部消息生成的摘要在第一段就引用尚未归档的后续内容。
即使 <=4096 条的一个完整工具交互仍超过字节预算，也必须显式拒绝，不能破坏事务边界硬切。

## 5. 轻量列表与外部 HTTP 兼容方案

### 5.1 新增接口，保留旧成功契约

建议新建 `GET /chats/summaries?page_size=50&cursor=...`，返回独立 `ChatSummaryPage`。
必须在 `/{chat_id}` 动态路由之前注册；命名和 schema 应更新 OpenAPI、SDK 与 Console 类型。

Summary 字段：id/name/session_id/user_id/channel/created_at/updated_at/status、明确命名的 `display_meta`（白名单轻量字段）、typed expert_binding、metadata_revision。
`display_meta` 与完整 `meta` 区分，防止旧客户端把缺省字段误判为删除。仅 SELECT 需要的数据库列，不 `SELECT c.*`，不读 NAS、不 pin，也不查正文缓存。

需要仅 metadata 的客户端可新增 `GET /chats/{id}/metadata -> ChatSpec`，按现有 pin/hydrate/复核协议恢复完整 meta；进入消息详情继续用已有 `GET /chats/{id}`，不强制额外请求 metadata。
详情可以去掉重复的 get_chat NAS hydrate，先读轻量身份鉴权，再由 HistoryStore 一次性恢复；完整结果字段、过滤/隐藏消息与归档边界语义均保持。

旧接口保持：无参数数组不变；page + page_size 不变；v1/v2 cursor 解码和排序不变；原参数组合的 422 行为不变；旧 meta 未知字段不丢失。
旧 offset/cursor 标记 legacy 并保留 best-effort 文档。严格稳定分页是新接口承诺，不能在同一旧 cursor 版本上静默升级排序或过期语义。
Tracing 同步增加目标用户 summary 接口，保留原 `/tracing/chats` 数组和原授权逻辑。

### 5.2 严格稳定分页：MySQL 快照

本方案按用户确认采用快照，而非仅 updated_at keyset。

新增独立的短期 list snapshot 表及 item 表，与会话 publication/reader retention 分离：

- header：随机 snapshot ID、service_unit/tenant/source/agent、认证主体、规范化过滤条件 hash、created/expires、total、state。
- item：snapshot_id、ordinal、chat_id、冻结的轻量 summary。主键 `(snapshot_id,ordinal)`，唯一 `(snapshot_id,chat_id)`。
- 首屏在一个有界 REPEATABLE READ 快照中按 `(updated_at DESC, external_chat_key DESC)` 生成 items；分批 INSERT，不让每批看到不同的源视图。仅在全部完成后设 ready 并返回第一页，失败不暴露半成品。
- 不锁会话/执行权，不包含 NAS locator，不给 publication 加 pin；快照保存展示值，不影响正文 GC。
- 后续只按 ordinal 做 keyset，不随 Chat.updated_at 更新改变位置，既有成员更新不会重复或消失，新建会话在刷新快照后出现。
- status 为快照捕获值；Console 用现有运行状态事件/轮询更新，不为了“实时 status”重新排序。标题和时间也冻结，刷新后更新。

cursor v3 包含 snapshot_id、after_ordinal、version、expire，使用持久共享密钥 HMAC，绑定 scope/主体/filter；每页仍重新做当前授权。过滤不匹配、篡改返回 422，过期返回 410，跨主体按不可访问处理。
不能使用 Pod 本地内存或每 Pod 临时签名密钥，否则跨实例翻页/重启会失效。快照 TTL 只清理这类可再生列表结果，不用于推断 publication reader 已死亡。

建议 TTL 10 分钟、page_size 1..100、每主体最多 3 个有效快照、每快照最多 100,000 项/64 MiB 展示 payload；所有值可运维调整并通过首屏预检阻断，不能截断。
源扫描采用 keyset/分批构造，但必须保留同一数据库一致视图；首屏构造成本随成员总量增长，这是选择严格快照排序的明确代价。超限不自动回退 live pagination；后续可通过用户确认的过滤条件缩小集合。
列表快照过期、创建失败、单主体配额、总存储配额都有指标；清理使用按 expiry 分批删除，避免长事务。

删除与权限撤回优先于冻结展示：后续页批量复核 Chat 未删除和当前授权，跳过失效项，继续扫描补满 page_size；cursor 按“扫描到的最后 ordinal”推进。
`total` 定义为首屏捕获成员数，删除后可大于最终可见条数；`has_more/next_cursor` 按实际未扫描范围定义。保证对象仍存在且仍有权限的成员不因并发更新重漏，不承诺继续展示已删除/已撤权对象。
此例外需写进新接口契约和 Console 去重/刷新行为，不能让快照绕过删除和权限边界。

### 5.3 内部调用与 Console 迁移

1. repository/manager 新增 summary 查询和精确身份查询，保留现有完整 ChatSpec 方法。
2. Console 侧边栏只调用 summary；选中后使用 `ChatHistory.chat` 合并完整 metadata，直链加载无需先全量 list。
3. summary 使用独立类型，禁止作为完整 ChatSpec 传回 upsert；维护本地草稿与历史完整 meta 的合并规则。
4. Console stop、按 session 找 Chat、get_chat_id_by_session 等改为 scoped SQL 精确查询，避免为了 ID/name/status 拉全库及 NAS。保留多 Chat 歧义判断，不能随便取第一项。
5. 运营看板迁移 summary；CLI、导出、分享等真正依赖完整 metadata 的调用继续走完整接口或有界迭代器。

### 5.4 保留全量接口的容量边界

把“兼容数组返回”与“无界 SQL”分开：旧接口仍返回数组，但内部按固定批次选 locator、pin、读取和复核，最终成功返回前检查累计预算。
优先修复 ChatRepository._revalidate、HistoryStore._revalidate_rows、anchor 去重与 full tracing 相关 IN；不能只批量 pin。
返回前的最终复核也需要统一数据库读视图，或逐批 FOR SHARE 后锁至短事务完成，避免每批独立视图被误称为全量同一时刻复核。NAS IO 不放进锁事务。
全量正文仍按原 current/tracing 语义，不能静默改成只返回一页归档内容。

建议 legacy 数组预算 10,000 Chat/64 MiB 响应，current/tracing 单次正文预算 64 MiB；这是初始配置建议，发布前必须盘点已有成功用量并调整，使已知存量调用不被意外拒绝。
超限新增可识别的 `conversation_capacity_exceeded` 错误及迁移接口提示；现有 storage 故障的错误映射保持原样。新增容量错误必须在通用 StorageError->503 之前处理。
这属于错误域扩展，不是完全无行为变化；SDK/外部使用方须获得迁移说明。不能把硬上限说成完全向后兼容，更不能为了维持 200 返回截断数组。
离线全量导出改用有界迭代器/文件产物，不受在线数组容量所束缚；全量一致性备份仍沿用原专门协议，不以列表快照代替备份。

## 6. 验证与验收矩阵

### 6.1 真实 MySQL + 真实本地 NAS 文件，不能调小阈值替代

- checkpoint：4,095 / 4,096 / 4,097 / 8,193 / 16,385 个未应用事件；分别无 active checkpoint、有独立 active checkpoint publication。断言完整读到捕获 high、prefix/page 返回准确、拼页无重漏。
- 事件与 publication 分别构造：一事件一 publication、多事件一 publication、publication 横跨页边界；检查 pin 依据去重 publications 而非 events。
- 通过 production publication API 构造代表性数据；大规模 fixture 可批量安装等价合法历史以控制时长，但需明确区分它与端到端逐条追加证据，两类均保留。
- 新 adapter/新请求/独立进程读取，覆盖 Cron task history 增长与缓存未命中；大规模 projection/dependency 也保留真实阈值验证。
- 并发 checkpoint 切换/reset/删除/GC、后批 pin 失败、SQL commit 失败、NAS 损坏、取消与 IO 延迟；确认返回 Conflict/完整数据或 typed capacity，绝无截断成功，活跃 IO 期间 pin 不提前释放。
- compact：4095/4096 按实际 plan/bytes 允许成功或预检容量错误；4097 首版上游明确拒绝且未运行摘要模型/未改变 root、revision、watermark、memory；巨大单消息、plan 1 MiB、extent 8 MiB、bundle 限制分别验证。
- 自动 Hook 验证容量拒绝会结算错误且不继续 reasoning；故障日志不作为用户可观测结果的替代。

### 6.2 列表与兼容性

- NAS spy 在 summary 请求时直接抛异常：列表仍成功，NAS 调用和 reader pins 都为零。
- 同 updated_at 的多 Chat、第一页后跨页更新、新建、删除、改名、权限变化，完整遍历仍无更新导致的重漏；冻结字段符合快照契约。
- 跨 Pod/独立 DB connection 继续 cursor，服务重启后继续；同主体并发刷新快照互不覆盖；TTL、配额、篡改和 filter/scope 重用失败符合约定。
- 旧无参数组、page 模式、v1/v2 cursor、空 cursor、错误参数、Unicode ID、空 user/channel、未知 metadata 和 typed expert binding 的响应快照保持。
- 新 summary 不等于完整 meta；Console 详情合并、metadata 更新回写、直链、草稿、task/plan/goal、专家绑定、运营看板和 stop 寻址均回归。
- 全量复核 SQL 每批 bounded，超过容量不返回 200 partial；已有 current/history/share/answer-turn 隐藏消息及授权规则不变。
- 真实目标 MySQL/TDSQL EXPLAIN；测首屏快照构造、后续页 p95/p99、数据库行扫描/写放大、RSS、NAS 往返、快照清理负载。现有 recent 索引不能直接假定覆盖所有无 user/channel 过滤组合。

集成测试继续使用现有独立测试 schema fixture，不使用应用 schema；未配置、skip、SQLite/fake 均不算真实 MySQL 通过。跨节点 NAS 故障语义另需目标环境验收，本地文件不能替代。

## 7. 实施分期和完成条件

1. **正确性补丁**：明确 record/prefix/page/full；checkpoint 分批 pin；真实边界测试；完成条件是 >4096 全量读不截断、独立 checkpoint 不挤占批次上限。
2. **compact 容量闭环**：统一预检、Hook 可观测错误和终态结算；完成条件是超限不靠底层异常、不调用超预算模型、不改变未提交状态。
3. **完整接口兜底**：复核/IN 分批、聚合预算、错误映射与存量容量盘点；保留成功响应语义。
4. **summary + 快照服务**：数据库 DDL、独立 schema/游标签名/授权、并发及 TTL 测试；先新增能力，旧接口仍可用。
5. **迁移调用方**：Console、Tracing 和内部 ID 查询切换；详情按需读取；新旧版本混合部署验证和性能验收。

快照表是可再生数据，独立清理和发布，不并入现有会话备份、迁移七表权威集合；部署准入应单独检查新能力的 schema。
回滚新列表开关不回滚正确性修复。滚动发布先完成 DDL 和全部服务端支持，再切客户端；旧 Pod 不支持新接口期间不能让新请求随机落到旧 Pod。共享 cursor 密钥保留至少整个 TTL 窗口。

## 8. 本轮证据与限制

执行：项目虚拟环境 pytest，定向 `test_archive_runtime.py`、`test_retention.py`、`test_chat_repository.py` 及真实 MySQL `test_projection_scale_mysql.py`。
结果：**62 passed，1 skipped**。skip 原因为未配置专用 MySQL 测试数据库，不是验收成功。本轮没有创建或修改测试。

使用实际 ArchiveReader 和 `_ids`、替代数据库及 payload 的只读控制流复现：

- 无 active checkpoint，4095/4096 分别返回同数事件；4097/8193 均只返回 4096。
- 有独立 active checkpoint，4096/4097/8193 在 pin 时抛 `ValueError: publication pins require bounded positive integer IDs`。
- 替身未完整构造 active checkpoint 正文，因此不将 active+4095 用例计为有效读验收。

这些结果支持源码缺陷判断，但不证明真实 MySQL 的大规模 SQL、NAS 性能、GC 并发、跨进程恢复已经通过。现有集成规模测试仅将 MAX_PIN_BATCH 改为 2，不能替代本方案要求的实际边界矩阵。
