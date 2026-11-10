# NAS 会话元数据迁移：目标对齐复核

日期：2026-11-06。工作树：`.worktrees/conversation-mysql-nas`，源码基准：`49a4222c8`。
本轮由主代理与三个子代理审查当前源码，仅记录结论；未修改运行时代码、连接数据库或操作业务 NAS。已有未提交修改不属于本报告的实现成果。

后续[调整方案与分期计划](../specs/2026-11-06-conversation-metadata-only-adjustment-design.md)补充了复杂格式组合定位、安装计划 SQL 化及在线投影漏接。修正本报告的改名表述：专用 `update_title` 已为纯 SQL，完整 Chat 创建/更新仍依赖 `_publish_metadata`；不能将所有标题更新归为 NAS 写入。

## 1. 审查目标

用户重新明确：原 NAS 保持不变，工具将会话元数据同步到预建表；切换后新增会话元数据直接写 DB，NAS 保存内容。

本报告按“历史内容不复制、不重打包；工具只读旧 NAS、写入 DB”理解第一项。若只是要求不覆盖旧文件、允许另存整套正文，当前实现与目标的差距较小；这两种含义必须区分。

保留旧文件意味着其中原有的元数据副本仍然存在，但不再是在线权威，也不接收更新。不应为了消除副本而重写旧文件。“NAS 只保存内容”主要约束新增数据的职责分工。

沿用既定约束：保留 schema/索引/存储格式、兼容旧 API、非必要减少 NAS 交互、不加入列表并发稳定快照；Compact 容量、摘要生成和组装优化继续延期。如果旧格式定位无法在现有表契约中表达，先报告设计冲突，不能暗中改表或以重新打包冒充纯同步。

## 2. 总结论

**SQL 单权威运行时的主体已经实现，但历史迁移实际采用“保留旧源 + 生成整套新 bundle”，并非“仅同步元数据、继续原位使用旧内容”。**

| 目标 | 当前实现 | 差距 |
| --- | --- | --- |
| 预建表，工具不建表 | v3 用 DML facade，校验六张会话表、两张迁移控制表及原分享表扩展字段 | 基本符合，尚需最小权限验收 |
| 旧文件不覆盖、不删除 | 输入只读，原 workspace 绑定保留 | 字节/位置保留；直接输入模式还强制去写权限 |
| 只同步元数据 | 旧原件另存为 artifact，业务正文又转成 bundle | 主要设计差距 |
| 新增核心元数据直写 DB | Session、Chat、Message、Turn、边界、定位、版本与分享索引已经接 SQL | 主体符合 |
| 元数据变更非必要不访问 NAS | 白名单外 Chat meta 放 NAS，完整创建/更新即使 content 为空也发布 bundle，恢复状态含控制字段 | 尚未闭环；专用 update_title 已为纯 SQL |
| 完整迁移后能切换 | 非空 v3 verify 缺 acquire，finalize 再走相同校验 | 已知主流程阻断 |

不建议推倒 SQL runtime、身份隔离、publication、checkpoint 和批量 SQL 工作。需要重新设计的是历史内容接入、纯元数据写入边界与相应操作流程。

## 3. 当前源码证据

### 历史导入会新增 NAS 内容

`migration/importer.py:389` 在导入业务对象前调用 `_retain_source_artifacts`（`:426`），读取旧文件原字节并发布为 `legacy_artifact` bundle。随后 `_session`（`:598`）、`_archive`（`:879`）、`_shares`（`:1044`）继续转换业务内容。公共 `_publish`（`:313`）执行 reserve、写 bundle、验证和 commit。

因此会同时出现原件副本及新正文副本，有额外 NAS 读取、写入和空间成本；没有现场规模测量，不能估计具体容量倍数。原件保存阶段还有单文件 8 MiB 限制（`bundle.py:32`、导入器 `:457` 附近），大旧文件可能在业务导入前被拒绝，不能因 checkpoint 大条数测试通过便认为任意历史文件可导入。

### 新运行时不会直接读旧 JSON

MySQL Session 只走 SQL locator 和 bundle，没有旧 reader/fallback（`session_adapter.py:168`、`:251`）。`BundleStore._parts`（`bundle.py:199`）只接受生成的 `bundles/<prefix>/<operation>.bundle`，History 同样按 extent 读 bundle。

仅删除导入器的 `write_bundle` 或原件保存步骤，会得到当前运行时不能读取的数据。纯同步必须同时支持旧内容定位、读取、新旧历史组装及后续生命周期。

### 新增写入的主要接线已完成

