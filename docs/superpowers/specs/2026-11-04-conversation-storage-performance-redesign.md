# 会话数据库迁移：需求、入口与性能设计重整

日期：2026-11-04。代码基线：`3337db27c`，工作树 `.worktrees/conversation-mysql-nas`。
状态：整体设计，已开始分工作包实施。已落地 context-usage 去除完整 Chat 预读，见[实施计划](../plans/2026-11-04-conversation-context-usage-sql-plan.md)；第二个工作包[summary 查询与 Console/Tracing 迁移](../plans/2026-11-04-conversation-summary-query-plan.md)也已实现并通过本地回归。DDL 和持久化格式不变，未连接业务数据库/NAS；真实数据库、浏览器与性能验收未完成，其余阶段不能视为已实现。

## 1. 本轮已确认的范围

用户在本轮明确确认：

1. 保持现有 schema 和存储格式，仅优化实现。包括不新增表、列、索引，不更改 bundle、recovery projection、commit plan 的持久化版本和字段语义。
2. 暂无现成规模/SLO。先建立可检验的请求成本目标和基线，再确定绝对延迟、吞吐及预算数值。
3. 列表暂不考虑并发更新下的稳定翻页。保留最近更新排序，本轮撤出共享 MySQL 列表快照、TTL 快照表及 Redis 快照设计。
4. 非必要情况下尽量少访问 NAS。应同时减少正文读取、重复索引解析、open/stat、写入、fsync 和目录操作；不能只减少返回字节。

“不考虑并发更新”限定为列表翻页需求。Session 独占执行、generation/lease、提交对账、删除授权、backup fence、reader pin 与 GC 的并发正确性仍必须保持。

继承 ADR 0038/0039/0044/0045 的既有业务边界：MySQL 为元数据与发布权威，正文在 NAS；用户问题准入先持久化；工具完成/审批/压缩等语义边界持久化，文本有界合批；终态成功需确认提交；同 Session 单 Turn 执行；不自动重放工具或跨 Pod 续跑；永久分享可独立于已删除 Chat 存活；迁移采用一次连续停机、全量校验，回退保留新提交。

本轮允许重新设计应用内接口、SQL 组织、事务/连接租约、锁协议、读取流水线、内存缓存和资源调度。增加轻量 HTTP 读取接口属于 API 演进，需调用方契约测试；旧接口不删字段、不静默截断。

## 2. 需求先于组件

| 需求 | 必须满足的语义 | 性能目标 |
| --- | --- | --- |
| 会话发现 | scope/user/channel 隔离，最近更新排序，草稿及任务会话可识别 | 只读 SQL 轻量列，无 NAS、无 reader pin、无恢复根 |
| 身份与授权 | 精确 Chat/Session/Turn 身份，目标用户 Tracing，多 Chat 歧义规则 | 索引点查/范围查，不拉完整列表寻找 ID |
| 数值/运行状态 | context usage、当前 Turn 状态、Stop 定位 | SQL-only；不经完整 ChatSpec hydrate |
| 打开会话 | 保留 memory/task/error/anchor、审批叠加、隐藏上下文脱敏、完整 metadata | 仅读取当前可见响应所需正文；避免路由与 reader 重复 hydrate |
| 历史与单轮读取 | 稳定消息身份、归档边界、源 msgid、旧无 Turn 数据语义 | 根据范围/Turn 定位必要 extent，不先重建整个 Session |
| 执行与恢复 | 完整恢复模块、执行身份、语义边界持久化、未知提交先对账 | 新消息正文尽量只写一次；复用已提交 locator；减少重复扫描和 SQL 往返 |
| checkpoint/compact | epoch/revision/watermark 正确，归档与恢复状态原子可见 | 在线按所需 record/prefix/tail 读取，完整校验流式遍历 |
| 分享 | 全候选旧接口兼容、选中问答切片、completed 判定、永久快照 | 选中项按索引读取，匿名读取只读分享对象，避免原 Chat hydrate |
| 删除与回收 | 先不可见，仍有引用的不回收，不因取消提前释放保护 | SQL 删除快速返回，GC 有界后台执行，不在删除请求同步扫 NAS |
| 备份/迁移/恢复 | 完整元数据与 NAS 引用闭包，无尾部遗漏 | 顺序批处理、流式验证、受限资源；不挤占在线控制请求 |

