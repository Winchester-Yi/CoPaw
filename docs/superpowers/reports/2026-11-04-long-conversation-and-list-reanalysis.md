# 长会话与轻量列表方案：合并后重新分析

日期：2026-11-04。基线：`099ecaeb847dd174a7f2e5fce0739775745dcdb6`，工作树 `.worktrees/conversation-mysql-nas`。
复核原报告：[2026-11-03 长会话与列表方案](2026-11-03-long-conversation-and-list-design-review.md)。本轮仅分析、运行隔离探针与测试、更新文档；没有修改运行时代码，没有操作业务数据库/NAS。

## 1. 结论变化

原报告的 checkpoint 截断、独立 checkpoint 引用挤占、compact 缺少容量闭环、列表仍依赖 NAS、无界复核 IN 均仍成立。本次合并没有修改相关核心文件，因此不能把合并视为这些问题已修复。

需要新增或加强的决定：

1. **把 plan 写入/读回不对称提升为正确性修复**：不仅是“后续容量预检”；已有合法数量的 plan 可序列化成功、解析失败。
2. **checkpoint 修复必须覆盖迁移与备份验证调用方**，不能只覆盖在线 memory/compact。
3. **Tracing 历史兼容修复先于列表调用方迁移**：合并后旧 SQL tracing_view 与新看板的归档/完整历史契约冲突。
4. **列表快照保留用户已确认的共享 MySQL + TTL + 最近更新排序**；补齐显式一致读、资源预算、恢复后失效、有界删除扫描。
5. **正确定义兼容承诺**：旧成功响应字段、排序和完整性保持；容量拒绝属于新增错误契约，不能称为完全无行为变化。建议预算值仍未定稿，不直接写死为生产上限。

## 2. checkpoint 复核与新证据

实现入口：`archive_reader.py:74`；facade：`archive_runtime.py:128`。

- 默认 limit=4096，同时 SQL 硬编码 LIMIT 4096；完整性检查也按 min(high,watermark+limit) 判断，完整读取会把一个 prefix 当成成功全量结果。
- active checkpoint 与事件 publications 不同时，去重 publication ID 数还包含 active checkpoint。
- read_checkpoint_record 先读 checkpoint state，再只取 record，因此 record-only 调用也暴露于事件读取边界。

本轮使用真实 ArchiveReader、CheckpointRecord/Event 编解码与验证、真实 retention._ids；SQL/NAS IO、pin 落库为替身。每个事件独立一个 publication，active checkpoint 使用另一个 publication；active record 正文已完整构造。

| watermark 后事件数 | 无 active checkpoint | 有独立 active checkpoint |
|---:|---|---|
| 4,095 | 成功返回 4,095，pin 4,095 | 成功返回 4,095，pin 4,096 |
| 4,096 | 成功返回 4,096 | pin 抛 ValueError |
| 4,097 | **成功但仅返回 4,096** | pin 抛 ValueError |
| 8,193 | **成功但仅返回 4,096** | pin 抛 ValueError |
| 16,385 | **成功但仅返回 4,096** | pin 抛 ValueError |

相比原报告，本轮补足了 active+4095 的有效控制流读与清理检查。它仍不是真实 MySQL/NAS 验收。

必须保留限定：事件数、publication 数、source occurrence 数不是一回事；多个事件共用 publication 时，pin 边界可能不触发，但隐式 4096 事件窗口仍存在。

### 不应遗漏的调用方

- 在线：archive binding 的 prepare/recover/ready、compact、precompaction prefix。
- `migration/verify.py:562`：直接调用 ArchiveReader.checkpoint，并把返回 events 与完整源 events 比较。长事件尾部可能使导入后的 runtime 校验失败，不能误归类为数据库连接错误或靠跳过 verify 上线。
- `backup.py:1560`：恢复后的 runtime reader 验证直接调用同一函数。该次 reader 调用可能只覆盖前缀；不因此断言备份的其他 checksum/快照验证都失效，也不能把它当全量 checkpoint reader 已验证。
- 搜索所有 `.checkpoint` 直调与 facade 包装，分别声明 record/prefix/page/full 语义；修复后全量校验路径不能仍传旧窗口。

