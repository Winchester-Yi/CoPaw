# Python 内存优化第二阶段设计

日期：2026-10-15。状态：代码实现及本地验收完成，尚未生产迁移或启用。实现基线：`ddb683634`。

用户已确认：暂不使用 Redis；允许全部写入方升级后，再按租户灰度启用新格式。后续已授权实现 2A、2B，并将第一阶段后台清理间隔调整为 60 秒；不执行生产迁移或部署。

## 1. 目标与边界

降低长会话中历史 `turn_states` 对普通执行的内存影响，消除 checkpoint 每次追加全量读写事件日志的成本。继续使用现有共享 NAS、独立 lock 文件、POSIX flock、revision 和原子替换。

本阶段交付分成两个可单独验证的部分，建议先做 2A，再做 2B：

| 部分 | 内容 | 验收重点 |
| --- | --- | --- |
| 2A | checkpoint 分段追加与按水位读取 | 追加不再随历史事件总量读取/重写旧日志；恢复不会跳过损坏数据 |
| 2B | session 终态 turn 冷存储、按需查询和减少复制 | 普通执行不解析冷 turn；状态、重试、恢复和分享语义不变 |

不迁移 Redis、MySQL 或 SQLite；不裁剪用户历史；不改变 Stop、审批、cron execution key、Context Epoch、证据引用和分享快照契约。不改变模型压缩策略，也不迁移 `agent.memory.content`、`task_runs`、技能快照或 hook overlay。SSE、文件/媒体预算、Provider 缓存仍属后续范围。

因此，本阶段不会让整个 session 或整个进程具有固定内存上限：活动 Agent 历史、未结算 turn、单条大消息及旧完整历史响应仍可能很大。不能把普通执行的改善等同于所有历史接口都已分页。

## 2. 源码事实与风险

GitNexus 绑定 `/Users/shixiangyi/code/Swe` 并刷新索引；query/context 对相关 Python 方法未给出可靠调用链，FTS 构建失败、结果存在损坏路径。以下以源码复核为依据；实施时仍须逐符号 impact，UNKNOWN 不能视为低风险。

| 位置 | 当前行为及约束 |
| --- | --- |
| `app/runner/session_lifecycle.py:24,167` | admission 保存完整 user `Msg.to_dict()`；按 `existing.message == message` 判断是否需要重写 admitted 状态 |
| `app/runner/session_lifecycle.py:66` | 终态仅更新 status，旧正文仍在 `turn_states` |
| `app/runner/session.py:206,258,308,377` | 整文件 read/json.loads；事务只读一次；提交浅复制顶层后整份 json.dumps；取消时等待磁盘写完成 |
| `app/runner/api.py:282,319,786` | turn 正文补足无 Agent 输出的用户锚点，并扫描归档去重；历史转换还构造规范化 dict、InMemoryMemory 和返回模型 |
| `app/runner/api.py:1161` | answer-turn 查询读取历史后按 msgid 切片，状态和 chat_id 来自 turn_states |
| `app/routers/console.py:1124` | 当前恢复按 dict 插入顺序反向找最新 turn；孤儿 admitted 持久化为 failed |
| `app/chat_sharing/router.py:92,131` | 分享优先使用持久化终态；丢掉它后推断可能把失败/停止输出当作完成 |
| `agents/memory/conversation_archive.py:394,420,1260,1292` | checkpoint 读全部 events 后过滤；append 两次读全部日志，再拼接字符串整文件替换 |
| `agents/memory/conversation_archive.py:1708` | 在线 memory.add 后先 read 再 append，跨实例可能计算同一 next sequence；后写者失败，不是磁盘序号重复 |
| `agents/memory/conversation_archive.py:304,625,696` | checkpoint/archive/epoch 涉及多文件，普通异常可回滚但 SIGKILL 不会执行回滚 |
| `agents/memory/conversation_archive.py:1508` | chat 独立 flock；文件原子写有 fsync、replace、目录 fsync |

审批恢复还依赖 Agent memory 及 ApprovalService 的工具调用和队列数据；不能因 turn 已终态而连带清理这些状态。Logical Chat Session 和 Chat Record 不等同：session 冷数据必须归属 session，而不能因一个 Chat Record 被删除就删除共享 session 的全部 turn。

