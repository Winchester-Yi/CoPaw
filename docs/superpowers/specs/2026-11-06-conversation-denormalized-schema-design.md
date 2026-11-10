# 会话元数据迁移：减少关联的反规范化方案

日期：2026-11-06，更新：2026-11-09。状态：Chat 纯 SQL 写入、Session ID 冗余及状态投影已进入实现；旧内容来源表和只同步元数据导入仍为设计稿。未修改实际数据库。替代[三张内容支撑表草案](2026-11-06-conversation-content-schema-design.md)的推荐方向；实际库情况仍以[只读审计](../reports/2026-11-06-conversation-database-schema-audit.md)为准。

字段收敛、单来源表 CREATE 草案、状态 writer 矩阵和分包见[SQL 契约与实施拆解](2026-11-06-conversation-sql-contract-review.md)。checkpoint head 继续只指 publication，不在 Chat 维护第二份 source head；普通来源无需机械增加反向索引。SQL 尚未执行或在目标 MySQL 验证，内容分类/真实旧样本及大描述预算仍是放行条件。

## 1. 先明确“旧内容”和“新 bundle”

旧内容指切换前已经存在 NAS 上的会话 JSON、JSONL、Session root、Turn ledger、checkpoint journal 和分享 snapshot。工具读取这些文件并提取元数据，但不改路径、字节、权限，不额外复制或重打包。

新 bundle 指当前迁移分支为切换后的内容写入采用的文件格式：一次保存把新增消息及必要恢复内容组织到一个不可变内容包，数据库记录发布和定位。它仍在 NAS，不是数据库中的正文，也不是把旧文件换一个名字。

例如旧会话已有 100 条消息，切换后增加两条：原 100 条仍来自旧文件，新两条及必要恢复内容写入新 bundle；数据库索引负责组合。第一次新恢复保存可能还需要保存未知模块等必要状态，不能承诺每次只写新增消息字节，但不因此在迁移时转换全部历史正文。

bundle 是现有实现的选择，不是“元数据进数据库”的必然要求。本次为减少改造与保留已验证发布机制，继续使用它；若未来改变新内容文件格式，应独立评估，不与此次旧内容兼容强行合并。

## 2. 修订结论

**撤回“所有内容统一进入 objects/views/parts 三表”的优先建议。推荐只新增 legacy_sources 一张业务支撑表，专门描述旧内容来源。** 新 bundle 保留 Publication 的现有路径、格式、状态、pin/GC 和未知提交对账，不再给每个新 bundle 添加 object 行。

| 方案 | 查询和存储特点 | 选择 |
| --- | --- | --- |
| 每条业务记录内联完整路径与组合清单，零新增表 | 普通读取少查一次，但重复长路径/指纹；组合清单随消息大量复制，行宽和校验成本高 | 不采用全量内联 |
| 一张 legacy_sources，成员清单内联为有类型元数据 | 普通消息仅存整数引用，按页批取；组合恢复只查一行描述，无成员关联 | 推荐 |
| 三表统一 objects/views/parts | 适合组件需要 SQL 反向查询和跨对象物理回收的场景 | 本次旧文件不回收，其额外模型/行数收益不足 |

新增一张业务表不包含此前需要预建的两张迁移控制表；两者继续只服务作业、ID 映射、断点和 SQL 安装计划。

反规范化只复制小型、稳定或可原子维护的数据，不把消息正文搬进 SQL、不将复杂 manifest 复制进每条消息，也不把迁移日志当线上永久来源。

## 3. legacy_sources 的有限职责

source 是一个不可变、带业务归属的旧内容来源。它可以描述一个真实旧文件，或一个固定格式的多文件恢复单元。它不是全局文件目录，不引用 managed bundle、其他 source 或临时迁移日志，不提供任意表达式/插件/递归依赖图。

建议字段：

| 字段 | 类型建议 | 用途 |
| --- | --- | --- |
| id | BIGINT UNSIGNED PK | 业务 locator 的整数引用 |
| service_unit_key | VARBINARY(256) | 服务隔离 |
| scope_kind/tenant_key/source_key/agent_key | 沿用明确 scope 类型及精确字节身份 | 读取和备份的授权边界 |
| session_id/chat_id | BIGINT UNSIGNED NULL | 有可靠业务归属时登记；孤立分享允许为空 |
| legacy_key | VARBINARY(64) | 作业首次分配后稳定的逻辑来源键，重试复用；不按正文 hash 去重 |
| root_key/root_binding_version | VARBINARY(64)/BIGINT UNSIGNED | 可信部署根及版本，不接受 SQL 中任意绝对路径 |
| source_kind | ASCII VARCHAR(16) | extent_file 或 composite |
| resolver_key/descriptor_version | VARBINARY(64)/INT UNSIGNED | 固定格式解析器及描述协议版本 |
| relative_path/file_byte_length/file_sha256 | VARBINARY(1024)/BIGINT/BINARY(32)，可空 | 单文件来源的真实路径、长度、指纹 |
| manifest_text | MEDIUMTEXT NULL | 组合恢复的无正文文件清单、角色、版本和提交边界 |
| descriptor_sha256 | BINARY(32) | 规范描述完整性；不代替每个源文件的校验 |
| created_at | DATETIME(6) | 登记时间 |

