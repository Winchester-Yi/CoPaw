# 单来源表方案：SQL 契约与实施拆解

日期：2026-11-06，更新：2026-11-09。状态：B 包 Chat 纯 SQL 与摘要投影已进入实现；其余来源定位与 SQL-only 导入仍为草案。未执行数据库 DDL，未完成新版真实数据库验收。基于[反规范化方案](2026-11-06-conversation-denormalized-schema-design.md)，字段收敛及验证范围见[首批实现记录](../reports/2026-11-09-conversation-sql-metadata-implementation.md)。

## 1. 冻结方向与尚未放行的事项

内容兼容只新增一张 `swe_conversation_legacy_sources`，不建 objects/views/parts。新 bundle 继续既有 Publication；普通属性纯 SQL；摘要采用 Chat 同事务小型投影。两张迁移控制表保留，不能作为永久内容描述。

[新来源表 SQL 草案](sql/2026-11-06-conversation-legacy-sources-draft.sql)已给出完整列、唯一键和本行 CHECK，但尚不是可投产迁移脚本：现库缺服务维度与分享扩展，旧格式大样本、应用预算和运行时适配仍未验收。此次不生成可直接针对现库执行的 ALTER 组合，不让 DBA 先部署半套结构。

## 2. 减少字段与索引的原则

- 单表来源唯一键采用 `(service,scope_kind,tenant,source,agent,legacy_key)`。legacy_key 在首次票据分配后固定；按已有作业映射重试，不用文件 hash 作为领域身份。
- scope 备份直接按此唯一键的 legacy_key 分页，不另建 scope/id 索引；热读取按主键集合批取并校验归属。
- sources 不删除、旧文件不物理 GC，因此不为每个 legacy_source_id 添加反向索引，也不增加 path/hash/组件索引。
- 新增 Turn recovery publication 是实际 managed 内容引用，需要纳入 GC 反向保护索引和查询；不能一概省略所有关联索引。
- Chat 两种常见列表沿用现有 recent/channel_recent 索引。没有执行计划证据，不批量添加 scope、Session、channel 各种排列或将 TEXT 放进覆盖索引。
- sources 的 session_id/chat_id 是核验已知归属的信息，不增加外键 JOIN；纯身份及 source 与业务 locator 一致性在安装事务/读时校验。

## 3. 各表最小列矩阵

下述均以完整 service-aware 契约为基线。现库六表缺 service_unit_key，不可把这里的内容列增量当作独立升级脚本。

| 表/对象 | 最小变化 | 明确保留的权威 |
| --- | --- | --- |
| messages | publication_id、byte_offset、byte_length 允许 NULL；新增 legacy_source_id BIGINT NULL、legacy_selector_text TEXT NULL | occurrence、顺序、层、source ID、Turn、可见性；archive_publication_id 仍是归档成员关系 |
| sessions | 新增 recovery_legacy_source_id、recovery_legacy_selector_text | 与已有 recovery_publication_id/offset/length 构成一组 head，按 state_revision 安装；context epoch/usage 仍专列 |
| turns | 新增 recovery_publication_id/offset/length、recovery_legacy_source_id/selector_text 共五列 | admission/terminal publication 保留操作事实；新五列只指当前完整 Turn 恢复素材，不暗示过去存在历史提交 |
| chats | 新增 external_session_key VARBINARY(1024) NOT NULL；本批复用有界九键 metadata_text 和 status，不新增重复显示列 | 专家专列、metadata/history/checkpoint revision、单一 active_checkpoint_publication_id |
| publications | 新增 payload_kind ASCII VARCHAR(16)，bundle/legacy_index；relative_path/format_key 允许 NULL；archive/checkpoint/event 各新增一组 legacy_source_id/selector_text | operation、SQL 范围/边界/epoch/watermark、CAS、状态及新 bundle 的路径/GC |
| shares | 原字段全部保留，补 service_unit_key 和 nullable publication/offset/length；新增 legacy_source_id/selector_text | 永久 token、创建者、原 snapshot key、计数和时间；不依赖 Chat 存活 |
| retentions | 不新增 legacy pin 成员；managed 保护契约继续 | scope guard、fence、新 bundle reader/backup/dependency |

所有 selector 均是应用严格校验的 UTF-8 JSON 文本，TEXT 不表示允许任意表达式或任意大小。只用于旧来源格式明确的选择规则，不进入 SQL 筛选和索引。

### 避免 checkpoint 的重复 head

Chat **不再新增 checkpoint_source_id**。已有 active_checkpoint_publication_id 唯一指向当前 checkpoint 的已提交索引行，可能是 bundle，也可能是 legacy_index；从该行 checkpoint 角色得到内容定位。

Chat epoch/revision、publication watermark/source_high 与来源结果必须一致。增加另一套 Chat source head 不能减少这里必要的版本校验，反而可能分叉。

### content metadata 的处理

历史无正文扩展属性进入 metadata_text，不为它们新增 legacy 内容列。现有 content_meta bundle locator 保留已存在 DB 格式的兼容。

