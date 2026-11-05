# 引用查询批量化：首批边界与一致性细化

日期：2026-11-05。基线 `f66a603ba`。状态：详细设计，未实施。

本文细化[SQL/NAS 成本总体设计](2026-11-05-conversation-sql-nas-cost-design.md)的 B1，不包含 pin/dependency INSERT、NAS 批读、缓存或写入准备。

## 1. 分成两个可独立验收的包

| 包 | 范围 | 主要风险 | 退出条件 |
| --- | --- | --- | --- |
| B1a | 现有事务阶段内的 projection/source/plan 引用捕获和逐 Message 复核 | 字段遗漏、去重掩盖冲突、单批返回过大、驱动 packet | 查询次数随实际批次数增长，原归属与 locator 校验不变，事务/锁类别不变 |
| B1b | 全量 Chat/History 跨批复核、其他无界 IN/UPDATE、publication 联合锁定 | 多 SQL 时点、锁顺序、只读事务与连接状态恢复 | 明确一致性语义、真实双连接与 EXPLAIN 验收；完整响应无截断 |

B1a 不依赖新增只读事务；原本已有的 transaction、FOR SHARE 和普通读阶段保持。B1a 完成不能宣称“所有无界 IN、所有 publication 逐行锁”已消除。

## 2. B1a 涉及的实际调用链

### 2.1 Projection 捕获

`snapshot_projection` 已在 transaction 中调用 `_snapshot_rows` 并创建 ReaderPins。新实现先收集唯一 Message/Turn ID，批量查两类窄行，再按原 refs 顺序逐项校验。

- 同 Message ID 的完全相同 ref：只查询一次。
- 同 Message ID 指向不同 publication/offset/length：查询一次，但逐 ref 检查，至少一项失败，不能覆盖前一个 ref 后当作成功。
- 同 publication 上多个不同 Message：只减少 publication ID 查询，不合并 occurrence。
- same-publication 的 block_name ref 没有 Message/Turn ID，不新增无意义 SQL；其 block 校验仍由原后续逻辑完成。
- 缺 Message、错 Session、deleted、locator 不匹配，继续使用原 IntegrityError；在 NAS 读前拒绝。
- Turn 仍校验 service、Session、terminal_publication_id；独立 Turn block、question key、history_status 的正文校验保留。

### 2.2 Source 捕获

`snapshot_sources` 在原 transaction 内批量读取唯一 source_message_id，随后逐原 plan.messages 调用 `_validate_row`，并按原输入 ID 顺序核对 session_order。

不允许用 SQL 返回顺序替代输入顺序。原本重复 source_message_id 被拒绝的规则保留；即使 helper 支持集合去重，这个领域约束也必须在调用 helper 前检查。

pin 与 locator 同事务，NAS 在事务外，失败与取消仍走原 finish/drain。此包不修改 PinBatch 或 ReaderPins，也不扩大 4096。

### 2.3 最终 Message 复核

`ProjectionReferences.revalidate`、`SourceMessages.revalidate` 在原 commit 事务内，对每类 Message 集合使用分批 FOR SHARE，随后与捕获行逐字段比较。

- Source 使用 SOURCE_FIELDS 全集；Projection 保留自己的原字段集合，特别包含 `in_context`。
- 首包不合并两个领域的 snapshot；一条 Message 可能仍被两类 snapshot 各复核一次，但从逐行 SQL 变成各自分批。之后只有保留字段并集才能合并查询。
- Turn 复核保留原普通读类别，不在性能包中偷偷改锁强度。
- 中间/末批缺项或字段变化都失败，并由原 commit 外层事务回滚；已经创建的 reader pins 仍在原 finally 中释放。

### 2.4 Plan 归属检查

`PublicationStore._validate_plan_reference_ownership` 先汇总 source/prune/share proof Message ID，分批读归属列，再汇总关联 publication ID，分批读 scope/Session 列。

reserve、write 前检查、commit 前检查仍分别执行，不建立跨阶段的“已验证”缓存。所有 owner/generation/revision 和最终 association 锁继续由原流程负责。

## 3. 查询模块与固定模板

建议新增 `src/swe/app/conversation_storage/reference_queries.py`，只提供这五种内部固定查询：

