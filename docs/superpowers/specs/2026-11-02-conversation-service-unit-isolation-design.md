# 共享 MySQL、独立 NAS 的会话服务单元隔离设计

- 日期：2026-11-02；状态：设计及目标 SQL 已同步，运行时服务隔离待实现；未执行生产 DDL。
- 工作树：`.worktrees/conversation-mysql-nas`；核对基线：`597b2ba9f`。
- 补充：[原存储设计](2026-10-29-db-nas-conversation-storage-design.md)、[迁移方案](2026-10-30-nas-conversation-migration-plan.md)、[多 scope 恢复与分批 pin 修复](../reports/2026-11-01-conversation-r1-r2-fixes.md)。
- 本文涉及的服务隔离要求补充上述方案；在实现及验收前，原有通过记录不代表支持多服务隔离 NAS。

## 1. 已确认的部署事实与目标

用户确认：不同后端服务共享同一个会话 MySQL 数据库，各服务有独立隔离的 NAS；同一
`CMB_CAAS_SERVICEUNITID` 下的全部副本挂载同一份 NAS，该标识在重启及扩缩容后不变。
示例值：`LQ13.03@xxxx_prd_prd`。

用户进一步确认：前端—网关—后端服务链路已实现服务分流。本方案复用现有分流机制，
服务路由不是待设计事项；本次负责存储归属与隔离，以及其与既有分流结果的一致性。

定义“会话服务单元”：共享同一会话 NAS 的稳定后端服务分组。服务单元与 tenant、source、
agent、workspace、Pod、执行 owner 分别建模。仍使用原业务 workspace 与原 NAS，
不初始化新的业务 workspace，不把服务标识拼进租户 ID。

目标：数据库能够记录并过滤会话的服务归属；服务 A 不得读取、修改、pin、恢复或回收
服务 B 的会话及正文，即使两者业务身份和 NAS 相对路径完全相同。这个约束覆盖正常请求、
匿名分享、后台任务、故障结算和运维工具。环境配置仍是可信部署输入，不替代租户和用户授权。

## 2. 方案取舍

| 方案 | 收益 | 代价与结论 |
| --- | --- | --- |
| 只给 Session 增加归属字段 | 变更少，可用于统计 | 现有唯一键仍冲突，Publication/分享/GC 可走到错误 NAS，不满足隔离 |
| 服务单元进入完整存储 scope，并在七张表记录 | 直接对应已确认的服务与 NAS 关系，查询和维护均可明确隔离 | 需要同步调整查询、唯一键和运维协议；推荐 |
| 新建 NAS 注册表，再增加服务到存储域的映射 | 支持多个服务共享 NAS 或一个服务切换多个 NAS | 当前没有这类关系，新增表和生命周期暂不需要 |

推荐第二种。后续若服务标识与 NAS 不再保持稳定对应关系，必须重新设计存储域与迁移协议，
不能直接修改已有数据的服务字段来“接管”另一块 NAS。

## 3. 身份和配置契约

服务端及工具字段名使用 `service_unit_id`；SQL 列使用 `service_unit_key`，与现有
`tenant_key` 等精确字节身份列保持风格一致。

```text
WorkspaceScope = (service_unit_id, tenant_id, source_id, agent_id)
SessionIdentity = WorkspaceScope + (user_id, channel, external_session_id)
NAS locator = (service_unit_id, relative_path, offset, length)
legacy_share namespace = (service_unit_id, legacy_share, tenant_id, "", "")
```

`WorkspaceScope` 在概念上扩展为四维。实现时使用显式命名参数构造，逐处审计现有
三元组拆包、位置索引、序列化及比较；不让旧三维 scope 默认为当前服务。

MySQL 模式启动时读取一次 `CMB_CAAS_SERVICEUNITID`，验证后注入存储适配器，运行中
不跟随 `os.environ` 变化。支持 `.`、`@`、`_` 等示例字符；保留大小写及原始 UTF-8 字节，
不截断、大小写折叠、做哈希或按 `@` 拆分。不用它生成路径。