## 3. 方案比较

| 方案 | 收益 | 代价 | 选择 |
| --- | --- | --- | --- |
| 仅删除 terminal.message，保留全部状态 | 易实施，减少大正文重复 | 热元数据仍随历史增长；仍需解决正文查询和 admission 幂等 | 不作为最终方案 |
| NAS 冷 turn 文件＋有界热窗口；checkpoint 分段追加 | 复用部署与锁语义，历史可恢复，普通执行按需读 | 增加文件数与版本协议，需要崩溃注入测试 | 采用 |
| 数据库统一会话事件和索引 | 查询、索引与事务可集中管理 | 存储迁移、连接与故障语义扩大范围 | 后续独立设计 |

冷 turn 首版采用按 turn 定位的不可变版本文件，而不是自建 B-tree 或每次重写全量索引。这样新 turn 的 admission 不必扫描全部冷历史，也不引入通用缓存/数据库框架。代价是 NAS 小文件数量增多，需要测量 inode 与元数据 I/O；后续可在接口不变的前提下合并存储。

## 4. 2A：checkpoint 分段追加

### 4.1 数据与提交根

保留 `CheckpointEvent` 字段、epoch 内连续 sequence、UUID boundary 和现有 HMAC UI cursor。新增版本目录：

```text
dialog/<chat_id>/
  state-v2.json                         # 唯一原子发布根，小型 JSON
  objects-v2/<uuid>.json               # 不可变checkpoint/manifest/evidence对象
  epochs-v2/<epoch>/final-root.json     # reset时冻结的旧epoch最终根
  epochs-v2/<epoch>/<ordinal:012>.jsonl
  epochs-v2/<epoch>/<ordinal:012>.meta.json
  <existing boundary UUID>.jsonl       # 原归档 batch，ID和行号不变
  <existing boundary UUID>.offsets     # 派生的big-endian uint64行偏移
```

`state-v2.json` 包含 format_version、generation、current_epoch、checkpoint/manifest/evidence 相对文件引用，以及当前 epoch 的 `last_sequence / active_segment / committed_bytes / sealed_segment_count / applied_event_sequence / checkpoint_revision`。后两个值与所引用checkpoint同时发布并校验一致，供head读取而不解析完整record。引用只能指向服务端生成的本 Chat 目录；不能接受客户端路径。reset先在确定路径冻结旧epoch最终根并fsync，再发布新根；新根保存previous_epoch及其根hash，形成已提交链，不加入无界列表。未成功切换时提前写出的历史根不可见，重试持锁核对当前根并重新冻结；读者不靠目录枚举认定epoch已提交。

checkpoint/epoch/manifest/evidence 变更先写新不可变文件并 fsync，再一次替换根并同步目录。未变化的文件共享旧引用。只有根发布后 batch/新 checkpoint 才可见；异常或进程死亡留下的未引用文件不能被目录扫描自动导入。所有 v2 读写与 tombstone 判断都持同一 chat lock，旧版文件在 v2 启用后仅是迁移备份，不允许新旧两份真源双写。

该提交根是分段协议的组成部分，用于避免现有“checkpoint 已更新、manifest 未更新”和“epoch 已更新、record 未更新”的崩溃窗口。它不把 session 与 checkpoint 合成一个全局事务：两者仍各有提交根，沿用既有候选、归档和结算恢复规则。

### 4.2 追加协议

在一次现有 chat flock 内完成：检查 tombstone → 读取根与当前 epoch → 清理超过 committed_bytes 的未提交尾部 → 校验事件及 expected_epoch → 分配 next sequence → 写完整 frame → flush/fsync → 原子发布新根 → 返回 receipt。