唯一键为完整 service/scope + legacy_key；按主键批取时仍校验 service 和完整归属。备份直接按完整 scope 与 legacy_key 有界分页，复用此唯一键，不另补冗余 scope/id 索引；不加 path 唯一、hash 查找、组件反向或 GC 索引。

extent_file 的 direct 列必填、manifest 为空；composite 的 manifest 必填、direct 文件列为空。descriptor 严格版本化，source 登记后不可原地改写。相同物理文件允许在不同 scope 或不同恢复单元中重复描述，因为它只读不变；不增加全局去重表，也不因此扩大跨租户读取权限。

root 绑定与 source 一同进入业务恢复集合。manifest 只能列该受信任根内的真实旧文件，包含角色、真实长度、指纹、已提交字节范围及必要 generation/epoch/watermark；不存消息、摘要、完整恢复状态或工具结果。

### 组合清单的容量边界

不把整个 Session/Chat 目录和全部历史 epoch 无界塞进一行。按真正可独立恢复的单位登记，例如单个 Turn record、archive batch、checkpoint epoch；能够真实字节定位的内容优先采用 extent_file。

manifest 单行序列化上限、单请求累计描述字节和重建对象容量分别有界，且单行必须满足实际 packet 的安全预算。MEDIUMTEXT 只是物理类型，不能作为“任意历史规模都支持”的承诺。具体应用预算由 P0 样本统计冻结，新增预算与来源分类应先在预检输出。

如果某个不可拆恢复单元确实超限，应在导入前报出该对象和实际预算差异；不能静默裁剪、重写 NAS、把正文塞 SQL 或隐式建立 source→source 图。checkpoint retained epochs 和 Session ledger 的真实大样本是此方案的放行条件。

## 4. 业务 locator 保留三个明确分支

| 分支 | publication_id | legacy_source_id | byte_offset/length | selector |
| --- | --- | --- | --- | --- |
| 新 bundle 内容 | 必填 | NULL | 真实 bundle block 范围 | NULL |
| 旧文件真实内容 | NULL | 必填 | 真实文件字节范围 | NULL |
| 旧组合恢复结果 | NULL | 必填 | NULL | 固定 resolver 的有类型选择条件；完整结果可为空 |

当前 Message 的 publication_id 非空、offset/length 非空限制需要相应调整，绝不将 offset 解释为数组序号。Session、Turn、Chat 内容头、归档/checkpoint/event locator 和 Share 按各自读取需求增加 legacy_source_id 与受限 selector；不机械给每表复制全部通用字段。

Publication 继续代表真实操作；新 bundle publication 保持原协议。为兼容归档边界/checkpoint 批次而保留的导入索引记录，必须明确属于 SQL 索引导入操作、允许没有 bundle，路径等字段仅在这一显式类型下为空；不伪造过去的历史发布。纯元数据 Chat 操作直接 SQL，不创建这类 publication。

归档边界/checkpoint head 的 SQL 元数据继续保留在现有表，不因来源表而减少必要索引。原 content-only 三表方案的“全量移出 Publication 文件字段”不再进行。

旧 Turn 的已准入/可读取判断不能只依赖 admission/terminal publication 非 NULL。新 projection 的旧消息引用需要独立版本，携带 occurrence/Turn 身份与冻结 locator 校验；保留旧 projection 解码，不能把可变 Turn 指针直接当作不可变内容引用。

## 5. Chat 的有界反规范化

为让 summary 常见路径只查 Chat，建议：

1. 在 Chat 冗余 `external_session_key VARBINARY(1024)`；创建及允许的 Session 重绑时，与 session_id 同事务更新。
2. 复用 Chat.status 作为展示状态投影；执行权仍由 Session.active_turn_id、generation 和 Turn.state 决定，不能用展示字段授予执行权。
3. 本批复用原有 `metadata_text`：当前实现原本只保存 summary 相同九键，增加 `display_metadata_text` 会完全重复内容。因此暂不新增该列；合法转义后的九键序列化预算调整为 40 KiB。完整无正文扩展属性入 SQL 仍需逐键分类，届时再评估是否需要单独显示投影，不能将本批误当作全量 Chat meta 已入 SQL。

状态投影只更新本次 active Turn 所属 Chat，不向同 Session 全部 Chat 广播。reserved/admitted/running/settling 映射 running，stopping 映射 stopping，无 active 关联或终态映射 idle，保持原 CASE 语义。准入、Stop、结束/拒绝、恢复/结算等所有状态 writer 同事务维护，沿用 Session→Chat→Turn 锁序；不因重命名覆盖执行展示状态。