HTTP 生命周期、Workspace 工厂、Runner、Chat/History、Tracing、分享及 Cron 的会话路径已接 SQL storage。Runner 的 SQL 分支在创建旧 SafeJSONSession 前返回，正常路径不双写旧 sessions JSON。

SQL 管身份/归属、列表字段、消息顺序和定位、Turn 控制状态、checkpoint head、版本/代次及分享索引。NAS 存消息、归档、摘要/checkpoint record、工具输入输出及恢复内容。这些内容适合留 NAS；bundle 头部和偏移索引等自描述结构不应被误判为业务元数据迁移失败。

数据库故障时拒绝服务，不回退旧文件写入。开关仍默认 legacy，正式切换必须显式配置 mysql 与匹配的 scope/authority。

### 不必要的元数据 NAS 依赖仍存在

`chat_mutations.py:11` 仅定义九个轻量 meta 字段，`split_metadata`（`:46`）将未知键、类型不匹配值及过长字符串留 NAS。`chat_repository._publish_metadata`（`:669`）总是生成 `content_meta` block，即使是 `{}`。新建空 Chat、走完整更新的属性变更仍会写 NAS；专用 `update_title`（`:571`）已有纯 SQL 路径。

summary 新接口已经纯 SQL；旧完整接口为保持完整 metadata 会 hydrate NAS。需要调整权威字段及写入路径，不能直接删旧接口字段或授权检查。

`session_adapter._save_state`（`:327`）仍发布恢复状态。投影会提取已知消息引用，不能说每次重复写全部历史；但其他 root 状态仍在 NAS，包括 Cron 的 `output_delivery_state/output_delivery_completed` 等控制字段。应区分可查询元数据、控制状态与不透明恢复内容，不能把整个 root 都称正文，也不宜全部搬进 DB。

## 4. 当前操作流程及限制

当前 v3：DBA 预建表 → 停旧 writer → 配置冻结输入及独立新 bundle 根 → 导入原件和转换正文 → verify → DBA 收紧分享字段 → finalize/ready → 安装 runtime 配置并启动新版。

当前流程说明见[v3 迁移运行手册](../plans/2026-11-02-conversation-migration-and-launch-runbook.md)。旧 staging/原子 RENAME/七表 install 流程已被替代，v2 import/install 写入口已禁用；不能混用旧操作指南。本轮目标调整后，还需统一更新正式手册，以下目标流程不是现成命令清单。

具体限制：

- 需要六张会话表、两张迁移控制表，并扩展已有分享表；本模块不执行 DDL。
- 首次会话目标表必须为空；同一应用库的服务应一次列全，避免分享归属及 ID 分配冲突。
- 输入根与新 NAS 根不能互相包含。直接输入检查所有文件和目录无写权限，通常需要临时 chmod 或提供冻结视图；仅停止旧服务不足以通过检查。
- 支持同一作业断点恢复，ready 后不能继续补导其他服务。当前不是持续增量同步工具。
- finalize 输出 authority/scope 配置，启动核验 ready、服务 ID、数据库身份及路径绑定，不依赖 restore_backup。
- 新旧 writer 不能混跑；已有 DB 新写入后，保留旧文件也不等于能够无损退回旧版本。

## 5. 建议目标流程

1. **预建表与预检**：DBA 提前建表；工具检查结构、权限、scope 与已有数据，不自动建 staging 或改表。
2. **停旧写入**：覆盖旧实例、Cron 及会更新分享状态的入口，建立可验证的停写边界，记录源清单/指纹。工具不改旧文件内容、路径或权限；只读视图/挂载方案不能继续机械要求 chmod 原树。
3. **只同步元数据**：读取旧内容提取身份、顺序、边界和定位，分批幂等写 DB，正文不复制、不重打包。报告/断点放专用控制位置。
4. **完整验证**：正式读取器通过 DB 定位直接读旧内容，覆盖历史、恢复、归档、checkpoint、分享及隔离；对比前后源指纹。
5. **切换新版**：DB 成为元数据唯一权威；历史文件继续只读，新内容写新内容文件，API 组合新旧内容；mysql 失败不回退旧 writer。
6. **业务与重启验收**：验证新会话、续写旧会话、列表/改名、Stop、Cron、分享、重启与实例身份，再扩容。

这是推荐目标流程，**尚非当前可执行 runbook**。无需额外引入在线双写、CDC 或长期双向同步；本轮按一次停写切换设计。

## 6. 核心待办及依赖顺序