## 3. 当前入口清单与实际重路径

下表记录 `3337db27c` 基线的源码核对，不是性能压测结果。链路中的 NAS 指直接或间接正文/索引读取。实施进度以各工作包计划及测试结果为准。

| 入口 | 当前链路/证据 | 应使用的目标路径 |
| --- | --- | --- |
| `GET /chats` 数组、页码、cursor | `runner/api.py:1080` → ChatManager → `chat_repository._rows:163` → pins → `_hydrate:133`；cursor 还执行 `_count:333` | 旧全量兼容路径保留；新 summary 页独立 SQL 查询 |
| Console 首屏/加载更多 | `console/.../sessionApi/index.ts:1669,1827` 调 `listChatsPage`，并按 meta 识别任务/模式 | summary 页；进入详情后合并完整 metadata |
| `GET /tracing/chats` | `routers/tracing.py:106` 调完整 list_chats，目标用户 scope | 目标 scope 的 summary 页；旧数组兼容但从主 UI 撤出 |
| `GET /chats/{id}` | `runner/api.py:1712` 先 mgr.get_chat；`history.current` 又 snapshot/hydrate Chat | 一次正文流水线，路由不预先 hydrate |
| Tracing detail/archive | `routers/tracing.py:124` 先完整 get_chat，随后 current/archive_page；此前 parity 修复须保留 | 一次 target-scope identity 校验 + 一次必要正文流水线 |
| `GET /chats/{id}/history` | `history.archive_page:523` 按 archive_order/high 查询，pin 后 NAS | 保留 keyset，复用批量 extent 读取，不读取恢复根 |
| `GET /chats/answer-turn` | `runner/api.py:1409` → indexed.answer_turn；兼容旧无 runtime Turn 数据 | 精确身份/Turn/消息范围，不能退回全历史拼装 |
| `GET /chats/{id}/context-usage` | `runner/api.py:1633` 先完整 get_chat；`history.context_usage:600` 本身是纯 SQL | 从路由开始 SQL-only |
| Console Stop | `routers/console.py:2498` 全量列 Chat，再筛 Session，再逐候选询问 current_identity | scoped Session/Chat 候选范围 SQL + coordinator 原歧义判断 |
| Goal/SubAgent/任务资源访问 | 多处先 `manager.get_chat` 仅为授权/关联 | 轻量身份查询；业务正文仍归各自领域 |
| 发送、工具结果、流式语义发布 | `runner_integration` → `SemanticRuntimePublisher` → `_save_state` → reserve/write/commit | 统一写入协调、一次冻结快照、有界合批和批量校验 |
| Session 载入 | `session_adapter.py:251` root pin → NAS → projection refs → hydrate | 批量定位、正文复用缓存、同 bundle 批量读取；确需执行时才载入 |
| checkpoint/precompaction/compact | `archive_runtime.py:128,325` / `archive_binding.py:163` | 分开 record、prefix、page、complete traversal，避免全读后丢弃 |
| share-options/create | `chat_sharing/router.py:239,274` 与 `shares.py:439`，选中 occurrence 已有索引路径 | 保留全候选兼容，UI可演进分页；创建只 hydrate 必要选中问答 |
| 永久 token | `shares.read_snapshot:616` | token → publication → 必要快照正文；不依赖原 Chat 存活 |
| GC | `gc.report:314` 逐候选调用多条引用保护查询；mark/reclaim 独立 | report 批量只读预筛，真正 mark 时重验，不凭报告直接删除 |
| 迁移/备份/恢复 | migration.verify 与 backup.verify_runtime 调 checkpoint；backup 部分表 fetch_all | 全量语义不变，扫描/校验内部有界；现有输出格式仍可能要求累计内存 |

相关不迁移领域仍包括 Tracing 分析库、Goal/SubAgent 状态、审批业务状态、附件与生成文件、Cron 定义/执行。只优化它们访问会话身份的适配入口，不将其状态搬入会话表。

## 4. 当前成本及不可隐藏的下界

定义：`H` 为全部保留历史，`A` 为恢复根列出的当前引用/状态规模，`R` 为本次响应条数，`P` 为唯一 publication 数，`Δ` 为本次新增/变化内容，`B` 为 SQL 批量大小。