建议列宽 `VARBINARY(256) NOT NULL`，无默认值。缺失、空串、全空白、首尾空白、控制字符、
超过 256 个 UTF-8 字节均拒绝就绪；不自动 trim 或用 `default`/Pod 名回填。256 是本方案
的容量选择，实施前用现有全部服务值预检；超限则调整设计，不能裁剪生产值。

心跳已有 `service_heartbeat.get_service_unit()` 读取同一个环境变量，但它允许缺失；会话
MySQL 模式应增加自己的严格入口校验，不为了复用 helper 而改变整个心跳功能的兼容契约。
测试在构造器显式传值；离线维护使用绑定到源备份/manifest 的服务值，不从操作机器默认环境猜测。

部署清单还应把已核验的服务标识与原 NAS 根绑定。启动时要求清单服务值等于环境值；这个检查
能发现清单和环境配错，不能证明底层挂载内容正确。挂载关系仍需部署预检确认。

## 4. 数据库字段、唯一约束与索引

推荐七张会话相关表均增加同名 `service_unit_key`，便于每个直接入口及维护扫描显式隔离。
这是一个领域字段在多个实体上的受控冗余，不增加新表或 SQL JSON 类型。

| 表 | 归属来源及约束 |
| --- | --- |
| `swe_conversation_sessions` | 由可信完整 SessionIdentity 写入，创建后不可普通 patch |
| `swe_conversation_chats` | 与所属 Session 服务值一致；重绑 Session 仅允许同服务 |
| `swe_conversation_turns` | 与关联 Session、Chat 一致；历史关联保留原有语义 |
| `swe_conversation_messages` | 与 Session、正文 Publication、关联归档及 Turn 的服务值一致 |
| `swe_conversation_publications` | NAS 归属权威之一；workspace 与 legacy_share 都必须有服务值 |
| `swe_conversation_retentions` | 与目标 Publication 服务值一致；无目标的 guard/fence 也必须绑定服务 |
| `swe_chat_shares` | 与分享 Publication 服务值一致，孤立历史分享也必须有明确归属 |

不要求历史 Turn 的来源 Session 等于 Chat 当前 Session，保留现有同服务重绑行为；但
所有相关实体必须同服务。关联检查在数据库事务内进行，禁止根据外部传入的冗余字段直接写入。

唯一键调整：

- Session 完整身份、Chat 外部键的唯一约束在最前面增加 `service_unit_key`。
- runtime Turn key、Publication operation key、Publication relative_path、Retention
  `(retention_key, member_key)` 的唯一范围增加服务单元。它们不再假设跨隔离 NAS 全局唯一。
- GC guard 唯一范围变为 `(service_unit_key, scope_kind, tenant_key, source_key,
  agent_key, guard_marker)`，backup fence 使用同一完整范围。
- 已含全局内部父 ID 的唯一键，例如 `(session_id, question_msg_key)`、
  `(chat_id, boundary_order)`，无需机械增加服务列；父子同服务由事务校验。
- 七表内部整数主键继续在共享 schema 全局唯一，不改为复合主键，不允许各服务独立从 1
  分配导入 ID 后直接合并。
- 分享 token 保持现有全局唯一及原链接格式。迁移遇到同 token 多服务归属冲突必须报告并阻止
  切换；即使内容相同也不能自动丢弃某份归属。不把 token 静默改成服务内唯一。

服务内列表、身份检索、pending、租约扫描、GC 候选、Retention scope 查询以服务列为前导
维度；按全局主键点查仍需服务谓词及关联校验。只按全局父 ID 的已授权内部查询可以沿父关系
限定，但不能让未授权传入的整数 ID 成为跨服务访问入口。索引最终以实际 SQL 和 EXPLAIN 验证，
不为所有反向引用索引重复增加列。

