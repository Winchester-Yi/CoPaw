# B1b / B2：简化读取与 SQL 批量化设计

日期：2026-11-05。基线：`f4d400194`。状态：已实施，本地回归和静态检查通过；真实 MySQL 验收待专用环境。

本版替代本文件此前的“NAS 后只读 RR 快照＋全量 Message 复核”。用户已确认简化展示读取，同时继续优化集合查询、pin/dependency/Message 批量写入。

背景：[SQL/NAS 成本设计](2026-11-05-conversation-sql-nas-cost-design.md)、[B1a 设计](2026-11-05-conversation-reference-batching-design.md)。本文件是 B1b/B2 当前方案，旧文档的强一致展示读取设想不作为本版要求。

## 1. 边界与选择

- 保持 schema、索引、NAS 格式、旧接口结构、正常成功内容与排序。
- 展示读取不要求跨批同一时点；不新增只读快照、隔离级别封装或全量 Message 行锁。
- 同 Session 写入继续由已有数据库 authority、Session/Chat/Turn 锁与 CAS 协调，不假定调用方已绝对串行，不新增 Redis 或进程内锁替代数据库控制。
- 多实例、后台 GC、备份、Stop、恢复仍存在，保留其必需的锁、pin、精确代次校验、durable 确认、原子事务、幂等和 UnknownCommit 对账。
- 保留授权、scope、正文/locator 完整性、写入来源复核和轻量 Chat 复核。
- 单 pin 4096 publication、单持久化 plan 4096 Message/1 MiB 等容量不变；compact 容量另行处理。

| 方案 | 结果 | 选择 |
| --- | --- | --- |
| 普通分批读＋轻量 Chat 复核，写入/GC 保护保留 | 减少重复扫描和设计复杂度；无跨批统一时点 | 采用 |
| RR 快照复核全部对象 | 有统一复核时点，但额外读取全部 Message、增加事务封装 | 不采用 |
| 去除全部锁、pin、复核 | 要求整个系统和 GC 都互斥，现有部署不满足 | 不采用 |

展示语义放宽：后续批次可能看见更新，新增内容可能留到刷新；不再逐 Message 二次确认可见性。轻量 Chat 复核仍发现已提交的删除、重绑、权限相关元数据/版本变化并拒绝结果，不承诺响应送达前绝对即时撤权。冲突不自动循环重读 NAS。

分享发布、迁移、备份与恢复验证不继承这种展示放宽。

## 2. S1：简化 History / Chat 读取与集合查询

History、Tracing、archive page、answer turn 流程：

```text
短 SQL 事务捕获 Chat/high/Message/boundary 并建立 pins
  → 结束事务
  → 轻量 Chat 检查、metadata hydrate、原授权
  → NAS 内容读取和身份/格式校验
  → 最终轻量 Chat 复核、原授权
  → 返回原响应，真实 IO 排空后释放 pins
```

具体变化：

- 删除 `_snapshot`、`archive_page`、`_turn_snapshot` 内展示用 `_revalidate_rows(..., current_read=True)`，不再对全部捕获 Message 第二遍 FOR SHARE。
- NAS 后不再查全部 Message IDs，只做窄列 `_validate_chat_row`；若 `_revalidate_rows` 没有其他消费者则删除，不留无效开关。
- `_validate_chat_row` 保留原 service/Session、user/channel、history_revision、epoch、metadata/expert revision、content metadata locator 等比较字段。NAS 前后的小检查保留；只是缩窄 SELECT，不删原授权/版本条件。
- 捕获行归属、Msg 身份、extent 和格式检查保留。pin 建立时 publication 已 deleting 则失败，不跳过行拼成部分成功。
- pin 与 locator 捕获仍在原事务，SQL lease 不跨 NAS；取消后真实 IO 排空才释放 pins。

这移除两组 O(N) Message 复核，保留 O(1) Chat 检查。原 history_revision 改变仍可导致拒绝，本版不另外放宽该条件。

完整 Chat 列表/详情的 `_revalidate(rows)` 保留，改为固定窄列普通分批 SELECT。包括 id、scope/deleted_at 与原比较字段，不带 metadata_text 等大字段。每批比较后丢弃临时行，不再累计一份完整 current 字典。同一短 SQL acquire 阶段顺序执行，复用 lease、不新增快照事务。单详情只查一个小集合；空集合不获取连接、不发 SQL。context_usage 共享路径原错误语义保留。

写入侧 `SourceMessages.revalidate`、`ProjectionReferences.revalidate` 保留 B1a current read 与逐字段验证，不能与展示复核混用。

查询批次：复用 reference_queries 固定模板和整数预算；增加必要的 Chat/publication 模板。binary keys 使用少量领域模板，不接受任意表名/WHERE/锁 SQL。

