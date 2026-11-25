# 会话元数据迁入 MySQL，正文沿用原 NAS 读写

2026-11-23 · 源码基准 `4ce0a493d` · 实施方案

## 1. 存储边界

上线后，MySQL 承接 `chats.json` 的会话目录职责。新旧会话正文都使用现有 `SafeJSONSession`、Turn Ledger 和 `ConversationArchiveStore`，从原 workspace 的 `sessions/`、`dialog/` 及关联资源目录读取，按现有文件协议继续写入。

本次迁移的输入是原 NAS 格式，现场尚未生成 bundle。停写和恢复写入由现场人员控制。迁移工具在人工停写后读取输入，负责元数据导入、校验和发布。

| 数据 | 权威与写入边界 |
| --- | --- |
| Chat UUID、标题、用户与渠道、创建和更新时间、目录顺序、删除标记 | MySQL Chat 行。保留外部 UUID 和既有列表、查找、分页规则。 |
| 服务单元、租户、来源、Agent 与逻辑 Session 关联 | MySQL。保留现有精确作用域及内部 Session ID，Chat 重绑时同事务校验目标归属与关联。 |
| 专家绑定、计划/目标模式、自动标题标记、任务关联和场景描述 | MySQL。专家绑定沿用专列和独立版本。已确认的业务 `meta` 保留原键、类型和值。包、凭据和大块正文继续由原有资源或状态文件承载。 |
| 消息、工具输入输出、摘要、Agent 恢复快照、Turn 内容和终态、归档、checkpoint 版本及水位 | NAS 原会话文件和原提交协议。运行态走原 Answer Turn 协调器，内容可见性与重启恢复由原会话文件决定。 |
| 招乎活动指针、Cron 回执和任务会话追加 | 继续经 `runner.session` 读写原 NAS Session。指针对应的 Chat 是否存在及属于当前用户和渠道，由 SQL 查验。 |
| 分享 | 沿用 `swe_chat_shares` 索引和现有 NAS JSON 快照路径、token、访问计数及已删除 Chat 的独立分享语义。 |

在现有 `legacy` 和 `mysql` 之外增加显式权威值，建议使用 `mysql-metadata`。每个部署的 migration job、来源发布凭据和线上配置都记录同一存储协议。现有 `mysql` 继续服务已使用 bundle 的环境。新模式按已发布来源准入，其 schema 检查覆盖启用的表、索引和业务契约。误配模式、作用域变化和 DB 不可用时沿用存储不可用语义。

此方案调整现有 ADR 对 Turn、历史和执行权威的职责定义。实施时补充一条决策记录，解释新模式的 NAS 锁和版本边界，以及与现有 bundle 模式的适用范围。 [ADR 0038](../../adr/0038-conversation-metadata-in-mysql-content-on-nas.md) · [ADR 0039](../../adr/0039-conversation-publication-uses-mysql-commit.md)

## 2. 改动顺序

### 第一步 · 确认字段和旧文件定位

- 从代表性的各来源 `chats.json` 做只读统计，记录字段名、类型、字节大小、出现次数及对应读写入口。优先覆盖任务、招乎、专家草稿、场景和 W+ SOP。产出按字段的迁移映射和超限清单，报告只放字段名、对象身份和统计值。保留显式空值、布尔值、扩展字段和旧排序。场景快照的 ID、版本、路径、冻结工具 schema 按结构化元数据校验。若某字段包含正文或凭据且目前只存在于目录文件，先为该字段确定可读回的原生存储位置，处理完再发布该来源。
- 将旧 Session 文件定位提取为纯计算，可由导入器和线上共同调用。默认仍按原 user/session 清洗和拼接规则。自定义 `session_paths`、`sessions_dir`、`dialog_dir` 必须由可信配置或发布映射绑定，验证原 reader、writer 和文件锁得到同一个真实位置。SQL 的 Session 唯一键包含 channel，旧文件名不含 channel。迁移时核验路径别名、清洗结果及跨渠道冲突，线上创建或重绑时也校验路径归属。保留同一个逻辑 Session 下多 Chat 共享文件的行为。
- 复用 `swe_conversation_chats` 和 `swe_conversation_sessions` 的身份、绑定、时间及分页索引。建议保留 `metadata_text` 作为摘要投影，为完整的非摘要业务元数据新增列，用同一事务更新两者，详情读回时还原 `ChatSpec.meta`。有独立物理文件归属需求时，为 Session 增加受作用域约束的路径绑定，键长度按实际样本和目标库验证。删除清理需要能从已提交的 SQL 墓碑找到待重试记录，优先用 Chat 行上的完成标记。最终 DDL 在本阶段通过真实样本、查询计划与在用 schema 核对后确定。