原 Session 身份唯一键声明上限 2112 字节，新增 256 后为 2368 字节；原先的部署目标和
索引容量预检仍须执行。所有新复合索引按完整列宽重新核算，禁止前缀唯一索引或身份摘要。

### 4.1 已同步的目标 SQL 与执行顺序

1. [六表建表脚本](../../../scripts/sql/conversation_metadata_tables.sql)：六张新表均新增
   `service_unit_key VARBINARY(256) NOT NULL`，无默认服务；同步本节约定的唯一键以及
   Session lease/lookup、Chat 列表、Publication pending/GC、Retention scope 索引。
2. [分享表扩展脚本](../../../scripts/sql/conversation_share_publication_extension.sql)：
   保留八个原列、token 主键及计数，增加服务字段和 service+chat/creator 索引。由于复制来的
   旧记录尚待逐条确定归属，这一步的服务列允许 NULL；只在停写 staging 使用该过渡结构。
3. 迁移工具以经过核验的源归属回填分享服务及 locator；全量校验引用、服务身份、NAS 和业务语义。
4. [分享归属收紧脚本](../../../scripts/sql/conversation_share_service_unit_finalize.sql)：
   在任何 ALTER 前检查所有分享服务非 NULL/非空、Publication 存在且同服务、offset/length
   完整；失败则停止，成功才改成 `VARBINARY(256) NOT NULL` 且无默认值。脚本不执行自动回填。
5. 严格 schema readiness、安装器和运行时全部升级后，才允许安装最终七表并启动。

六表整数主键及父 ID 约束保持原结构，分享 token 保持原主键及排序规则。新脚本不添加
数据库外键、JSON、身份哈希或默认服务。建表脚本用于空 staging，不是已有生产表的升级脚本。

当前 `schema_contract.py` 仍对应三维运行时，不能现在仅把其字典改成新列后让就绪检查放行，
否则尚未隔离的查询可能被启用。它与适配器、导入/安装器必须在同一功能交付中同步升级；
当前目标 DDL 对现有运行时的拒绝是明确的兼容边界，不代表运行时接入已经完成。现有使用旧
运行时写入新建表的集成测试也需在该接入阶段更新，本文的 SQL 测试只验证数据库约束。

## 5. 运行时访问与 NAS 边界

运行时仅绑定一个服务及其原 NAS 根；一般请求不接受客户端切换 `service_unit_id`。
`WorkspaceConversationStorage`、Publication/Execution/History/Retention、分享全局适配器、
GC、故障恢复均显式接收同一个不可变服务上下文。适配器之间不能混用不同服务与 BundleStore。

每次读取流程：可信服务与业务授权 → 同服务 SQL locator → 同服务 reader pin →
从本服务 NAS 读取正文。定位失败、服务不匹配或缺少文件均返回现有对应错误，禁止退回其他
服务、旧文件协议或在本服务重新创建一份同 ID 会话。

写入、重试对账、commit、abort、恢复结算、pin 释放、依赖安装、GC 标记及物理回收的 SQL
都须带服务限定。服务值在幂等请求及 commit 关联校验中参与比较。任何跨服务 dependency、
recovery/checkpoint/body extent 引用必须在 NAS IO 或提交前拒绝。

原 NAS 相对路径 `bundles/<prefix>/<operation>.bundle` 不变，完整定位通过服务+相对路径完成。
不同服务可以存在同样的 operation/path；本次不搬动历史目录，不把示例服务字符串作为目录名。

历史 cursor 的签名命名空间增加服务，版本升级后旧 cursor 要求重新分页；跨服务 cursor 必须
失败。进程或共享缓存、迁移映射、stop/recovery 证据均包含服务上下文；SSE/Stop/审批入口
仍需原有用户及执行所有权校验。服务 ID 不能代替 Pod owner、generation、lease 或停止证据。