已核对的成本：

- 列表每页虽有 SQL LIMIT，仍可能对每项内容 metadata 建 pin 并读 NAS；轻量列表不应承担该成本。
- `projection_references._snapshot_rows:108`、`revalidate:52`，以及 `source_messages.lock_publications:97` 等逐引用 SQL，造成串行网络往返 O(A/P)。缓存正文不能跳过这些 SQL。
- `retention.pin:317` 与 dependency 安装逐成员 INSERT。批量后 SQL 命令数可变为 O(ceil(P/B))，但实际成员行数仍为 O(P)。
- `bundle.iter_extent:454` 每个 extent 先 inspect，再打开文件读取；同 bundle 的多个 extent 会重复路径遍历、索引解码和打开文件。
- `SemanticRuntimePublisher._root/_publish` deepcopy 和比较全状态；`_save_state:327` 先编码完整 root，`prepare_recovery_blocks:131` 再解码、投影并编码。已复用正文，但 CPU/引用清单成本仍随 A 增长。
- 当前同 scope 的 guard 为排他锁，普通不同 Session 的部分写事务会竞争同一行；普通 reader pin 本身不走该 guard。
- 池租约获取 ping，归还分别 get_autocommit 和读取 baseline，短独立 CRUD 的命令数明显超过业务 SQL 数。
- 默认 lease=30s，续租 sleep=10s，池 acquire_timeout=30s；这组默认预算不能证明满池时仍能及时续租。

约束带来的下界：

1. 完整历史响应至少需要处理返回内容 R；不能承诺全量详情与历史长度无关。
2. 既有恢复格式存完整引用清单，每次持久化此清单仍至少 O(A)；本轮不能承诺完整提交是 O(Δ)。
3. 依赖和 pin 仍按 publication 逐成员存行；批量只能减少命令往返，不能取消行/索引/redo 成本。
4. 若长期不 compact 导致 A 增长，完整 root 与 dependency 写入累计可能很大；需要容量准入和压缩，不靠缓存承诺无限扩展。
5. 保留精确 total 的旧分页接口仍有 COUNT 成本；新接口应允许不返回 total，以 limit+1 表达 has_more。
6. 现有索引不能保证所有管理员跨用户/筛选组合都无 filesort；不改索引的约束下必须如实记录 EXPLAIN 与扫描成本。

## 5. 目标应用架构：按数据需求分流

不改表，不增持久化投影，不把所有读取继续塞进完整 ChatSpec repository。

```text
Console / Tracing / CLI / 内部服务
        |
        +-- 身份/摘要/状态查询 ---- MySQL 显式列 ---- 返回
        |
        +-- 正文查询 ---- SQL locator+pins ---- 缓存/批量NAS ---- 组装与复核
        |
        +-- 执行写入 ---- 冻结快照/预检 ---- reserve ---- durable NAS ---- SQL commit
        |
        +-- 离线维护 ---- keyset批次 ---- 必要正文流 ---- 完整验证/原格式输出

所有分支受同一资源调度约束：控制请求、交互正文、后台维护分级
```

比较的实现路线：

| 路线 | 收益 | 问题 | 结论 |
| --- | --- | --- | --- |
| 只给旧 repository 加缓存/批次 | 局部改动较小 | 身份查询仍依赖完整对象，隐藏 NAS 路径保留 | 不作为总体架构 |
| 按读取需求拆接口，复用底层事务/内容IO/发布机制 | SQL-only 入口可证，避免重复 hydrate，可单独预算 | 需显式迁移调用方并做契约测试 | 推荐 |
| 异步持久化、全局结果缓存、延迟一致读 | 可降低某些请求等待 | 改变提交/授权/持久性语义，需新协议或存储 | 不在本轮 |

### 5.1 轻量 SQL 查询

提供内部 `get_identity`、`find_session_candidates`、`list_summaries`、`get_run_status`。查询结果为明确类型，不伪装成含完整 metadata 的 ChatSpec。

摘要字段限于现有 title、ID、user/channel、时间、typed expert binding、运行状态及现有 metadata_text 中的白名单展示字段。未知内容字段不读取 NAS 补齐。保持任务/plan/goal/创建者等现有消费需求逐字段映射。