**阶段验收**。每个样本 Chat 的完整字段与详情响应可往返对照，摘要查询只读轻量列。同路径异身份有确定的归属或可定位的冲突报告。DDL 清单明确新部署与已有会话表的操作差异。 [ChatSpec](../../../src/swe/app/runner/models.py) · [Chat 字段拆分](../../../src/swe/app/conversation_storage/chat_mutations.py) · [Session 路径](../../../src/swe/app/runner/session.py) · [会话 DDL](../../../scripts/sql/conversation_metadata_tables.sql)

### 第二步 · 接入 SQL Chat 仓库和原文件运行链

| 入口 | 实施改动 |
| --- | --- |
| Workspace 和 W+ SOP | 给 `ChatManager` 装配独立的 SQL Chat 仓库，W+ SOP 使用同一个仓库解析入口。让 `workspace.conversation_storage` 继续表示现有 bundle 整套能力，使原 Runner、Console、历史、Tracing 和控制命令进入已存在的 NAS 分支。 |
| Chat 仓库 | 从现有 MySQL 实现复用作用域过滤、稳定分页、锁序、标题 CAS、专家绑定、草稿竞争和墓碑。提取关联校验与 SQL 写入，解除对 `PublicationStore`、prepared publication 和 SQL active Turn 的依赖。详情与列表保留旧运行态投影。 |
| NAS 执行 | 保留原问答准入、会话与归档文件锁、Session v1/v2/v3、checkpoint、停止与重启恢复。Console、渠道、任务和分享通过 SQL 确认 Chat 归属，再用原文件读写。Cron 选迁等直接读目录文件的旁路，按业务语义接入 SQL 或限定为明确的离线输入。已迁移工作区以 SQL 提供业务目录，`mysql-metadata` 模式的 bootstrap 跳过旧 `chats.json`。新登记工作区仍检查是否存在待迁移数据。 |
| 删除与竞争 | SQL 事务核验归属与预期版本并提交墓碑。提交后按原归档锁和 Chat 私有资源边界清理，成功后记录完成。通过可重跑的清理入口处理失败和进程中断。运行中删除沿用原业务约束并复测晚到写入，保护共享 Session 文件及独立分享。场景创建竞争只回收失败一方的私有资源。 |
| 分享与运维 | 复用现有分享索引和 JSON 快照路径，验证现有 token、权限和访问计数。文件维护入口按新模式纳入成套备份与恢复，保持原文件的校验和锁约束。 |

问答入口先完成 SQL 目录的授权与创建，再通过 NAS 持久化用户轮次并执行。回答终态跟随 NAS 提交。回答写入成功后发生目录更新失败时，目录操作按 Chat 身份与版本独立重试，原回答仍可从已提交的 Session 读回。出现 SQL 提交结果未知时，按稳定 Chat 身份或目录版本重新读取确认。SQL 行锁持有期间只做数据库操作，文件清理在提交后按原文件锁完成。

**阶段验收**。用单实例完成旧历史打开、续聊、重启再读、改名和删除。屏蔽 bundle 组件后上述路径仍可完成。目录摘要、改名、专家绑定操作无需访问正文文件。 [工作区工厂](../../../src/swe/app/workspace/service_factories.py) · [请求分派](../../../src/swe/app/runner/query_execution/admission.py) · [Chat 仓库](../../../src/swe/app/conversation_storage/chat_repository.py) · [删除与场景资源清理](../../../src/swe/app/runner/manager.py) · [W+ SOP](../../../src/swe/agents/tools/emit_wplus_sop_event.py)

### 第三步 · 加入纯 SQL 迁移和来源发布

1. 在现有 `conversation-storage prepare` 命令增加模式选择，建议为 `--mode mysql-metadata`。生成带明确协议版本的 manifest，绑定迁移 ID、目标 DB、服务单元、来源、可信 workspace、原 NAS 路径及冻结目录证据。复用 discovery、来源隔离和安全路径检查，原写入路径直接作为线上目标。报告和临时产物存放在审计目录。
2. 在 `migrate` 按 manifest 协议分派。使用现有 `ChatsFile` 解析器生成目录与 Session 关联，按稳定身份分批安装。业务行与批次 committed 标记共用一个 SQL 事务。重启先查该批是否提交，通过原迁移 ID 和批次键续跑。遇到未知目标行或输入变化时报告冲突，保留既有行。目标 schema 可以分别安装两张会话业务表和复用的迁移控制表。该分派的成功条件只依赖所用的表和原 NAS 就绪状态。
3. 校验 UUID、归属、目录顺序、字段值、绑定版本、Session 关联、路径归属和摘要可见性，使用线上 SQL 仓库读回。发布前抽取或按配置扩大原 NAS 读回范围，重点覆盖共享 Session、v3 ledger、归档、招乎和路径覆盖。离线读回用纯读接口或只读挂载，保持待切换 NAS 文件的字节、目录和权限可核对。
4. 以 SQL 事务发布来源，凭据绑定原 workspace、模式和校验结果。新模式下全局完成状态根据来源和自身 schema 判定，分享 JSON 路径沿用其自身检查。来源失败隔离、同 manifest 续跑和已发布来源跳过沿用当前语义。只保留必要的 journal 与元数据校验，导入过程无需创建 bundle、内容定位或 `import_plan` 文件。