Cron、维护任务和外部回调必须到达目标服务单元，不能由一个仅挂载 A NAS 的进程遍历共享库后
操作 B。跨服务管理只能使用单独明确授权的管理入口，不能复用普通数据适配器放宽 scope。

## 6. 分享链接与请求路由

当前 `ConversationRuntime.shares` 不绑定 workspace；`read_snapshot()` 先按 token 取
Publication，再从固定本地 NAS 读取，因此必须单独加入服务约束。匿名分享也不能跳过它。
分享计数更新与 locator 读取都限定同服务；错误服务不得读取正文、创建 reader pin 或增加访问计数。

本方案保留全局 token。普通详情或分享请求落到错误服务时使用现有不泄露归属的未找到语义，
日志可记录内部服务不匹配。新增数据库字段本身不会让网关自动选中正确的 NAS。

前端—网关—后端服务分流已由用户确认实现，继续使用现有链路，无需新增路由组件或应用内
跨服务代理。后端以自身部署环境中的服务标识绑定存储，不允许客户端字段覆盖该标识。
ADR 0039 已要求 reconnect、Stop 和审批的会话亲和性；到达所属服务后继续沿用其内部副本
亲和性。验收复用现有分流，核对普通会话、历史分享、reconnect、Stop 和审批均到达所属服务，
且落点环境标识与数据库归属一致；错误落点仍须被存储隔离阻止访问。

## 7. 迁移、发布与历史回填

优先在本次 MySQL/NAS 首次正式切换前纳入该维度。当前会话七表安装方案是全表集安装，
多个服务分别执行安装会覆盖或排斥其他服务的数据，不能直接复用原单服务流程。

首次全量迁移使用一个共享数据库级迁移协调清单：

1. 盘点全部服务 ID、每个原 NAS 根、全部 workspace scope，以及分享来源。每个源位置显式
   带服务，不能仅凭 tenant、相同绝对路径、环境默认值或文件名推断归属。
2. 在统一连续停机边界停止所有涉及共享会话表的服务 writer、重试、分享计数和 GC，保留各 NAS
   只读源快照及对应证据。各服务独立 NAS 不代表七表可以分别切换。
3. 用同一个协调 manifest 为每类实体分配全局不冲突 ID；各 NAS 的导出可以分别进行，但
   全部 metadata 汇总到同一隔离 staging。源位置及分配键包含服务。
4. 旧分享表若已有全服务数据，仅凭 tenant/chat/snapshot 路径不能证明归属；要求逐条以可信
   服务清单和源快照解析。无法唯一确定的记录阻止切换，不批量填成执行迁移机器所在服务。
5. 按服务校验全部引用闭包、业务语义和 NAS 字节，再统一执行七表安装；不同服务把各自
   runtime manifest 绑定回各自原 NAS 与 workspace，验证后开放入口。

如果这七表已有生产数据，则必须单独准备停机升级：可先增加 nullable 列，但旧 writer 保持
停止；逐实体回填并校验，重建唯一键/索引，最后改为 NOT NULL 无默认值，再启动全量新版本。
无法确定归属不允许上线；不设计旧/新查询语义并行写入。服务 A/B 都连同库时，不能用
`UPDATE ... SET service_unit_key=<当前环境>` 全库回填。

严格 schema readiness、分享 schema 校验和安装器同步更新。运行时不执行自动 DDL；不因为
增加一列就跳过对七表定义及关联的整体校验。回退需要覆盖全部受影响服务、共享 metadata
及各自 NAS；首次启用新写入后的旧格式恢复仍须使用完整导出方案。

## 8. 备份、恢复与回收

备份文档、scope manifest、迁移 journal、安装与验证报告升级为显式带服务的版本；旧版
三维文档不自动绑定当前环境。需要转换时由操作方显式指定并核验源服务，转换后再校验。