- 每批最多 256 key/ID；请求预算先沿用 64 KiB，再按连接已知 max_allowed_packet 缩小。
- binary 按真实 bytes 和最坏转义计费，不套用整数每项21字节，不规范化或大小写折叠。
- 空集合不查，单项无法容纳明确失败，不截断。
- anchor suppression 分批返回 DISTINCT source_msg_key，只去重判断 key，不合并 Message occurrence。
- boundary 按 ID 窄列批取，恢复原 boundary_order 排序。

## 3. S2：publication 批锁及同事务消重

第一版保留 FOR UPDATE，依赖改窄列批查、current 最后单条完整锁。暂不同时切换 FOR SHARE：现有后续 add_dependencies 再拿排他锁，局部降级会引入锁升级，也不能消除重复查询。

- reader/backup pin：短事务锁 publication、检查 committed/live、插入 pin；提交释放行锁，NAS 期间由 pin 保护。
- 依赖：检查完整 scope、Session、committed/live、捕获 locator、target ID < source ID，按 ID 升序切批。
- current：保留 request/result_metadata_text 精确比较、prepared/committed/aborted 状态处理与幂等。
- 保持 guard、Session/Chat/Turn/proof、publication 原锁阶段顺序；不为 reader pin 新增 scope guard。
- SQL ORDER BY 不证明实际锁顺序，验收索引路径和 GC 等待；分批不减少事务总锁量。
- pin 返回固定的 path/length/scope/关联、kind 和 operation_key 标量，排除大 plan；checkpoint/event 继续使用捕获阶段已读取的标量。current 完整读取例外。分享读取复用 pin 前已读取的完整 committed publication 做 token/plan 身份校验，逐项比较 pin 返回字段，避免批量 pin 额外搬运 plan。

当前 commit 在 Source 锁依赖/current 后，Retention.add_dependencies 又查一轮。本版改为：

```text
原 guard / association 锁
  → 一次批量锁验依赖，完整锁验 current
  → authority、revision、源 Message 最终检查
  → 使用当前事务已锁定行批量安装 dependency
  → 安装 Message/root/checkpoint
  → publication committed，一次 COMMIT
```

公开 `add_dependencies` 独立调用仍自行锁验；commit 内使用私有“从已锁定行安装”步骤，立即消费当前 task 原事务的精确行集合并核对 scope、成员和方向。不保存跨阶段授权缓存，不把旧 snapshot/pins 当最终写入授权；事务结束后不得复用锁证据。reserve/write/commit 之间仍分别检查。

锁强度调整后续须证明整条调用链无升级、GC 保护完整。本版先通过批量、窄列、消除第二组查询降低成本。

## 4. S3：retention 批写与精确成员检查

普通 reader/backup/dependency 使用模块级固定 PreparedInsert，复用 binary 参数、65535参数上限、packet预算、每连接有界 prepared cache 和后批/生成器失败后的 session discard。默认100行/256 KiB，实际按 packet/参数缩批；不假定 executemany 一定合并。

guard/backup_fence 单行路径保留。多行 upsert 保留原 no-op，不更新旧 scope/kind/created_at，不复活 released，不用 INSERT IGNORE。成功由完整集合验证证明，不依赖 affected_rows/lastrowid。

现模板只允许占位符。N>1 在原事务读取一次数据库 UTC_TIMESTAMP(6)，绑定本次所有新成员 created_at；旧成员不变。N=1 保留原 `_insert`，避免新增时间查询/prepare；N=0不插入、不查时间，但仍验证既有 key 无成员。所有批次属于原 pin 或 commit 外层事务，不按批提交。

成员验证按领域区分：

| 集合 | 验证方式 |
| --- | --- |
| reader/backup，单 key 最多4096成员 | 一次窄列 FOR UPDATE，LIMIT=预期数+1，最多4097行；多读一项拒绝额外成员，不增加17次分页往返 |
| dependency，可超过4096 | 现有唯一索引按 member_key keyset，每页最多256行 FOR UPDATE；source publication 全程排他锁，同 source 合法安装/释放均经过该锁 |

首次查询包含 member0；不能预过滤 expected IDs、active、kind或scope，否则隐藏多余/错误成员。比较 scope、publication_id==member_key、kind、active、guard_marker；backup 专有路径检查 snapshot_ref。dependency 预期耗尽继续确认尾部无成员，整页倍数补探测、空集合也检查。

缺项、多项、released、错误身份均冲突，后批失败回滚本次新增。保留普通 pin 原唯一键和锁协议；不为展示一致性新增数据库事务 API。同 key 重试、释放交错仍须验证。

BackupManager.release 复用有界检查，在原外层事务先验证全部 key 再释放，保留 guard、snapshot_ref 和错误语义；capture 完整快照/恢复验收不变。

## 5. S4：Message 批量写入