### 修订后的读协议

保留原四种 API 划分，但记录这些约束：

- record-only 只 pin 并读取捕获快照的 active record，不读取 event 正文。
- full 成功必须完整覆盖捕获 high；不能依赖默认页大小。明确返回 captured_high、read_through、has_more，避免 prefix 被误用为 full。
- page 按 event sequence 推进，捕获 epoch/revision/active locator/high；一 publication 跨页时，正文校验和页内截取分开处理。
- locator 选择与 pin 使用相同一致读/事务边界；复用 ReaderPins 时所有批次保持至全部 NAS worker 排空，获取、事务提交、取消和 IO 清理都覆盖。
- pin 批大小不等于全量资源预算。大规模 pin 本身是持久化写，事务/SQL 数量和连接占用必须测量；不能为了读完无限延长事务。
- 提交结果未知时先按现有协议对账，不能因超时或 TTL 推断 worker 已死，提前解除 reader/发布保护。

## 3. 新确认：compact plan 可以写出无法读回的序列化内容

`CommitPlan.serialize` 在 `commit_plan.py:39` 检查消息/结果/prune 条数，但 `:101` 直接返回 JSON，没有检查 MAX_PLAN_BYTES。
`CommitPlan.parse` 在 `:152` 拒绝超过 1 MiB 的 JSON。
`PublicationStore.reserve` 在 `publications.py:369` 使用 serialize 的结果进入 prepared 写入；随后 `_validate_prepared_references` 等路径会调用 parse。

本轮构造符合当前 ArchiveInstall、CheckpointInstall、EventInstall 与 MessageInstall 约束的 compact plan：

- 4,096 个 source occurrences；source key 为短字符串 m1..m4096。
- source locator 复用，不是给每条正文新增一个 NAS block。
- serialize 成功：**1,317,934 字节**。
- MAX_PLAN_BYTES：**1,048,576 字节**。
- 立即 parse 同一文本：`IntegrityError: publication commit plan is invalid`。

这证明 writer/reader 的字节契约不对称；没有在本轮实际写入 prepared 行，不能把潜在 prepared 滞留声称为已发生的数据库故障。

### 解决方案与 compact 两阶段预检

1. 通用 writer 在 reserve/任何持久化之前按最终序列化字节执行与 reader 一致的限制；以 `parse(serialize(plan))` 可读性/等价性为测试性质，不必为性能在生产每次重复完整解析。
2. compact 前预检已知事实：source occurrence、唯一引用、实际可构造 plan、现有 record/上下文、允许输出预算。超限在候选生成和摘要模型之前阻断。
3. 未生成的摘要内容无法在模型前精确计算。对输出设置明确上界，生成后按最终 checkpoint/recovery/event/plan/bundle 再预检，之后才 reserve/write/commit。不能声称第一次检查能精确知道未知输出字节数。
4. 统一写入和读回的 extent/bundle/index 限制；8 MiB 是读取 extent 预算，256 MiB 是 bundle 总量，1 MiB 是 plan；不能互相替代。
5. 超限使用 typed capacity 错误；自动 Hook 不吞掉该错误后继续 reasoning。保留当前“确认 publication commit 后才替换 memory/summary/root”的边界。
6. 容量失败不进入 provider 重试循环，不触发同一过大 compact 的盲重试；SQL 模式经原 terminal 结算为明确 failed，SSE 带稳定 code。UnknownCommit 仍先对账，不能用容量错误覆盖它。
7. 后台 governance precompaction 的失败应记录状态并清理调度水位，前台进入 active/emergency 且超容量时才按明确错误闭环；不能让后台预测失败无条件中断仍在预算内的正常模型请求。