**阶段验收**。同一 manifest 首次导入、提交响应丢失、进程中断及恢复得到同一批数据。输入变化和错误来源正确隔离。未发布来源不可被新模式访问。迁移前后原 NAS 会话内容树保持一致，应用启动后原路径可以继续写。 [准备命令](../../../src/swe/cli/conversation_storage_workflow_cmd.py) · [来源导入](../../../src/swe/cli/conversation_storage_tenants.py) · [迁移控制表](../../../scripts/sql/conversation_migration_control_tables.sql) · [来源发布](../../../src/swe/app/conversation_storage/migration/source_state.py)

### 第四步 · 上线、回退和环境验收

1. **上线。** 先做只读准备和 schema 核对。现场人工停写后，以同一数据边界备份 SQL 与 NAS，导入并发布来源。启动一个新模式实例，验证旧 Session 续聊确实写入原路径。现场根据验收结果恢复业务写入。
2. **回退到旧镜像。** 在新模式承接写入前，可用冻结的旧目录启动。已承接写入时，现场人工停写后，按同一恢复点将 SQL 中可见 Chat 导出为旧版 `chats.json`，保留其顺序、标题、扩展字段和专家绑定，排除墓碑记录。将导出的目录与当前 NAS 会话配套，验证旧镜像能读并继续写，一并核对分享 token 和访问计数。
3. **真实环境验收。** 在目标 MySQL 和真实共享 NAS 上至少用两个实例验证同 Session 并发、锁超时、进程退出后的恢复、删除重试及共享 Session 的历史存活。按代表性规模记录目录迁移耗时、SQL 批次数、失败重跑结果及冷读历史开销。新旧模式同时存在于代码期间跑相应回归，确认其他部署仍能按其原协议启动。

**发布验收**。同一旧 Session 完成历史、分页、续聊、停止、重启、压缩、重置和单轮回读。验证招乎活动指针、Cron 回执、Tracing、分享、草稿竞争与旧镜像回退。故障用例覆盖 DB 中断、NAS 写失败、提交响应丢失和清理中断，核对已确认内容与元数据都能按各自提交记录恢复。 [NAS 文件锁验收工具](../../../scripts/verify_session_nas_lock.py)

## 3. 当前实现的操作入口

DBA 初始化 `swe_conversation_sessions` 和 `swe_conversation_chats` 后，应用元数据模式的 DDL。迁移进程读取 `CMB_CAAS_SERVICEUNITID`，从 NAS 工作目录根扫描 `<scope-id>/workspaces/default`。CLI 通过 `SWE_DB_NAME` 和应用现有数据库配置连接目标库。线上进程配置 `SWE_CONVERSATION_STORAGE_AUTHORITY=mysql-metadata` 和部署的 `CMB_CAAS_SERVICEUNITID`。 [基础表 DDL](../../../scripts/sql/conversation_metadata_tables.sql) · [元数据模式 DDL](../../../scripts/sql/conversation_mysql_metadata.sql)

```bash
swe conversation-storage prepare --workspace-root /nas/swe --manifest /audit/metadata-manifest.json
swe conversation-storage migrate --manifest /audit/metadata-manifest.json
swe conversation-storage migrate --manifest /audit/metadata-manifest.json --apply --full-audit
swe conversation-storage export-metadata --manifest /audit/metadata-manifest.json --destination /audit/metadata-rollback
```

`prepare` 记录固定路径下发现的工作区、归属和扫描异常，并对 `chats.json` 做基本格式校验。source 系统级 `default` 用户只扫描 `default_<source-id>/workspaces/default`，跳过属于该用户的编码目录。扫描清单不记录 `chats.json` 摘要。发现异常时退出 2，同时保留扫描结果。`migrate` 会跳过顶层 `scan_errors` 中的目录，并继续处理清单中已识别的工作区。工作区文件修复后，`migrate` 会从同一清单重新读取并校验。对需要补迁的目录，先确认归属与线上路径，再准备待补迁来源的清单。

`migrate --apply` 读取执行时的 `chats.json` 并写入数据库。开始导入一个工作区时，在数据库中记录本次读取的摘要，供中断续跑时核对。`--full-audit` 增加对关联 Session 和归档的只读解析。已发布来源在应用重跑时跳过。原 `--workspaces` 显式清单入口仍可使用，并保留原有的摘要校验行为。

回退导出生成 `report.json` 和每个已发布工作区的 `chats.json`，报告记录其原工作区位置。现场人工停写后使用同一恢复点的导出文件和原 NAS 数据。