- 原 Session/Chat 已锁定后按 plan.messages 顺序生成行；在线 session_order 与归档 archive_order 独立推进。
- 复用 `_message_values`，保留 context_epoch、source locator、历史 Turn、Chat、typed identity；source key/正文/extent 相同也不合并 occurrence。
- 多行固定 PreparedInsert，100行/256 KiB，受实际列数、参数和packet再约束；单行保留原 INSERT，零行维持原 high/revision/update 条件。
- 全部行成功后按原次数更新high/history_revision和publication；全部属于原一个事务。
- 不以首个lastrowid+下标推算ID；当前安装不消费逐行ID，批写不追加逐行查询。
- 真正需要ID的消费者按已分配唯一顺序区间批查窄列；缓存关闭不为refresh强制生成映射。

refresh_projection_cache、removed_occurrences 的逐block locator查询后续单列小包：新行优先复用顺序映射，历史行依现有索引有界读取、保持最小ID匹配语义。不用巨大tuple IN或反复全扫换取较少SQL，遗留点查如实报告。

## 6. S5：其余集合写入

| 入口 | 处理 |
| --- | --- |
| delete_chats | 原单事务冻结候选内部ID；锁完Session再锁Chat；分批UPDATE，changed取OR，后批失败全回滚 |
| share Turn/proof | question keys、精确Message IDs批锁；原snapshot、completed/visible/high/Turn校验及最终CAS保留 |
| share无proof | 保留旧最早occurrence和anchor suppression语义，第一版保留旧逐问分支，不任取一条 |
| prune | 保留4096上限，分批锁验全部目标后分批UPDATE；affected_rows不必等于输入数 |

候选捕获不新增RR快照，share最终发布仍严格proof/CAS。放宽展示不能扩展为允许部分安装或错误发布。

一对多_selection_occurrences限制keys数不等于结果有界。结果分窗须按已有archive/session顺序索引分别读互斥两类行，恢复原排序并验证扫描量。旧无proof点查未处理时须明确，不宣称消除全部N+1。

## 7. 成本与顺序

| 场景 | 当前 | 目标 |
| --- | --- | --- |
| N条History Message复核 | NAS前后各重读N行 | 取消两组Message复核，保留小Chat检查 |
| N个Chat复核 | 无界IN、宽列 | ceil(N/Bq)次窄列普通读，无新快照/完整current副本 |
| P个提交依赖 | Source逐条锁，Retention再查 | ceil(P/Bq)次依赖窄列锁＋一次current完整锁，同事务复用 |
| 4096个pin成员 | 4096 INSERT | 行数预算足够时41 INSERT＋一次数据库时间，集合验证一次有界查询 |
| 4096合法Message | 4096 INSERT | 行数切批41 INSERT，字节预算更小时更多，high/revision更新不增加 |

Bq按真实字节/packet计算，最多256；一对多结果另算。上述不含连接检查、事务和prepare往返，也不代表延迟承诺。完整响应和事务总锁仍有O(N)下界，NAS次数/字节不增加，不重读正文或重放模型工具。

顺序：S1简化读/集合查询 → S2 publication批锁消重 → S3 retention批写 → S4 Message批写 → S5其余集合写。S2/S3紧邻，各包独立回归提交，不混入NAS批读/缓存/写入快照重构。

## 8. 验收

1. 查询0/1、255/256/257、4095/4096/4097、8193/16385；大集合仅用于合法全量读/依赖，不构造非法单pin/Message plan。小packet后测试实际批边界。
2. 写入0/1、99/100/101、完整批/尾批、合法4096上限；覆盖字节/参数、binary NUL/引号/反斜杠和Unicode。
3. 完整数量、排序、重复occurrence、cursor、Tracing、隐藏/审批、share/backup格式兼容；Chat删除/权限/revision仍由轻量复核拒绝，不要求Message跨批统一时点或逐条并发复核。
4. GC先标记则pin拒绝；pin先提交则GC发现保护；慢NAS/取消到真实IO排空才释放pins。此并发保护仍必需。
5. current/source、Stop/fence、dependency安装/释放、同retention_key重试、released、member0、尾部额外成员、backup snapshot_ref。
6. 首中末批领域错误、唯一冲突、断链、取消整体回滚；COMMIT响应丢失按原operation对账，无重复安装。
7. SQL/prepare/连接检查、扫描/返回行数、字节、锁等待、RSS、event-loop lag、NAS次数；单行不增加无谓时间查询/prepare，不把逐行INSERT换成逐行SELECT。

无生产SLO，不承诺P95/P99或倍数。共享venv驱动9.4.0与manifest26.7.0不同；真实MySQL/NAS、packet、锁和取消须在匹配部署的专用环境验收，不升级共享venv。本轮仅修改设计和排查索引，未实施运行时代码或执行新数据库验收。

实施入口与验证边界见[实现记录](../reports/2026-11-05-conversation-simplified-batching-implementation.md)。