不推荐增加单次 source 条数或拆成几次 store.compact 就上线；分段还涉及完整工具交互、每段 summary/root/CAS、candidate 绑定与 UnknownCommit。

## 4. 列表与完整接口：原结论不变，补充资源边界

MySQLChatRepository._rows 仍 SELECT Chat → 分批 pin → 每项 hydrate NAS content metadata → revalidate。
旧 list/filter、offset、cursor、get 都使用这条链；空 content metadata 也可能有 publication，因此不能用“轻量字段都在 MySQL”推断当前列表已经不读 NAS。
ChatRepository._revalidate、HistoryStore._revalidate_rows 与 anchor 去重仍有随返回量增长的 IN；projection locator/revalidate 另有逐引用 SQL，后者不会因为 IN 分批自动解决。

必须区分三种优化：

- 新 summary：只 SELECT 明确轻量列/白名单 display_meta，不 pin、不读取 NAS；完整未知 metadata 留在详情。
- 旧全量接口：保留成功内容，内部批处理、聚合条数/字节/deadline，超容量明确失败；不是返回前 4096 项。
- 复核/写保护：有界 SQL 与稳定锁顺序；不把分批查询误称为同一时刻快照，不缓存 authority、删除或 lease 判断。

实施前同时看数据库效率审计：长 pin/逐引用 SQL 和列表快照构造会占用共享池，可能与 30 秒执行租约竞争；增加批处理不能以牺牲续租进展为代价。需要基于 deadline 的准入/并发与续租资源预算，而非只增大池。

## 5. 共享 MySQL 列表快照：方向保留，补六条实施约束

用户已确认的“updated_at 最近更新排序 + 更新不造成翻页重漏”继续有效。只加 keyset/time high 不能满足它。

1. **显式一致读**：现有 DatabaseConnection.transaction 只 start_transaction，不显式指定 RR/consistent snapshot。新增快照构建需明确隔离级别和只读源扫描视图，使用同一 task/连接顺序分批读取，不能依赖部署默认隔离级别或每批各开事务。扫描期间新增、更新的双连接用例是验收项。
2. **资源准入**：构建是 O(成员数) 的 SQL 读取/快照写入，首屏不会自动变成 O(page_size)。限制并发构建、累计行/字节/时长和每主体配额；占用共享池必须给续租留预算。先前建议的 10 分钟、100,000 项、64 MiB 均待实测，不是已确认容量。
3. **排序身份与投影完整**：冻结 updated_at/external_chat_key 和 ordinal；投影来自 typed MySQL 字段与轻量 metadata，不读取 NAS 补缺失值。状态冻结并通过现有运行状态更新，不为状态变化重排。快照绑定 service/tenant/source/agent、操作员主体、目标用户与规范化 channel/filter。
4. **删除/撤权后的有界扫描**：每页重验当前可访问性优先于冻结展示。不能为了“补满一页”无界扫描大量已删除 items；定义扫描批次/时长上限，允许返回短页甚至空页但有前进 cursor，前端不能把空页等同耗尽。遍历保证针对仍存在且仍授权的快照成员。
5. **恢复与清理**：快照可再生、不同于 publication pin；TTL 不能用于判断会话 owner/worker 已退出。snapshot tables 不并入会话权威备份时，restore/cutover 后要清空或按共享 generation 使旧 cursor 失效，避免旧快照继续服务。删除清理与在途分页用短事务协调，不能先读半页又被清理余下部分。
6. **发布兼容**：新 schema 与服务端先部署完成，后切客户端；旧接口不静默升级排序和 cursor 版本。持久共享签名密钥保留整个 TTL/轮换窗口；跨 Pod/重启可续读，回滚 summary 功能不撤销 checkpoint/plan 正确性修复。

两表 header/item 方案仍可用；不需要把列表快照并入 NAS publication 依赖图或为它创建 reader pins。

## 6. 合并后调用方迁移修订