场景/提示词 snapshot 等真实内容必须先分类：若旧接口需要它们而本次仍要求 NAS 承载，应使用旧 registry source+selector 的显式 content_meta 分支，届时仅补该角色所需两列，不能把它们塞进 legacy_sources 描述。这个字段归属是正式 DDL 放行条件，不能通过删除旧响应字段或将正文改名为 meta 跳过。

## 4. locator 的 NULL 契约

| 状态 | pub | legacy source | offset/length | selector |
| --- | --- | --- | --- | --- |
| 未设置 | NULL | NULL | 均 NULL | NULL |
| bundle extent | 非 NULL | NULL | 均非 NULL | NULL |
| legacy extent | NULL | 非 NULL | 均非 NULL | NULL |
| legacy value | NULL | 非 NULL | 均 NULL | 受限选择条件；完整组合结果可 NULL |

Message/Share 正文禁止未设置；尚未保存内容的 Session/Turn 可以未设置。禁止半个 extent、pub/source 同时存在、无 source 的 selector、无目标偏移。合法空结果不等于无 locator，零长度文件也不自动代表合法消息 JSON。

publication 的 archive/checkpoint/event 角色不重复保存自身 publication ID：

- bundle 行：operation 生成的 path/format 非空；legacy 字段全空；存在角色时 offset/length 成对非空。
- legacy_index 行：path/format/整包长度/durable_at 全空；有内容角色则必须给出 source，按 legacy extent/value 规则填写该角色字段。没有角色则整组内容定位为空，相应范围/版本字段必须符合领域存在性规则。
- legacy_index 代表这次导入真实完成的 SQL 索引提交，安装事务内直接形成 committed；不经过 NAS reserve/write/confirm，不假装完成过文件 fsync。
- legacy_index 保持逻辑 live，不进入 deleting/reclaimed；GC 候选、直接 ID mark/reclaim/retry、StopProof 与 unlink 全部显式拒绝，不能只在候选查询过滤。

NULL path 可让现有服务内路径唯一约束自然不参与纯 SQL 索引行，不伪造路径。operation、archive boundary、checkpoint revision 唯一键继续保留。

## 5. manifest 的成本与固定解析边界

单文件来源用窄列，查询不 SELECT manifest。组合来源清单包含全部必要旧文件/角色/真实范围/指纹和版本证据；只能指该可信旧根内的文件，不引用 managed bundle、其他 source 或迁移日志。

本地序列化实验使用 Turn ledger 风格路径及每文件角色/长度/sha256/range 元数据，compact JSON 结果：

| 合成组件数量 | 描述字节数 |
| --- | --- |
| 64 | 17,744 |
| 4,096 | 1,137,672 |
| 16,385 | 4,560,401 |

这是合成描述大小，不是业务样本、数据库或 NAS 性能测试。它证明内联清单会增长，不能将原 1 MiB CommitPlan 上限机械套用到所有 source，也不能因为 MEDIUMTEXT 能容纳就一次查询返回多份大清单。

按独立 Turn record、archive batch、checkpoint epoch 等真实恢复单元划分来源。Session 读取只恢复业务所需的内容，冷 Turn 点查独立来源；不为每条消息 materialize 整份 Session。需要完整恢复时再按业务关系有界读取全部必要 Turn，不建立隐式 source 递归图。

应用预算分别包含：manifest 单行序列化字节、SQL 请求实际 packet、响应累计描述字节、NAS 输入字节及解码输出容量。source 窄列先取可判定信息，大 manifest 按实际字节单独读取；异常不可用 NULL/0 伪装空结果。完整大小预算及不可拆大来源需要真实样本确认后再冻结，不承诺合成 16,385 覆盖所有真实场景。

## 6. 读写入口的精确行为

### 单表摘要 SQL

```sql
SELECT external_chat_key, external_session_key, user_key, channel_key,
       title, created_at, updated_at, status, metadata_text,
       expert_id, expert_binding_revision
FROM swe_conversation_chats
WHERE service_unit_key = %s AND tenant_key = %s
  AND source_key = %s AND agent_key = %s
  AND user_key = %s AND deleted_at IS NULL
  AND (updated_at < %s OR (updated_at = %s AND external_chat_key < %s))
ORDER BY updated_at DESC, external_chat_key DESC
LIMIT %s;
```

首屏省略 cursor 条件，按需追加 channel/external_session_key 过滤。保留旧 v2 cursor 和 limit+1，不新增 COUNT。无 user 或按 external_session_key 的排序路径仍需实际 EXPLAIN；减少关联不自动意味着扫描已优化。

本批确认 metadata_text 原本只允许 display_metadata() 的九键，因此直接复用，暂不创建 display_metadata_text。六个字符串各原始 UTF-8 ≤1024 字节；compact JSON 使用 ensure_ascii=False、拒绝 NaN/重复键，序列化上限为 40 KiB：控制字符转义会放大字节数。未知或内容型字段仍按既有协议放 NAS，旧完整 metadata 响应保留。后续扩大 SQL 扩展属性前，必须先完成逐键分类、独立预算和显示投影隔离，不能直接解除现有九键校验。

### 状态投影 writer