| 查询种类 | 返回列 | 锁 |
| --- | --- | --- |
| Message locator | 当前 Message 的有界标量列；包含 SOURCE_FIELDS、Projection 比较列及 id/session_order | 无 |
| Message current locator | 与前项相同 | FOR SHARE |
| Message ownership | id、service_unit_key、session_id、publication_id | 无 |
| Turn reference | id、service_unit_key、session_id、chat_id、terminal_publication_id、history_status、question_msg_key | 无 |
| Publication ownership | id、service_unit_key、scope_kind、tenant_key、source_key、agent_key、session_id | 无 |

查询始终携带 service_unit_key；领域继续检查 Session 和完整 scope。模板由内部 enum 选择，不接受调用方任意表名、列串、WHERE 或锁片段。

publication 的 `result_metadata_text` 最大可达既有 plan 1 MiB 上限，Turn 的 execution_identity_text 也不是本次所需列。因此禁止在这些批量查询中 SELECT *。Message 当前 schema 只有有界标量，但仍建议显式列集，并用测试核对所有原比较字段均在列集中。

内部接口约定：

```python
async def fetch_reference_rows(database, query_kind, ids, service_unit_key):
    """Return id-to-row for a bounded sequence of trusted fixed SQL queries."""
```

契约：

1. ID 必须是正的 unsigned bigint，拒绝 bool、负数、超范围值。规范化为唯一、排序后的整数；空集合不执行 SQL，也不获取新连接。
2. 接受可信 bytes service key，并限制到当前 schema 的长度；同 task 使用 `db.acquire()`，已有事务复用原租约。
3. 返回字典不依赖 SQL 返回顺序；出现重复行 ID、返回未请求 ID、错误 service 时抛 IntegrityError，不保留“最后一行”。
4. 缺项留给领域使用原错误语义处理；各调用方必须检查自己要求的全部 ID，不能只遍历返回行。
5. 一次阶段内所有批次顺序 await；禁止 gather 共享 session。helper 不创建事务、不提交、不 pin、不做 NAS、不保存跨调用结果。
6. helper 只处理整数 ID。source key、cursor、archive_order 等集合不强行复用这条接口。

## 4. 查询与 packet 预算

256 IDs 是初始上限，不是无条件批大小。还要考虑实际物理连接的 packet 上限、模板长度、scope 参数字节和 protocol 余量。

当前物理连接初始化已经记录 `PoolEntry.max_allowed_packet`，但未公开到查询调用方。B1a 建议只增加 `DatabaseSession.max_allowed_packet` 的只读属性，让同一 `db.acquire()` 阶段取该连接的已验证上限；不为每批额外执行 `SELECT @@max_allowed_packet`，不读取业务配置寻找参数，不把 pool 私有字段引用散到领域模块。

切批规则：

- 使用 `min(64 KiB 查询预算, 已验证 packet 的保守预算)`；packet 预算沿用现有 prepared 路径的头部余量和安全比例原则。
- 计入固定 SQL、service 参数最坏转义长度、每个 uint64 至多 20 位十进制及分隔符；逐批取不超过 256 个 ID。
- 单个合法 ID 加 scope 都无法容纳时，在 SQL 执行前明确抛出 StorageUnavailable，不发出超容量查询，也不返回部分成功。
- 只计请求字节仍不等于限制响应内存，因此要使用上一节的窄列和有界行数。完整 snapshot 的总行数/内存下界仍存在，不声称本包解决全状态容量。
- 初始对比 64/128/256/512 上限；在真实驱动/专用库验证后选择发布值。返回成本断言按实际预算得出的批数计算，不能硬编码“永远 ceil(N/256)”。

所需数据库改动仅为读取现有 packet 能力；不修改 pool 大小、连接预留、事务隔离或 baseline 协议。

## 5. 错误与 fixture 更新

生产错误处理继续原领域规则：捕获阶段 IntegrityError，当前 locator 变化由原调用方失败，未知事务结果仍按原 UnknownCommit 收敛。helper 的结构性返回错误不得转为空集合。

以下测试替身需要同步支持有界集合查询，不能通过无限返回所有行来让测试变绿：