- ordinal 从 0 递增，segment 初始上限 **1 MiB 或 512 条**，先触达者封段；单事件初始上限 **64 KiB UTF-8 序列化数据**。上限用于 deterministic facts，不允许复制工具正文。新增事件超限时明确失败，不静默截断或跳过。
- 每条 frame 采用一行 JSON envelope，保存 payload、payload 字节长度和 SHA-256；限制长度后逐行解析。写入处理 short write；不假设 NAS 上一次 O_APPEND 就能原子写完整行。
- 根中的 committed_bytes 是可见边界。进程在 append 后、根发布前死亡，该尾部不可见，下一写入方在锁内截断；根已发布但回复丢失时，事件已提交，不能自动重跑模型或工具。append校验当前active段的已提交前缀，最多1MiB/512条；旧sealed段损坏由读取时检测，不承诺每次追加扫描全盘验证。
- receipt 为 `(epoch, sequence, event_id, payload_hash)`。内部追加 API 支持 expected sequence：重试同槽位且 ID/hash 相同返回原 receipt，不同则冲突。由存储层锁内分配序号；需要消除未知结果的调用方先取得/保存 proposed sequence 再条件追加，不能用新随机 ID 盲目重试。首版不承诺任意外部副作用 exactly-once，也不维护无界进程去重集合。
- wrapper 不再 read 全部 state 算 next sequence。`memory.add` 与落盘失败沿用显式错误传播；失败后不能把未持久事件当作成功 checkpoint 输入。

封段时先持久化只读 sidecar（first/last sequence、bytes、count、hash），再发布根；新segment、sidecar、epoch目录及其新增父目录项都须在根发布前fsync。仅根中的sealed_segment_count决定sidecar是否已提交。崩溃后超出此水位的sidecar/新段不可见，下一写入方持锁删除或重建，不能给继续追加的active段沿用过期hash。旧快照按自己记录的字节边界读取，后续 append 不改变已提交前缀。ordinal 连续，读者通过 sidecar 的序号范围二分定位水位所在段，不把所有文件名或 segment 元数据加载为列表。

### 4.3 读取、损坏与保留

新增内部接口 `read_checkpoint_head()`、`read_checkpoint_record(snapshot)`、`iter_checkpoint_events(snapshot, after_sequence, through_sequence)` 和有界 page。head只返回小型根引用/epoch/watermark，不解析完整record；后者按需读取，其completed_task_index/progress等本阶段没有新增容量上限。append只验证事件结构、head中的epoch/序号及与该事件相关的规则，不调用会解析完整record的通用验证路径；现有验证要求应拆分，不能绕过必要检查。迭代器逐 frame 读取并校验连续序号、epoch、长度和 hash。

投影用有限 deque 取所需近期 delta；候选验证只读取指定前缀/序号范围；需要全部未应用事件的旧接口保留兼容，但在线调用方逐一迁移，不能仅把 `state.events` 偷换成最后 5 条。用于语义归纳的输入仍按当前消息边界选择，不丢未应用事实。

只允许移除根的 committed_bytes 之后的未提交尾部。已提交前缀中间损坏、缺段、序号跳跃、epoch 不符或 hash 不符均停止该 Chat 的 checkpoint 写入并返回可诊断错误，不能沿用“跳过坏 JSON 继续”。读者发现损坏后持chat锁写入并fsync固定的`journal-v2-quarantine.json`标记（仅错误位置/类型，不存正文）；所有Pod的append、reset、checkpoint安装和迁移入口先检查该标记并拒绝写入。仅在停写维护、完整验证修复后显式解除，不自动清除，不新增后台全盘扫描。损坏不影响其他Chat；如果NAS本身不可写导致隔离标记不能持久化，则报告隔离失败并要求停止该租户写入，不能宣称已实现跨Pod隔离。

**本阶段不删除任何已提交 journal 段，也不回收旧 epoch/generation。** 先解决内存与追加 I/O；磁盘保留和安全 GC 后续设计。这样旧 reader snapshot、候选与显式旧 epoch evidence 不会因本阶段新增清理失效。Chat 删除仍先 tombstone 后删除整个 Chat 归档目录，包括 v2 文件，锁与 tombstone 位置保持现状。

manifest 边界列表、evidence map、epochs 历史仍可能增长，本阶段只隔离它们与每次 append 的热路径，不宣称都已变为有界。归档 batch 改为按行定位、读取所需页后停止，保留 UUID/行号和 cursor；单条大 Msg 与完整响应仍按自身规模分配内存。