新增独立 summary 页接口，cursor 使用现有最近更新排序与 external_chat_key tie-break，读取 limit+1，不执行逐页 COUNT。需要精确 total 的旧接口继续保留；新 UI 可用已载入数量和 has_more，不能给旧 total 填假值。

Console/Tracing 主列表迁移到 summary；CLI/旧客户端保留旧数组、页码、v1/v2 cursor、完整 metadata。详情合并以同次详情中的完整 Chat metadata 为准，保留 typed binding 和本地草稿合并语义，禁止拿 summary 覆盖完整 metadata。当前 sessionApi 的 fromList.meta 路径必须修复后再切 summary。

权限/删除/owner/lease 不使用跨请求陈旧缓存。先复用可信数据库 scope 与轻字段授权；若历史兼容授权字段仅存在 NAS，保留受限 fallback 并记录原因/比例，不能把缺失字段当成授权通过。

### 5.2 正文查询流水线

统一步骤：SQL定位和授权 → 捕获high/版本 → 去重必要publication并pin → 归还连接 → 正文缓存/批量NAS → 解码/转换/审批叠加 → SQL复核 → 排空IO后清理。

路由不再先完整 get_chat 再调用 reader。身份授权在 reader 的入口与复核中完成，保留原错误映射和目标scope。context usage 正常路径从路由到存储不读 NAS；SQL 缺少历史授权字段且存在内容 metadata 时，遵循上一节的必要 fallback，不能省略历史权限约束。

批量读取按 `(scope, publication_id, path, expected_length)` 分组：在一次受保护的文件访问中验证索引、文件类型、长度和每个 extent，读取需要的 blocks，维持原逻辑顺序。不会为少量 extent 读取整份大 bundle；相邻范围合并需受字节预算限制。文件描述符只活在该批IO内，不能跨请求无限持有。

同一正文locator在请求内只读一次。跨请求可对已验证的不可变正文/BundleIndex使用进程内有界LRU、字节配额和同key读取合并；包含完整service/scope/publication/locator身份。命中缓存仍执行当前SQL授权/可见性/引用复核，需要publication存活的读取继续遵循pin协议；缓存不成为GC保护，也不自动免除必要检查。不缓存动态审批叠加结果和权威状态。

旧全量接口保留完整成功语义，内部批量复核及读取，并设置累计预算；超过容量明确错误。若要让“打开会话”首屏严格有界，另增当前在线消息页接口并迁移UI，不把旧current悄悄改成尾页。现有format下完整UI组装/响应内存仍有 O(R) 下界。

### 5.3 统一写入准备，保留必要durability

对一次语义边界只冻结一份完整状态；在内存中从冻结对象构造现有recovery envelope/blocks，避免完整root编码→解码→再次投影的往返。使用类型清楚的内部准备结果，磁盘仍是原format/version。

将locator选取、message/turn/pub归属验证、prepared引用验证、最终锁定验证按scope/session分组去重批量化。SQL命令数按批次数增长；最终CAS、当前权限、publication状态和lease仍在提交事务内检查。不能以请求缓存代替权威复核。

正常进程可复用已确认的冻结内容/索引，减少重复NAS读取；提交对账与重启恢复必须只凭持久化plan、bundle和SQL状态重建，不能依赖进程缓存。移除任何一次读回前，需证明其校验被等价保留，默认先优化重复路径/同文件访问。

只合并允许延迟的纯文本增量。工具结果、审批状态变化、checkpoint/compact/reset及终态按原语义边界及时持久化；不能为吞吐而直接后台fire-and-forget。无法证明无变化的未知模块仍需完整快照，不用未经覆盖的dirty标记跳过更新。

纯标题、binding、状态、可见性变更优先走已有窄SQL命令，避免全对象upsert重写NAS metadata；保留未修改的content locator。新建Chat时写空content_meta bundle的成本单独记录；是否改为纯SQL创建，必须先完整设计唯一身份、registry顺序、草稿竞争及提交未知的对账路径，不在本轮未经证明地绕开publication流程。

预检必须覆盖通用plan最终JSON字节，也覆盖recovery projection转换后的有效安装plan、envelope、extent、index和bundle容量。仅检查原始小plan不足以证明后续effective plan可读。已知结构容量在reserve/NAS写之前验证；摘要未知输出在生成后、持久化前最终验证。