日常备份只捕获指定服务的 workspace 与同服务 tenant 级 legacy_share。共享孤立分享去重
的 namespace 从 tenant 改为 `(service_unit_id, tenant)`；同租户不同服务不能互相比较或合并。
保留 R1 的同服务多 scope 聚合恢复，以及 R2 的分批 pin、单快照和完整清理语义。

恢复分清两种范围：

- 单服务备份可恢复到空的隔离 staging，做验证与导出；不能把它通过全表替换装到含其他服务
  数据的生产 schema。向已有共享生产库合并恢复不属于本次自动支持范围。
- 整库灾难恢复必须提供全部服务备份集合和显式的 `service_unit_id → 隔离 NAS 目标根`
  映射。先验证全局 ID、token、引用及服务集合，再按服务分发唯一 bundle，合并 metadata
  后在同一事务安装七表。校验遍历各服务各 scope；任一失败不发出成功报告。

不能直接把混合服务备份交给现有单个 BundleStore 的 `restore_backup`；实现前必须显式拒绝。
这不是放宽七表空目标要求，也不允许覆盖原业务 NAS。NAS 复制及提交后检查仍保留隔离失败
现场，SQL 安装原子性不宣称所有文件操作都可回滚。

GC 的候选、guard/fence、Publication 锁及 unlink 都限定当前服务。备份或维护 A 的保护不
阻塞 B，同业务 scope 的两个服务不得共用一把 guard。物理 GC 默认开关和停止证据要求不变。

## 9. 实施拆分与验收标准

| 步骤 | 改动范围 | 必须验证 |
| --- | --- | --- |
| 身份与 schema | `identity.py`、SQL、`schema_contract.py`、schema readiness、分享 store | 缺失/非法服务拒绝；精确字节及全部索引定义一致 |
| 运行时全链路 | `runtime.py`、`factory.py`、repositories、Publication/Execution/Retention、history/archive/share/recovery | 同业务身份跨服务互不命中；所有关联和幂等校验包含服务 |
| 运维协议 | migration、CLI、backup/backup_set、GC、清单和回退 | 全局 ID 分配；服务与 NAS 绑定；不覆盖其他服务 |
| 隔离验收 | 双服务、双临时目录、同一隔离 MySQL schema | 以下完整矩阵通过，现有回归通过 |

验收核心场景：

1. A/B 使用完全相同 tenant/source/agent/user/channel/session/chat 外部值，创建两套独立
   记录；列表、详情、Turn、修改、删除、恢复均只影响本服务。
2. A/B 放置相同 operation/path、不同内容；各自读到正确正文。向 A 提供 B 的内部实体 ID、
   Publication、cursor 或依赖时，在 pin/正文 IO/写入前失败。
3. 同服务两个副本共享 NAS，并发准入仍互斥；跨服务相同 Session 并发互不占用。重启、
   扩缩容不改变服务归属。
4. 错误服务访问分享不读正文、不 pin、不计数；正确服务历史 token 及快照语义不变。token
   归属冲突在迁移预检被拒绝。
5. A 的 GC/备份/恢复/故障结算不能触及 B；同服务多 scope 恢复保留共享分享去重，混合服务
   单根恢复失败；完整灾备按多个隔离根验证。
6. 服务环境缺失、manifest 不一致、未知归属、跨服务关联、全局导入 ID 冲突均阻止相关
   启动或切换，不能自动归入默认服务。
7. 现有长期 Session 的 4096/4097 Publication 边界、分批依赖、取消及失败清理测试保留，
   并增加跨服务集合输入；不能借本次修改重新引入单批 pin 问题。

本地模拟可用同一隔离 schema 与两个临时目录，不需要真实 Kubernetes/NAS；生产的环境变量、
真实挂载和路由对应关系仍须在上线环境验收。SQL 设计验证与后续运行时隔离验收分别记录，
不能将建表约束测试当成应用已完成跨服务隔离的证据。

## 10. 评审关注点