## 5. 2B：有界热 turn 与冷历史

### 5.1 保留与归档规则

`turn_states` 保留所有未结算记录，以及最近终态记录。终态窗口初始上限为 **64 个 turn 且 256 KiB UTF-8 数据**（两者同时满足）；按 admission 顺序从最旧终态移出。当前选定 turn、结算失败待重试 turn 和孤儿 admitted 都受保护，不能为了满足预算丢弃。

只有在此前 revision 中已经成功持久化终态的记录才可冷移出；terminal commit 本身先沿用原结算路径，后续 commit 才归档。`admitted` 或未知状态不能归档。保留语义为 completed/stopped/failed 及旧 cancelled 原值，外部映射沿用现状。超过预算的单条终态允许在下一次独立维护事务中冷移出，不截断正文；活动/受保护记录不受此硬预算，统计单独呈现。

热窗口是 session 级。每条记录保存稳定 admission_order、原 msgid、chat_id、created_at、status 和完整 message；迁移按原 dict 插入顺序分配 admission_order，不按时间戳重新排序。冷移出后正文及权威终态均存盘，不只留下难以恢复的摘要。

### 5.2 文件布局与可见性

```text
sessions/<existing-session>.json        # 提交根，schema_version=3
sessions/.turn-ledger/<session-generation>/
  metadata.json                       # session文件身份与ledger版本
  turns/<hash-prefix>/<sha256(turn-id)>/<revision>.json
  references/<hash-prefix>/<sha256(turn-id)>/<revision>.json
  latest/<hash(chat-id-or-legacy)>/<revision>.json
  pending.json                          # 单个未完成提交意图
```

schema v3 增加 `session_generation`、`turn_ledger_version` 、cold_turn_count、latest_heads（实际字段 ledger_latest）和下一 admission_order；session revision 仍单调递增。冷文件保存完整 turn、generation、revision、提交 nonce、原始身份和正文 hash；latest 保存此 Chat 的最新 turn 引用和顺序，防止恢复时扫描全部冷 turn。hash 只用于路径，读后仍校验原始身份；路径不由用户拼接。

冷文件按版本不可变。point lookup 先看热状态，再访问确定的 turn 目录，用 `os.scandir` 选不超过当前快照 revision 的最新版本，逐项保持常量大小候选；不能全目录 sorted/list。允许同 msgid 后续更新产生新版本，不原地覆盖旧读者可见文件。没有跨会话常驻索引。

root 的 ledger_latest 按 Chat 桶保存已提交 head revision/hash；latest 节点校验 generation、桶身份、hash及严格递减的前驱 revision/hash，缺文件明确失败。它按 Chat 数增长，未保存每条冷 turn 的常驻 ID 索引。latest每个revision保存至多64个此次冷移出的候选引用（含admission_order），并指向此前latest版本，不只保存最后一个候选。恢复先流式选出本快照可见的最新latest版本，再逐候选经`get_turn`读取当前权威状态并重验chat_id；旧版本已变更归属时回溯前一个候选/版本。合并热状态、Chat专属桶与legacy无chat_id桶后按admission_order选择。若t2原属A后以同msgid新正文重入B，A应回到t1，不能使用t2旧completed。目录版本枚举及异常回溯仍有元数据I/O，最坏随该Chat版本数增长，但不读取所有冷正文；首版不承诺latest严格O(1)，基准须单列这一成本。

历史遍历流式扫描有界记录；需要完整有序返回的兼容接口最终仍会积累并排序完整响应，单独标明成本。后续若 NAS 小文件成本不可接受，应另行比较现成数据库或磁盘索引，不能在本阶段临时自建复杂 B-tree。

### 5.3 session 提交协议

复用同一 session execution/短写 lock；持锁恢复 pending 后才能开始新写入。冷移出每次最多处理 64 个记录，控制 intent 和新增对象数量；单条遗留大记录不截断，按流式编码写盘，内存仍受最大记录大小影响。