### 5.4 批量引用与pin

复用现有PreparedInsert设施，将pin、dependency及可安全批量的Message安装改为受行数/字节/参数数限制的批量INSERT；不把prepared.executemany视为一条批量命令。已有record唯一键、成员集合检查、tombstone及锁顺序保持。

避免逐个source message、Turn、publication点查：批量fetch并构造id→row，在稳定ID顺序下进行当前读/锁定。批量返回不保证消除全部DB行锁竞争，需多连接死锁/GC测试。不能对同一事务连接gather多个SQL。批量Message安装若需要返回内部ID，应按已分配的唯一session/archive order查询映射，不能从lastrowid推算连续自增ID，也不能按可能重复的源Msg.id合并occurrence。

### 5.5 checkpoint改为按需读取

四种契约保留，但以按页读取为共用机制：record-only；prefix；固定epoch/revision/active locator/high的page；必须完整覆盖captured high的complete traversal。仅确需数组的兼容full包装才累计全部events并受内存预算限制。

与上一轮方案的重要调整：完整遍历不默认一次性选取并pin全部历史。每页在短SQL事务中选择locator并pin，事务外NAS读取，排空该页IO后释放该页pins；下一页继续固定快照身份，最终复核。record已读入内存不需要为了本地对象一直pin；仍在使用的NAS操作必须一直受保护。发生reset/active替换/缺页时明确冲突或完整性错误，不能转而接新版本。

消费迭代器必须到正常结束才算full成功；取消、预算超限或坏尾部不能取得“已完整验证”的结果。迁移/恢复逐页与源事件比较，摘要projection用deque保留最后5条，避免全事件数组驻留。离线只读适配器继续依赖已停止writer/GC的边界。

单批pin仍4096，active与事件引用去重计入。publication跨页只改变页内事件截取，不削弱完整extent校验；请求内有界缓存避免跨页重复读同一event extent。

locator分页优先利用现有`ix_conv_publication_events(chat_id,epoch,event_last,id)`：按已验证的非重叠事件范围，以`event_last,id`顺序推进，保留state/high约束与连续性校验。不要继续按未匹配该索引顺序的event_first排序后假定分页成本已解决；实际SQL必须做EXPLAIN验证，必要时接受并记录当前schema下的残余扫描。

### 5.6 锁与资源调度

第一步先缩短和批量化当前guard临界区，消除同事务重复guard查询。若同scope多Session实测仍被guard串行限制，采用现有guard行的共享/排他协议：普通写持共享保护，backup fence变化与危险GC持排他保护，具体Session/Chat/Turn变化仍持各自行锁。

这不是机械替换FOR UPDATE：首次ensure/upsert仍会获取排他保护，必须从热路径移到受控初始化/缺失恢复事务；共享保护下fence检查采用当前读；所有fence安装/释放和GC路径一致遵守协议；锁顺序整体验证。MySQL的共享锁允许共享读而阻塞修改，详见[官方锁定读说明](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)。本方案需双连接证明通过才替换现有协议。

连接池按控制请求（renew/stop/必要终态）、交互请求、后台任务分级。保留单进程总连接预算，控制类预留容量，所有默认业务调用都受普通容量限制；单纯优先队列不能抢占已被占满的连接，必须验收实际预留。不能为每租户建立新池，也不能另建不计总量的控制池。

renew采用lease剩余时间约束的端到端deadline，包含排队、锁等待、执行和清理余量。数据库超时后仍隔离/排空物理IO，不把超时解释为业务未提交。