| 顺序 | 工作包 | 交付与退出条件 |
| --- | --- | --- |
| 1 | 字段与存储权威清单 | 逐项确认 Chat meta、Session/Turn 控制字段、投递回执与恢复 payload；优先利用现有列/metadata_text，明确大小与兼容策略，未知 meta 不默认等同正文 |
| 2 | 历史只读定位契约 | 覆盖 sessions、registry、ledger、archive/checkpoint/share；核验现有 path/format/offset 能否准确表达。不足则报告 schema 契约冲突；格式分派不能成为数据库故障 fallback |
| 3 | 纯元数据同步工具 | 取消该模式的原件发布和正文重打包依赖；保留预检、批次、断点、幂等、错误定位与完整验证。大文件采用有界处理，不受无关的原件保存限制阻断 |
| 4 | 新旧内容混合运行 | 旧历史读取、新 append、reset/compact 边界、archive/share、重启均按 SQL 索引组装；旧根永不交给物理 GC 删除，新内容维持 publication 生命周期 |
| 5 | 纯 DB 元数据写入 | 空 Chat、改名、状态/轻量属性变更不写 NAS；内容未变保留原定位；保持 revision、身份约束与原子性，不伪造空 bundle |
| 6 | 修复 v3 CLI 阻断 | DML facade 提供受限 acquire/session 与真实 packet 能力，同连接/事务不丢失，不能透传裸 session 绕过 DML/只读策略；执行非空 import → verify → finalize → 启动 |
| 7 | 按新流程补验收/手册 | 预建表+DML 最小权限、原 NAS 字节/路径/权限不变、重复运行/断点、混合内容重启、既有 workspace/secret 启动、真实 NAS/跨实例、旧 API 兼容 |

第 2 项先做一个真实旧 Session 恢复和续写的最小可行验证，再扩展工具。schema 存在 format/path 字段，不代表现有 bundle 校验、GC、验证和恢复协议支持旧文件，不能承诺“只改导入器即可”。

另需补 Cron 兼容验收：旧调用方在 `session.execution` 内检查回执、保存 uncertain、发送外部消息、保存完成；新 `DatabaseSessionExecution` 是快照加 CAS，没有旧文件锁。方法名相同不代表互斥语义等价，也不能仅由审查断言已发生重复投递。应验证重入、重启及发送后提交失败；CAS 不能撤销外部发送。列表不追求并发快照的决定，不自动取消业务投递兼容要求。

## 7. 真实验收结果与范围重新归类

此前去重结果 **335 passed、9 failed、1 skipped**，见[真实 MySQL 验收](2026-11-06-conversation-real-mysql-acceptance.md)。本轮未重跑，不改变该事实。

| 结果 | 与核心诉求的关系 | 处理 |
| --- | --- | --- |
| 1 项 v3 CLI verify：DMLDatabase 缺 acquire | verify/finalize 必经，不能完成正常切换 | 必修 |
| 8 项 restore：恢复事务与 reader 清理跨任务冲突 | 自研灾备入口；import/verify/finalize/启动均未调用 restore_backup | 独立灾备缺陷，不再统称八个核心迁移阻断 |
| 1 项启动跳过 | 缺既有 workspace/secret 样本 | 补真实启动验收 |

现有 runbook 把恢复演练列为发布门槛，属于此前运维设计，不是程序依赖。可使用平台既有一致备份/恢复保护发布，不要求先完成项目自研灾备系统。但 restore 缺陷仍存在，不能将其宣传为可用。

checkpoint 的实际 4095/4096/4097/8193/16385、共享/独立 active、append/reset、取消/GC 共 16 项已在真实 MySQL 配合本地临时正文通过，不重复列为未实施；这不代表业务 NAS、多 Pod、最小权限或原 NAS 不变已验收。

## 8. 可独立延期的工作

- Compact 容量/终态、摘要生成和组装：按用户决定延期，问题未消失。
- 自研备份恢复修复、自动备份、数据库身份重绑定及旧版导出：独立灾备/回退工作。
- C1 同 bundle 批读、C2 NAS 背压、C3 有界缓存、D 快照复制/编解码：存储路径稳定后按成本推进，不能代替目标符合度修复。
- Subagent 独立 per-run JSON、Tracing trace store、Cron 任务定义属于独立子系统，不自动纳入全部数据库化；会话关联及投递兼容仍要验收。

继续保留 SQL 单权威、scope 隔离、代次/CAS、新内容耐久后发布索引、未知提交对账及 pin/GC。元数据应有纯 SQL 路径，涉及新内容的提交仍需处理 SQL 与 NAS 无共享事务的问题。

本轮应先收敛“历史只读引用 + 纯元数据同步 + 新增元数据纯 DB”，再修主流程并验收。仅修复九项失败不能宣告完全符合用户本轮目标。