1. 确定 revision `R+1` 和 nonce；原子写 pending（generation、base_revision、target_revision、nonce、计划文件路径）；先 fsync intent。
2. 创建不可变 turn/latest 版本文件，流式编码、fsync 文件及新增目录。不修改旧版本；失败时 session 仍指向 R。
3. 流式写新的 session 临时文件，热窗口排除已归档记录；携带 nonce、revision、generation，按现有 fsync/replace/目录 fsync 发布。该 replace 是唯一业务提交点。
4. 更新事务内 snapshot；删除 pending 并同步目录。取消必须等底层磁盘事务结果已知才释放锁，不因 to_thread 取消假装磁盘已停止。

恢复先完整校验 intent 的 revision、nonce 和路径白名单，路径只能指向 target_revision 的 turns/references/latest 文件，不能删除metadata或已提交旧版本。session 根已是 target_revision 且 nonce/generation 一致，验证 intent 列出的有界文件集后视为提交成功并完成收尾；仍是 base_revision 则删除 intent 列明的新版本再清 pending；其他组合报错并禁止新写入。不可忽略 pending 后继续复用 revision，否则未提交文件可能在将来错误变为可见。锁外 reader 按它一次读取的根 revision/generation 过滤，只看到旧或新完整视图。普通 commit 也必须先恢复 pending。

没有 session JSON 就没有可见 ledger。手动删除 session 后新请求分配新 generation，不扫描或恢复旧 ledger；旧文件保留为孤儿而非自动复活。备份/恢复必须作为整体包含 session 根和其 ledger，缺失引用文件报错，不能静默当空。本阶段不新增历史或孤儿 generation 的自动 GC；现有 Chat 删除保留 Logical Session，手动删除根后旧 generation 不再可见。只删 Chat Record 仍遵守原 session 独立保留语义，Chat 鉴权与 chat_id 过滤不可省略。

### 5.4 查询、重试与兼容

引入明确的内部 facade：`read_execution_snapshot()`、`get_turn(snapshot, msgid)`、`get_latest_turn(snapshot, chat_id)`、`iter_terminal_turns(snapshot)`、`get_turn_statuses(snapshot, selected_ids)`。普通执行只读热 session；只在检查同 msgid 或显式查询旧 turn 时读对应冷文件。`get_session_state_dict` 对已审计的内部存储调用返回物理热状态，不能在通用 getter 中自动补齐全部冷历史；对外历史由专门投影层合并。

- admission 查询热/冷原记录，直接按既有Python dict相等语义比较完整message。SHA-256只做文件完整性校验，不能用JSON hash不同判定message不同（Python中1、1.0、True可能相等）。相同 message 沿用原幂等行为；不同 message 同 msgid 保留当前更新行为，不新增未经批准的 409 契约。更新后的热版本覆盖较旧冷版本，并保留原 admission_order。
- terminal mark、Stop 和孤儿恢复统一通过 facade 定位；不能在热 dict 未命中时直接创建缺少正文的状态而遮蔽冷记录。恢复取最新权威顺序，不靠回填 dict 的插入位置。
- answer-turn 状态与 chat_id 按 msgid 查权威记录；旧接口只在原本能找到的消息范围内返回，不无意扩大历史可见范围。相同 Logical Session 下多个 Chat Record 继续按 chat_id 绑定和现有兼容规则筛选。
- 分享资格使用权威 status，包括冷记录；历史选项枚举可流式扫描，创建分享只解析所选 turn 的状态和相关消息，不能用“有 assistant 输出”替代完成状态。既有不可变 Shared Conversation Snapshot 不受影响。
- `GET chat`、终态 recovery 的完整 ChatHistory、既有 archive 分页和稳定 message ID 保持语义。为兼容缺失用户锚点，需要时流式补冷记录，沿用归档去重、隐藏上下文脱敏和审批标注；不得把所有冷 user anchor 放进 compaction archive 或伪造压缩边界。
- 不把终态 cold record 自动重新加入 Agent memory。`/new`、`/clear` 保留既有 epoch 行为，正文历史是否展示继续由原接口决定；冷存储不是新的模型记忆来源。

所有直接修改`turn_states`的写入方必须纳入审计：合法单条删除要写版本化tombstone并通过相同提交根发布，不能只从热dict删除后让旧冷版本重新出现；整session重建使用新generation。首版不新增自动历史删除功能。v3开关不得在这些writer仍只理解热dict时启用。