合并归还时autocommit与baseline检查为一次SQL，保留污染检测；探活改为按连接闲置情况执行属于后续测量项，不盲删。每个短SQL阶段复用一个连接租约，但NAS等待阶段释放连接。事务隔离/consistent snapshot必须显式定义并在归还时恢复或隔离；不要把含pin写入的事务声明为READ ONLY。[Connector事务参数](https://dev.mysql.com/doc/connector-python/en/connector-python-api-mysqlconnection-start-transaction.html)说明默认不请求consistent snapshot且不指定隔离级别。

NAS读写使用独立有界执行资源、有限队列/字节预算和背压，避免挤占默认to_thread池。调用开始前可以按deadline拒绝；调用开始后等待实际worker结束才释放pins/宣告writer停止。线程无法可靠中止卡住的文件系统IO；硬停止需要进程隔离及恢复协议，本轮不声称仅加timeout即可解决。

### 5.7 后台维护

GC report批量查询一批候选的引用关系，仅作为预筛；mark/reclaim继续在权威事务内复核并保留原生命周期。不能拿几秒前的报告直接删文件。

备份/导入/校验按既有索引keyset批量读取，NAS复制/验证受后台并发配额；fence保持时间单独衡量。既有manifest需要全局数组、依赖闭包或一致快照时，明确保留相应O(H)内存/事务下界，设置容量准入；没有改存储格式就不宣称全面流式化。

## 6. NAS访问准入矩阵

| 操作 | 正常目标NAS读取 | 正常目标NAS写入 | 例外/说明 |
| --- | --- | --- | --- |
| 新summary列表/翻页 | 0 | 0 | 不走旧完整ChatSpec接口 |
| 身份/状态/Stop定位 | 0 | 0 | 历史兼容授权必要字段缺失时明确fallback |
| context usage | 0 | 0 | 必须移除路由前置完整get_chat |
| 标题/binding等窄命令 | 0 | 0 | 使用既有SQL权威，不顺带更新正文meta |
| 旧完整metadata列表 | 仅需要的content meta | 0 | 保留兼容成本，不包装成summary |
| 打开详情/归档/Turn | 缓存未命中的必要extent | 0 | locator去重、bundle分组，不读recovery root |
| 执行载入 | 必要root与引用正文 | 0 | warm命中减少正文IO，当前SQL校验保留 |
| 流式文本 | 按需校验，减少重复 | 有界批次 | 不逐token写，但不能取消原语义边界 |
| 工具结果/终态/compact | 必要旧源/验证 | 必要durable bundle | 不省略fsync/目录持久化要求 |
| 分享token读取 | 必要快照正文 | 0 | 访问计数仍按原SQL语义写入 |
| 删除Chat | 0 | 0 | 物理回收异步走GC |
| 迁移/备份/恢复/GC | 完整性要求所需IO | 必要复制/回收 | 后台受限，不用缓存伪造目标存储校验 |

## 7. 基准与验收

暂无业务SLO，因此不写“P99<100ms”等虚构阈值。先记录相同数据、同驱动、同连接预算下的基线，建立每个入口的SQL/NAS成本断言，随后依据容量曲线确定发布门槛。

每个请求采集：业务SQL命令数、连接检查命令数、取连接次数/等待、扫描/返回行数、事务/guard锁等待、pin/dependency成员数、NAS open/inspect/stat/read/write/fsync次数与字节、CPU复制/编解码时长、队列等待、event-loop lag、renew剩余裕量。指标使用模板标识，不记录正文、密钥、SQL敏感参数。

| 验收项 | 可判定目标 |
| --- | --- |
| summary/context/普通identity | 对BundleStore所有访问设失败哨兵，端到端请求仍成功；零pin写入 |
| 精确身份查询 | 用户总Chat数增长时不增加hydrate数；候选多义仍按原规则失败/选择 |
| 单页正文 | 不读取页外/Turn外无关正文；同locator请求内只读一次；同bundle分组减少索引/打开次数 |
| 引用校验 | SQL命令次数随批次数增长，结果行数/成员行数如实记录，不声称O(1) |
| warm运行与恢复 | 正文NAS读取下降且SQL授权/版本/GC校验仍生效；不能只测单进程连续命中 |
| checkpoint | 实际4095/4096/4097/8193/16385；独立/共享active，事件共享publication，跨页、坏尾部、取消、append/reset/GC |
| plan/extent | 最终JSON及有效projection安装plan可读回；超限在可确定阶段尽早拒绝，无假成功 |
| 控制请求 | 满普通池、Tracing繁忙、慢NAS、行锁等待下仍有控制容量；renew无法及时完成时停止推进 |
| 写入边界 | 准入先持久化、工具边界不丢、unknown先对账、成功终帧在确认commit后 |
| 锁协议 | 同scope不同Session能并行；backup fence与writer互斥；删除/GC不漏保护；无锁升级死锁回归 |
| API/前端 | 旧成功字段、metadata、fragment/Turn身份、任务卡、审批、Tracing目标scope、永久分享保持；新summary前端读取详情meta |
| 离线 | 冷缓存/新进程验证目标NAS；源尾部完整比对；保留单次停机与回退语义 |

压测规模按需扩展：列表成员数、active A、完整历史H、唯一publication P、单正文/工具结果字节、同scope不同Session并发、跨scope并发，以及后台混合负载。报告容量拐点和失败方式，不将尚未制定SLO等同于无限制上线。

当前专用MySQL/NAS验收未执行；旧报告指出驱动安装版本与锁定版本有差异，实施基准前再次核实。隔离探针/假数据库只能证明控制流和成本次数，不能替代目标数据库EXPLAIN与NAS实测。

## 8. 实施拆分与决策状态

| 阶段 | 工作包 | 退出条件 |
| --- | --- | --- |
| 0 | 入口需求矩阵、成本埋点、锁定环境、代表性基线 | 每个关键入口能解释SQL/NAS/CPU/锁成本；无业务库探测 |
| 1 | SQL身份/状态/context入口、去重复get_chat、summary与前端metadata契约 | 正常轻请求端到端零NAS；保留旧完整接口 |
| 2 | locator/引用/pin/dependency批量化、短租约检查合并 | SQL往返下降、正确性与取消/GC矩阵通过 |
| 3 | bundle批量读取、有界正文/索引缓存、NAS背压；控制请求资源保留作为上线前必需项并行设计 | NAS系统调用下降，满载renew/stop验证通过 |
| 4 | 写入冻结/准备流程、checkpoint按页读取、plan/effective plan容量闭环 | 长会话不截断，必要写入保留，CPU/NAS重复成本下降 |
| 5 | guard共享/排他协议（仅在测量确认瓶颈后）、维护批量/限流、综合验收 | 同scope吞吐及后台混合负载达门槛，备份/GC无一致性回退 |

上述是依赖工作包，不是要求一次大提交。checkpoint/plan确定的正确性回归可先修；但不能以修复它们代替性能架构验收。控制请求资源与deadline不能等全部性能任务结束才处理。

已选择：保持schema/format；SQL轻读与正文读分离；不新增列表快照；减少NAS交互优先；不减弱提交持久性。

待测量决定：SQL批大小、缓存/队列字节、NAS worker数、连接控制预留、文本合批值、read/full预算，以及是否值得改变guard锁协议。默认值最终要由基线与故障测量支撑；代码中的已有默认值不是用户确认的生产SLO。

初稿仅作设计；后续已实施 context-usage、summary、Console 任务来源解耦、轻量运行状态查询，以及 checkpoint 读取契约/持久化 plan 容量工作包，验证记录见各自计划。剩余实施必须对照本表逐包评审，避免重新回到逐个旧方法叠加limit/cache。

## 9. 关联资料与历史结论修订

- [Console 分页与任务来源解耦草案](2026-11-05-chat-task-source-snapshot-design.md)：基于已提交 summary 工作包继续设计，任务快照与会话分页分离；已实施，见对应实现报告。
- [现有入口索引](../../../analysis/playbook/conversation-storage-entry-inventory.md)
- [数据库效率审查](../reports/2026-11-04-conversation-database-efficiency-review.md)
- [长会话重新分析](../reports/2026-11-04-long-conversation-and-list-reanalysis.md)
- [Tracing parity修复](../reports/2026-11-04-tracing-history-parity-fix.md)
- [元数据MySQL/正文NAS ADR](../../adr/0038-conversation-metadata-in-mysql-content-on-nas.md)
- [发布边界ADR](../../adr/0039-conversation-publication-uses-mysql-commit.md)

本轮明确取代先前“新增共享MySQL列表快照表”的实施方向；不删除旧报告，保留其分析历史。本轮也调整“完整checkpoint一次性pin全部引用”的默认实现方向为按页保护与完整遍历。任何缓存、锁协议调整和持久化省略均不得只凭本文宣称已实现或已验收。

2026-11-05：checkpoint 四契约、逐页引用保护及通用持久化 plan 字节校验已实施，见[实现与验证](../reports/2026-11-05-checkpoint-read-contracts-implementation.md)。完整遍历和调用方已调整，但 effective projection plan/compact 全链路容量、其余阶段 2–5 优化与真实 MySQL/NAS 验收仍未完成。