- `tests/unit/app/conversation_storage/test_publications.py:PublicationDatabase`：新增模板对应的 fetch_all 分支；保留 publication operation 点查、guard/fence 和故障注入。
- `test_projection_scale.py:HistoryDatabase`：根据本批 ids 与 service 返回准确行，记录每批 SQL/参数；原“返回全部 messages”行为必须收窄。
- `test_source_messages.py`：locator 改动测试改由 fetch_all 返回当前行；保留原 SOURCE_FIELDS 全字段差异检查。
- service isolation 的数据库替身仍能返回错误 service 的行，以测试 helper/领域拒绝，而不是仅在假查询里过滤后跳过校验。
- 基础替身提供 acquire 的 async contextmanager 和正的 max_allowed_packet 能力；不能通过在生产 helper 中 `hasattr` 探测后退回逐条查询来兼容旧测试。

## 6. B1a 验收与成本断言

| 测试层 | 场景 |
| --- | --- |
| helper | 0/1/B−1/B/B+1、4095/4096/4097/8193/16385；bool/非法 bigint；乱序、重复/额外返回行；小 packet 触发更小批次与完全无法执行 |
| projection 捕获 | 重复相同 ref、重复 ID 冲突 ref、同 publication 多 occurrence、same-publication block、Message/Turn 缺项/错 service/Session/locator |
| source 捕获 | 输入源顺序、重复 source 拒绝、deleted/current_visible/context_epoch/typed identity；缺项在 NAS 前失败 |
| 提交复核 | 每个原比较字段分别变化；中间/末批变化拒绝；保留 FOR SHARE 和原外层事务；source 与 projection 各自字段不遗漏 |
| ownership | source/prune/share proof 合并集合；缺 Message/pub、错 service/full scope/Session；三个发布阶段仍重新验证 |
| 资源 | 无 NAS 时不引入 executor；空集合零 SQL/零 acquire；已有事务无多余 BEGIN/COMMIT；每阶段一次租约，不跨 task；取消/异常仍清理所有既有 pins |

成本断言分别统计 Message locator、Turn locator、Message ownership、publication ownership，不把 pin 成员 INSERT 的成本混入本包收益。4097 个独立 Message 引用在 256 IDs 预算足够时，捕获 Message 查询从 4097 次变为 17 次；pin 和正文读取在 B1a 仍按原路径进行。

真实 SQL 验收使用显式专用测试库，验证固定模板、packet、同事务复用、分批 FOR SHARE 与 writer/GC 交错。缺库只能 skipped，不能把替身计数称为真实数据库性能结果。测试配置不得读取或修改 `src/swe/config/envs/dev.json`；新增集成模块必须在 fixture 执行前检查专用库是否明确配置。

## 7. B1b 的明确前置条件

B1b 需要单独实施计划，先确定以下协议后替换全量复核：

1. NAS 后的 read-side 全集合复核在独立短只读一致快照完成；事务必须显式 REPEATABLE READ + consistent snapshot，不能假定默认值。
2. 禁止该只读事务嵌入已有写事务；pin/当前锁定读继续原流程。成功、错误、取消、开始事务失败之后都验证会话 baseline，不污染下一租约。
3. 捕获 Chat/Session 身份、原比较字段与必要 high，跨批成员比较不漏项；需要的最终当前 head/授权检查在快照外执行，不能又读到同一旧快照。
4. `HistoryStore._snapshot` 的 anchor IN、Chat 全量 `_revalidate`、share proof、archive/prune 等集合查询/更新逐个明确语义。UPDATE 分批仍在原原子事务，不在本包顺手改错误响应。
5. source/current publication 联合锁定不能批量携带全部 plan 文本。旧依赖可窄列分批锁，current 仍需完整行做 `_matches`；依赖必须 `< current.id`，锁顺序、GC/fence 交错通过双连接验证后才替换。
6. 成功标准不是“ORDER BY id 就没有死锁”。记录实际 EXPLAIN、扫描范围、等待与失败路径，保持不确定提交不盲重试。

## 8. 实施顺序

[单次 hydrate 工作包](../reports/2026-11-05-conversation-single-hydrate-implementation.md)已完成实现和独立回归。下一步按本文件形成 B1a 的代码级实施计划，首先固定查询模板、packet 能力和替身契约，再迁移捕获/复核调用方。B1b、B2 成员写入、NAS/缓存工作包继续分别推进。

这项拆分用于避免一个“批量化”提交同时修改数据库事务协议、publication 锁顺序、正文 IO 和路由授权，方便分别证明性能收益及正确性。