### 5.5 减少复制

session JSON 写入采用 `JSONEncoder.iterencode` 写同目录临时文件，避免先创建完整 JSON 字符串；仍保留原子替换和 fsync。对象解析继续使用标准库，不为本阶段引入流式 JSON 依赖。

普通执行不做冷历史 hydration；减少一个请求中重复的 `_read_history_state`，将同一次快照显式传给历史/status 投影。历史规范化优先只转换需要显示的 message，不对整个 root 递归克隆。不能直接把共享可变 dict 交给会修改输入的 AgentScope 方法；具体边界用真实库行为测试验证，保留请求独占 memory lease。

## 6. 升级、迁移与回退

两个格式分别启用，2A journal v2、2B session v3 互不依赖上线。初始保持旧格式写入；新版 reader 能读旧版并处理新版。一旦一个对象已有新格式根，即使开关关闭也继续按其格式读写，不准在读不懂时退回旧备份。

1. 全部 Pod/worker/CLI/cron/维护工具升级；暂停调度并排空旧 writer，确认旧版本为零。仅“代码部署成功”不足以开始转换。
2. 迁移工具先 dry-run：统计记录、字节、损坏、未知 schema、pending/未结算和路径；不自动跳过坏数据，不修改源文件。
3. 每次持对应锁转换一个 session/Chat；先冻结源 revision/epoch，流式写新文件，再发布根。session 首次标准库解析仍可能占用旧文件大小，迁移 worker 单并发并在有足够内存的维护进程运行，不承诺首次迁移固定内存。
4. 旧 events 逐行严格验证各 epoch 序号、重复 ID 和 payload；遗留超大事件保持原文存入独立超大段，记录诊断，新增写入才执行 64 KiB 限制。存在中间损坏则该 Chat 不转换。
5. 按租户 allowlist 灰度，新建对象按开关选择格式，存量采用显式迁移。先一个租户/Pod 观察，再扩大；不在用户首次请求中偷偷迁移巨型文件。
6. 回退到仍支持 v2/v3 的新版代码可直接关新建格式开关；**回退旧二进制必须先停写并离线导出**：合并 ledger 回完整 turn_states，按 admission_order 恢复；journal 按 epoch/sequence 导出旧 events，同时导出当前 checkpoint/manifest/evidence/epochs。核对 hash、记录数和 revision，原子完成后才启动旧 writer。

保留迁移前备份不等于可直接回退：启用后发生的新写入必须包含在导出中。备份、删除、cron cleanup、归档维护和恢复脚本都须识别新目录；未完成该清单前禁止灰度。

## 7. 验证与验收

只用项目 `venv/bin/python -m pytest`。先基线再相同合成数据对照；不以 RSS 是否立即下降判断引用释放。

| 场景 | 可验证标准 |
| --- | --- |
| 10k 与 100k checkpoint events | 单次 append 不调用全量 reader/整日志 rewrite；常态读写量只含固定根、当前段及单事件，封段只额外读取当前段；历史数量增加不扩大 append 对象集合 |
| 水位读取 | 已应用旧段不解码；投影只保留要求的 deque；归纳消费完整指定前缀，保留水位后并发追加 |
| 10k 与 100k terminal turns，固定活动 Agent | 普通新 msgid admission 至多读热 root及该 ID 的确定路径，不遍历所有冷记录；热终态满足64条/256KiB，受保护状态单列；tracemalloc峰值不随冷历史总数线性增长 |
| 精确查询与分享 | 旧 completed/stopped/failed、无 assistant、重复 msgid、不同 Chat Record、旧 cancelled 与选取非连续 turn 均保持原结果及鉴权 |
| 故障注入 | 在 intent、每次fsync、根replace前后、收尾阶段 SIGKILL 后重启，旧/新提交各自完整，不丢记录，不读取未提交文件，不误恢复已删除 session |
| append 损坏 | partial write/未发布尾部可恢复；已提交中间坏帧/缺段/序号间隙必须失败关闭，其他Chat不受影响 |
| 并发与取消 | 两进程/两Pod争锁；取消worker仍写盘时不提前释放；checkpoint reset与append并发只能有一个epoch生效；条件重试不重复同槽位事件 |
| 兼容 | 旧session/events、稳定messageID、cron增量与execution_key、Stop、审批恢复、重启孤儿、完整ChatHistory、archive cursor和分享红线回归通过 |
| 部署前提 | 生产等价StorageClass验证跨客户端flock、锁持有Pod退出、rename可见性、文件及目录fsync；不支持就停止启用，不降级无锁写 |