详见 [合并影响审计](2026-11-04-conversation-merge-impact-review.md)。SQL tracing_view 仍排除 error/anchor、不返回 ChatSpec、has_more 恒 false；新看板依赖完整详情与 archive.has_more。应先单独修复该兼容性，再迁移 summary 列表，避免把“归档不可见”误诊成 summary 丢数据。

Console 仍存在详情转换使用 fromList.meta 的路径。summary 改造必须使完整 metadata 从 ChatHistory.chat 合并，按 metadata_revision/typed binding 处理；不能拿 display_meta 回写完整 metadata。直链、草稿、task/plan/goal 与专家绑定都在验收范围。

Tracing summary 既绑定操作员主体，也绑定目标用户；前端列表可能改用轻量项，但详情/归档必须继续读取目标 scope。stop/session 身份寻址使用 scoped 精确 SQL，保持多 Chat 歧义语义；没有必要为只找 ID 的请求获取全部完整 metadata。

## 7. 调整后的实施阶段

| 阶段 | 范围 | 完成条件 |
|---|---|---|
| 0 | 固定版本基线、Tracing 小修复 | 目标 SDK/驱动验证；SQL 看板可见完整消息及归档，授权正确 |
| 1 | checkpoint record/prefix/page/full + 通用 plan 可读性 | >4096 不截断；独立 active 不挤占批次；超长 plan 在 reserve 前拒绝；迁移/备份调用方同步 |
| 2 | compact 容量与终态 + 完整接口有界兜底 | 模型前已知预算/生成后最终预检；超限不吞、不继续超预算模型；不破坏原子提交/UnknownCommit；SQL/内存/deadline 有边界 |
| 3 | summary 与共享快照服务 | 无 NAS/pin；显式一致读；构建/清理/配额/撤权/restore失效；跨连接/Pod验证 |
| 4 | Console、Tracing、内部查询迁移 | 详情 metadata 合并、stop 精确寻址、新旧客户端兼容及实际负载验收 |

这不是要求把所有性能优化捆成一个大提交。checkpoint 与通用 writer 校验、compact 闭环、快照服务、调用方迁移应分别可审查与回退。

## 8. 验收补充与本轮证据

在原报告矩阵上新增：

- checkpoint 的事件数与 publication 数两维组合；独立/复用 active、跨 publication 页边界、捕获 high 后 append、reset/checkpoint replacement、取消/GC/提交结果未知。
- plan 的 1 MiB−1、等于、+1 字节及 UTF-8/JSON转义实际字节；serialize 输出可读回；writer 拒绝时没有 reserve/NAS/memory 变化。上述 4096 短 source key plan 作为现实大小回归，不能只测消息数。
- migration runtime verify 和 restore reader 验证用 >4096 事件尾部与故意坏尾部，不能只测在线 prepare。
- snapshot 多连接视图、构建取消/超时、并发配额、全部删除/大量撤权后的空页前进、跨Pod、TTL、密钥轮换、restore后的generation失效。
- 共享池饱和/Tracing写入/慢NAS同时发生时的续租与event-loop lag；使用锁定驱动版本，不以“async”代替负载证据。

本轮执行：

`../../venv/bin/python -m pytest` 定向 archive_runtime、archive、retention、chat_repository、history、MemoryCompactionHook 与 projection scale 集成测试。

结果：**101 passed，1 skipped**；真实 MySQL scale fixture 未配置，skip 不代表验收通过。

新增隔离证据：

- `/private/tmp/conversation_long_list_reanalysis_probe.py`
- `/private/tmp/conversation-long-list-reanalysis-probe.json`
- `/private/tmp/conversation-long-list-reanalysis-tests.xml`

探针复现 checkpoint 实际 4096 边界矩阵与 compact plan serialize/parse 不对称，未使用调小 MAX_PIN_BATCH 的办法。SQL、NAS和pin持久化仍为替身，不证明真实数据库执行计划、跨节点NAS、性能或故障恢复已经通过。