新增的 reserve/Stop/settling 等状态投影不顺便更新排序用 updated_at；但终态提交和历史安装本来就更新 updated_at，这些既有副作用必须保留，不能一概删去。外部 Session ID、轻量属性、状态均在导入完整验证中逐项比对权威来源；原接口状态和排序语义不变。客户端不能直接修改这些服务端投影。

这是读多、写少摘要路径的明确取舍：多维护少量列，消除每次摘要读取对 Session/Turn 的关联。若无法覆盖所有状态 writer，先保留原两次主键关联，不允许异步投影长期显示错误。零关联不是牺牲正确性的硬指标。

不把整个 Session/Turn 内容复制进 Chat，不在每次续租更新展示列，不复制共享 context usage 到全部 Chat，不把 Cron 回执塞进通用 meta。

## 6. 按入口核算查询与写入

| 入口 | 调整后的内容关联 | 约束 |
| --- | --- | --- |
| 用户摘要列表 | 只读 Chat 的窄列；无需内容表、Session/Turn 关联 | 一条分页业务 SELECT，不读 NAS、不 pin；不宣称连接池维护也无 SQL |
| Chat 标题/普通属性 | 直接更新 Chat，必要身份/锁检查仍保留 | 不读写 NAS；不追求每个写操作都只有一条 SQL |
| 新内容历史页 | 沿用已有消息、publication 批量校验及 pins | 不额外查询 objects/views/parts |
| 旧 extent 历史页 | 业务行后，按唯一 source ID 批量查 direct 窄列 | 不逐消息点查 source，不加载无关 manifest |
| 旧组合历史/恢复 | 批取真正需要的 composite 描述 | 一请求同 source 解析一次，只恢复所需 Turn/epoch |
| 混合内容 | 固定两个解析分支，新 bundle 保留 pins | 旧文件不写持久 reader pin，但仍有 IO drain/授权/版本检查 |
| 完整备份/校验 | 按 scope 有界扫描 sources，解析清单 | 冷路径允许更多读取，不用热路径表拆分换取维护查询方便 |

sources 的 direct 窄列查询排除 manifest；复合描述单独按唯一 ID 分批读取，同时受行数和实际返回字节预算限制。应用层批取避免 N+1 仍属于关联成本，不能把“不写 JOIN”当作“没有额外查询”。

新 source 行数随旧文件/恢复单元增长，不随每次新消息保存增长；业务 Message occurrence 行仍然需要，不能为减少表而退回整份 JSON 扫描。内联 manifest 去掉成员行与索引，代价是完整描述的解析/传输和少量不可变信息重复，需用真实源规模量化。

## 7. 生命周期、权限与备份

legacy_sources 本轮不自动删除，旧文件永不物理 GC。因此无需 SQL 组件反向查询或 source 依赖保留表，也不让 composite 引用 managed bundle。新内容继续现有 publication dependencies、GC 状态与锁序。

backup 的简单契约是：备份目标 scope 的全部 legacy_sources、所有声明旧文件及可信根绑定，包含 legacy_share；文件去重在已授权的备份进程内按 root/path 实施，不通过跨 scope 授权共享。描述引用的同一文件若出现不一致指纹应拒绝，不能任选一个。

此取舍可能保留/备份一些已经退出当前在线视图的旧文件，但符合“原 NAS 不变”的边界，并省去在线依赖图。DB 快照不会冻结 NAS，旧根必须在切换后继续停写。自研 bundle-only backup/restore 未升级前明确不支持混合模式，平台一致恢复仍需包含来源表与旧文件。

source 的 root/path 是描述，不是权限；读取先验证实体、完整 scope 和可信映射。不因为减少 JOIN 而跳过旧分享隔离、完整身份、Message occurrence、CAS、fence 或 pin/GC 校验。

## 8. 下一步与验证

1. 用真实大样本验证单表 descriptor 的容量、必要文件与 pending/committed 纯读判定；不能只用小 fixture 冻结 DDL。
2. 冻结 sources 表、各业务 locator 的最小列及 Chat 三项投影，不创建三张 objects/views/parts；同步调整 schema readiness、导入、verify/finalize。
3. 为所有 Chat 状态 writer 增加同事务投影及回滚验证，确保状态正确、最近更新排序不变；若覆盖不足保持权威关联查询。
4. 验证旧历史打开→新问答→混合分页→重启→reset/现有 compact，内容不复活、旧文件不变、分享可独立于 Chat 存活。
5. 对比三表与一表的记录数、业务 SELECT 次数、扫描/返回字节、写入条数、NAS open/read/write、冷读耗时与 RSS；单表减少关联不自动保证整体更快。
6. 实际库六表缺 service-unit、分享未扩展、两控制表缺失的问题仍需最终 DBA SQL 一次性解决。本轮不重新连接或修改数据库，也不提前提交可执行 ALTER。

本草案是本次“减少关联、允许反规范化”要求下的推荐方向。复杂恢复不能仅靠改表完成，格式适配、真实样本、边界测试和旧接口兼容仍是实施前提。