已经确认服务到 NAS 的稳定关系，以及现有前端—网关—后端分流机制。提交实现前需要评审
本稿推荐的完整隔离范围、七表冗余及全局分享 token 保留规则。路由仅做既有链路与存储归属
的一致性验收；当前草案不引入跨服务自动转发、在线服务归属变更或向非空生产库自动合并恢复。

## 11. 本次 SQL 设计验证

2026-11-02，使用项目虚拟环境，在随机生成的隔离 MySQL schema 中执行仓库原样 SQL；
未访问业务 NAS 或修改生产表。先运行新增跨服务身份测试，旧 DDL 因缺少
`service_unit_key` 失败；同步 SQL 后运行以下两个测试文件：

```text
tests/integrated/conversation_storage/test_schema.py
tests/integrated/conversation_storage/test_service_unit_schema.py
16 passed, 1 warning in 133.13s
```

覆盖六表完整定义和原有约束、跨服务相同 Session/Chat/operation/path/runtime key/Retention
共存、同服务防重、服务列无默认值、分享原字段/计数保留、未回填/空服务/服务不匹配/缺失
Publication/不完整 extent 的收紧拒绝，以及成功收紧后的 NOT NULL 和全局 token 防重。
脚本执行完成后生成的隔离 schema 已清理；结果记录为
`/private/tmp/conversation-service-unit-schema.xml`。唯一 warning 是原有 `record_property`
与 JUnit xunit2 输出不兼容，不是 SQL 执行失败。

这不是运行时服务隔离的验收，未据此宣称原有全部 MySQL 适配器集成测试通过。目标建表
脚本与旧三维运行时明确不兼容；后续接入必须同步 schema 契约、适配器和迁移工具。

## 12. 提交后独立检视

子代理 `review_service_unit_design_sql` 只读检视 `597b2ba9f..b10a83abd`，未发现可确认问题。
检视覆盖设计与六表 DDL、分享扩展/收紧 SQL 的一致性、全局主键和 token 保留、
ALTER 前拒绝路径、迁移/恢复边界及新增测试，并独立读取 XML 确认 16 项、零失败、零错误、
零跳过。检视未重新连接 MySQL 或运行数据库测试，未访问业务 NAS、未修改文件。
该结论仅针对本次设计和目标 SQL，不扩展为尚未实现的运行时隔离验收。

## 13. 运行时与离线工具接入

2026-11-02 后续实现把服务身份接入七表 schema 契约、运行时、Session/Chat/Turn/Message、
Publication、Retention、历史/cursor、分享、GC、备份恢复与结束 owner 的结算。MySQL
authority 启动只读取一次 `CMB_CAAS_SERVICEUNITID`，与 v2 runtime manifest 全量比对；
缺失环境变量、旧三维 scope 或跨服务映射均拒绝。W+ 工具沿用可信运行时绑定。

迁移采用 v2 source inventory/manifest 和精确的 `nas_roots` 映射，在共享 staging schema
中统一分配全局整数 ID。Turn/Message 明确写入不可变服务列，分享回填后执行收紧 SQL。
整组恢复仍将七表放在一个事务中安装，不同服务按显式映射复制到隔离目标 NAS。

独立运行时、迁移与维护检视发现的缺口均增加回归：发布前检查全部显式父行和 Message/
Publication 引用；离线正文/artifact/archive/epoch 读取前核验服务；备份关联保留原 Turn
Session；外服务备份 release 先拒绝再验证正文；分享 token 用目标排序规则在复制前检查。
同服务 Chat 当前 Session 重绑仍允许访问合法历史，长期 Session 分批 pin/依赖机制保留。

实现与测试命令、XML 结果和环境边界见
[实施验收记录](../reports/2026-11-02-conversation-service-unit-implementation.md)。
本地临时目录仅模拟独立 NAS；真实 Kubernetes 挂载、网关对应关系和生产停机边界仍需现场验收。