记录聚合 `session_hot_terminal_count/bytes`、protected_count/bytes、ledger_lookup_read_bytes、journal_append/read_bytes、frames_decoded、recovery_failures、migration_failures 和提交耗时。不得以 session/msgid/scope 原值作为高基数指标标签，不记录正文/secret。

不设置未经测量的“降低50% RSS”目标。验收报告同时列出 Python 分配峰值、进程RSS、NAS读取/写入量、耗时P50/P95、数据体积、并发与活动Agent大小。完整历史和大选择分享按输出规模增长，manifest/evidence及磁盘历史增长列为未解决项。

## 8. 实施拆分与评审门禁

2A：存储根及帧协议 → lock内append和条件重试 → 按水位迭代及调用方接入 → 迁移/导出及崩溃验证。

2B：turn字段与writer/reader清单 → facade与冷版本提交 → admission/terminal/recovery/sharing接入 → 流式session写及去重复读取 → 迁移/备份/删除适配 → NAS及合成负载验收。

建议分别提交和灰度，避免同时改变两个恢复协议。实施前每个修改符号运行 impact 并报告HIGH/CRITICAL/UNKNOWN；提交前 detect_changes，不把空图当作零影响。设计批准后才开始实现；实施进度及尚未完成的生产门禁见 OpenSpec tasks。

设计自查及独立审查已补齐：旧epoch最终根、未发布sidecar清理、目录fsync、head不隐含完整record有界、Python dict相等语义、跨Chat重入后的latest回溯。仍需实施验证的风险是NAS能力、小文件成本与故障恢复行为，不能以文档审查代替压测。


## 9. 实现与操作说明

- 第一阶段默认由 lifespan 每 **60 秒**主动清理 push、suggestions/Q&A 和 summary 完成记录，各存储分批让出事件循环；TTL不变，不依赖用户再次访问。空闲条件下物理回收预算为 TTL＋60秒＋一次分批扫描。
- 新对象分别通过 `SWE_CHECKPOINT_V2_TENANTS`、`SWE_SESSION_V3_TENANTS` 租户列表启用，默认空。已有新格式根具有粘性，关闭列表不会降级。存量对象只能显式迁移。
- 维护命令：`swe memory-storage session --sessions-dir DIR --session-id SID [--user-id UID] preview|migrate|validate|export`；journal使用 `--dialog-root DIR --chat-id UUID`。`migrate`默认预览，`--apply`才写；`export --destination PATH`拒绝覆盖。迁移与回退须停写、单并发运行。
- session预览只读，不恢复pending；实际写入在锁内恢复。损坏JSON及未知schema拒绝覆盖。完整validate/export会读取全部已提交冷记录；正文与reference同时被外部删除时，点查询无法区别未知ID，完整validate通过cold_turn_count检出，因此上线前及备份前须完整验证。
- Python备份前后核对提交根，存在pending或读失败即拒绝备份；新格式恢复要求空目录，禁止合并旧generation。旧shell备份/恢复脚本遇新格式保守拒绝，应使用Python一致性备份工具。session原子导出需要同目录POSIX硬链接，生产NAS验收须覆盖。
- answer-turn及分享创建只点读所选冷turn；旧归档消息检索仍扫描归档页，完整历史/分享选项与完整导出按输出量占用内存。活动Agent、未结算状态、单条大记录、manifest/evidence和latest桶元数据仍是增长边界。
- 不自动删除已提交journal/旧epoch/ledger版本，不宣称端到端exactly-once或固定进程RSS。生产StorageClass跨Pod flock、rename/fsync、崩溃恢复和回退演练尚需部署环境验证，完成之前禁止启用新格式。