| 事务入口 | Chat.status | 更新时间规则 |
| --- | --- | --- |
| 新建/导入/Session 重绑 | 从最终有效 SQL 关联推导；外部 Session ID 从受锁行复制 | 保留原创建/重绑规则 |
| reserve active Turn | running | 不额外刷新 updated_at |
| admission/start | running | 保留该入口原有历史安装行为，不为重复投影再写一次 |
| Stop | stopping | 新增投影更新不刷新排序时间 |
| reconcile 到 settling | running；若仍 stopping 则保持 stopping | 不额外刷新排序时间 |
| fence/lease 超时/renew | 不因 owner/generation 改变而置 idle | 不增加投影写入/更新时间 |
| reject 并清 active | idle | 不额外增加原来不存在的更新时间副作用 |
| terminal 成功 | idle | **保留原有 updated_at 更新** |
| 消息/历史安装 | 按 active Turn；不擅自改变状态 | **保留原有历史 updated_at 更新** |
| rename/普通 meta | 不接受陈旧 spec.status 覆盖 | 保留原 metadata CAS 和排序行为 |

权威字段与投影同事务，沿用 Session→Chat→Turn 锁序。不增加异步同步器或 projection_revision；纯状态更新不推进 metadata_revision。只更新 active Turn 所属 Chat，不广播同 Session 全部 Chat。

全部 writer 和回滚/未知提交验收通过后，summary 才切到单表。在此之前继续使用原 CASE 主键关联，不能先启用单表后靠修复任务补状态。

2026-11-09：实现已补齐上述在线状态 writer 和同事务回滚单测，summary 改用单表。readiness 要求新版 external_session_key 列；已有表不能仅添加空默认列后放行，需停写回填 Session ID 及状态并验证后再切换。新 schema 的导入安装复制 Session ID，完整 verify 校验投影。真实 MySQL 状态矩阵测试已补充，尚待实际执行；当前代码不是现有生产 schema 的即刻升级包。

### 历史、checkpoint 与 Turn

- 先获取业务行，将 bundle 与 legacy 来源分组；bundle 子集保留 reader pins，legacy 只读子集省去持久 pins，所有实际 NAS IO 仍 drain。
- checkpoint event 查询包含 bundle 和 legacy_index，fixed high/epoch/revision 继续生效；不能把批次查询也过滤成 bundle-only。
- archive JSONL 和旧 journal framing 必须经固定解析器归一化成现有 list/dict；不仅是换路径再 json.loads。
- legacy value 无实际输出 extent，不能用 NULL checkpoint_length 与预算比较，也不能把它按零处理。按解码实际输入/输出和请求累计预算结算。
- 归档边界元数据与消息正文 locator 分离，归档成员保持 SQL 边界关系，不以边界内容替代每条 occurrence。
- Turn 按有效恢复 locator 和真实准入状态判断可读取，不能过滤掉没有 admission/terminal publication 的旧 Turn。各格式 adapter 返回完整 TurnState，不将整个 Session wrapper 当成 TurnState。
- 新 projection 冻结 occurrence/Turn 的身份及完整 locator。legacy 来源不进入 managed publication dependency 集合，但仍验证 immutable source、scope、selector 和当前业务版本；pub 为 NULL 不能等同于忽略依赖内容。

## 7. 实施分包与验证

| 包 | 文件范围 | 验证结果要求 |
| --- | --- | --- |
| A：字段/预算及样本 | 设计、source SQL、旧格式样本对照 | 完整 metadata/内容型字段归属、不可拆来源预算、全部旧格式得到明确策略；没有正文进入 descriptor |
| B：纯 SQL Chat 与投影 | chat_mutations/repository、execution、message_installs、publications、migration/importer | NAS methods 全拒绝时普通属性成功；draft/专家/CAS/删除不变；所有状态转换与原 CASE 相等且时间排序兼容 |
| C：来源与 resolver | 新 source/parser 模块、Session/History/Archive/checkpoint/share、projection | 原 NAS 不变、无全目录反复复制；旧 Turn/多 epoch/混合引用/缺失与篡改拒绝通过 |
| D：SQL-only 导入 | DML facade、journal/install_records、verify、CLI、authority | 无安装计划 bundle；同批写业务和 committed 日志；首中末失败回滚，resume 幂等，最终完整 verify/finalize |
| E：GC/备份/发布 | retention/GC、平台备份清单、schema readiness、DBA SQL、runbook | legacy_index 直接 GC 拒绝；新 bundle 保护不降级；scope 全量 sources/旧文件与新 bundle 闭包可恢复；真实库/真实 NAS/实例验收 |

A 的数据预算/内容分类未闭环前不冻结最终部署 SQL；B 可独立开展但不将单表读提前上线。每包保留回归与成本证据，不将合成大小或 SQL 条数当延迟验收。

现库上次只读确认六表为空、分享三条，但这不是本次停写快照。最终 DBA 操作前重新核对；不清理原分享，不让旧 writer 与新 authority 混跑，不自动修改 schema。自研恢复的八项缺陷仍单列，CLI facade 缺 acquire 属于核心链路必须修。
