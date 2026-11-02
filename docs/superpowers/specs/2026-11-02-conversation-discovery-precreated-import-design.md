# 会话自动发现与预建表直接导入设计

日期：2026-11-02。状态：待评审，未实现或执行数据库变更。
代码基线：881ab1764；工作区：.worktrees/conversation-mysql-nas。

## 1. 范围和成功标准

解决两点：自动发现原 workspace，正常数据无需逐个填写归属；迁移工具仅检查结构、执行 DML，使用 DBA 预建的应用表，不创建库/表、不改表、不换表。

复用原业务 workspace、共享 MySQL 和各服务原 NAS；稳定服务身份来自 CMB_CAAS_SERVICEUNITID。沿用已有网关分流。停写批量迁移，不实现在线增量同步。本次不改变源只读及源/输出目录不重叠限制；直接扫描原 NAS 并排除输出目录是第 1 点的独立改造，不能声称已随本方案实现。CLI 仍需明确区分冻结源根、配置根和原目标 NAS 根。

成功标准：候选覆盖完整、四维归属可追溯、异常阻断；无 DDL 权限账号能完成导入/续跑/校验；共享库全局 ID 无冲突；历史 token、分享八旧字段、计数和原文件不变；全部服务验收后才启用新版。

## 2. 当前实现与差距

- OfflineJob 强制 staging schema 和手工 workspaces；execute 调用 prepare_staging。
- WorkspaceMapping 已有 scope 和路径边界；Session 身份来自 chats.json 或人工 session_paths，替换后的文件名不能可靠反推身份。
- ConversationInstaller 建临时库、复制分享结构、执行扩列和收紧，再整体 RENAME TABLE，拒绝已存在六表的应用目标。
- MigrationManifest.allocate 每种实体从 1 分配 ID，只适合独占空 staging，不能合并到可能已有其他服务的共享表。
- AgentProfileRef 保存 id/workspace_dir，agent.json 保存完整 Agent 身份；config.context 提供 scope 编解码；runtime.resolved_scope 已有归属规则。
- 旧分享在 snapshot_root/tenant/chat_shares/token.json；不保证位于某个 Agent workspace。当前 snapshot_workspace_index 耦合需要解除。

## 3. 方案取舍

| 路线 | 优势 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 自动发现但保留 staging 换表 | 复用最多代码 | 仍执行 DDL | 不采用 |
| 自动冻结清单，预建应用表，分批 DML 导入 | 满足权限边界，可续跑，保留原分享表 | 需要 SQL 迁移日志和启动门禁 | 推荐 |
| 全量超大事务 | 似乎整体原子 | NAS IO 长事务和规模故障 | 不采用 |

复用已有不可变 bundle 发布协议。直接导入不再有换表原子性，依靠完整停写窗口、持久化日志、全量验证和启动门禁切换。

## 4. 自动发现

### 4.1 输入和无副作用解析

单服务输入为源根、已有配置根、原目标 NAS 根、审计目录和现有数据库配置；服务标识由环境读取并冻结，有显式参数时必须一致。多服务只需短 services 文件声明各服务和这些根，不逐 workspace 手填。密码不落盘。

CLI 纯解析已有文件和只读注册数据，不初始化 Workspace、Agent、TenantPool、Provider、Cron，也不调用 ensure_schema。不得用会生成默认 Agent/路径的配置 factory 或带写回的加载器。

冻结副本中的 config 可能仍记录原 NAS 绝对 workspace_dir：按服务声明的 original-root → frozen-source-root 做唯一可验证前缀重映射，保留相对路径并再次检查边界；相对 workspace_dir 按被迁移旧版本的实际规则解析。不能直接打开配置中的原绝对路径，也不能失败后回退读取活动 NAS。外部自定义 workspace 必须显式声明对应根映射，不猜路径。

### 4.2 候选与证据

1. 读取受支持租户布局和根 config.json 的全部 agents.profiles，包括 disabled Agent；核对 profile key/id、workspace_dir 和已有 agent.json.id。
2. 补充扫描会话标记，覆盖根级旧 workspace、未注册或已删配置但仍有 chats/sessions/dialog 的目录。只建立候选，不据目录名猜 agent。未知布局、权限失败或符号链接都不能当作空数据。
3. tenant/source 使用可往返验证的 scope 编码、既有 default_source 规则或明确注册证据。裸 tenant 缺 source 时只在登记唯一且与配置路径一致时自动补齐；多 source 阻断。共享库登记没有 service 字段，不能单独证明服务归属；Chat.user_id 不能作为 tenant。
4. Agent 来自一致的 profile/agent.json。缺 source、缺 Agent、证据冲突、同一物理根映射多个 scope、多个物理根映射同一 scope 都阻断，不自动合并。
5. Session 使用 chats.json 原始 user/channel/session 字段及可靠旧格式内部身份。文件名仅验证路径，替换碰撞、无可靠身份的孤立 session 才需要人工 override；重复消息 ID 不作为去重依据。
6. 空 workspace 记 empty；残留 ledger、删除归档和孤立文件均进入覆盖清单。任何无法解释候选形成 blocker，不静默跳过。归属 override 仅针对异常项，绑定稳定候选键、证据版本和说明，不提供全局默认租户回退。

发现报告记录完整 scope、证据引用、原配置相对路径、计数和异常码。使用受支持布局白名单加会话标记覆盖扫描；路径越界或重复物理路径显式报错。发现到未注册目录不等于已解决其身份。

### 4.3 分享

分页读取原 swe_chat_shares 八旧字段；按原 snapshot_key 和旧安全规则在所有声明服务 snapshot_root 查找。每个 token 必须有且仅有一份明确服务归属，零份或多份阻断，即使内容相同。只有一个服务根的作业不能声称覆盖全共享库未归属分享；全局切换必须汇总全部服务发现结果。

Chat 已删除仍可 legacy_share；分享根独立于 workspace，不为分享创建 workspace。snapshot_root 从现有部署配置解析，无法取得时只补一个服务级参数。保留全局 token 唯一及原 collation，不根据 chat_id 或当前环境默认归属。

v3 以冻结 services 清单作为服务/NAS 集合权威，而非从 workspaces 推导；零 workspace、仅孤立分享的服务也须保留。同步修改 target binding、bundle_stores、覆盖校验和 runtime 文件生成，分享不再要求 snapshot_workspace_index。

### 4.4 冻结

迁移 manifest v3 与旧 staging v2 隔离，最终 runtime scope 文件沿用现有 v2。稳定源键由种类、服务和原始相对位置规范编码，不能使用遍历序号；超限报错，不截断身份。

discover 不写数据库或源文件，输出完整清单及 blockers。freeze 固定服务根、配置/文件覆盖、分享原行、解析器版本、目标绑定和内容指纹。指纹仅检测变化，不作为领域身份。读取前后及最终校验重新检查内容、新增/删除和配置变化；异常阻断。仍需停写证据，指纹不能替代冻结。

## 5. 人工预建、零 DDL

DBA 提前执行六表脚本、旧分享表扩展和独立迁移控制表脚本。分享服务和 locator 在离线回填阶段允许规定的 NULL，导入全量验证后由 DBA 执行 NOT NULL 收紧。运行时只接受最终结构。

新增两张独立控制表：jobs 保存作业、目标/源绑定、阶段；entries 保存源→目标映射、批次状态与分享扩展字段回填前值。由新增 conversation_migration_control_tables.sql 人工创建。控制表不加入七张业务表的备份恢复集合，保留续跑审计，由 DBA 清理。

工具只执行 SELECT/INSERT/UPDATE，必要的控制记录清理可使用限定作业的 DELETE；不执行 CREATE、ALTER、DROP、RENAME、TRUNCATE 或临时表 DDL，也不调用 atomic rename probe。初始化与依赖 helper 同受约束，不尝试自动修复 schema。迁移账号无需 DDL 权限。

## 6. 预检、ID 与续跑

### 6.1 目标与独占

复用 schema_contract 校验列、类型、NULL/default/extra、索引顺序、唯一性、字符语义及 InnoDB。迁移结构契约只允许分享规定的过渡 NULL，不泛化放宽 readiness。保存原分享八列及索引定义基线。

本设计仅支持共享库六张新表首次迁移：首次创建协调作业时六表业务数据为空；之后各服务/各 scope 作为同一作业分批导入，续跑只接受同 migration_id、同绑定且可逐批证明归属的行。未知既存行阻断，不支持已经开启新版写入后追加另一个迁移作业。不能要求每个 workspace 开始时七表整体为空；原分享表非空属于正常输入。

专用连接持有应用 schema 级命名锁，串行化离线迁移；全部批次使用该受控连接，锁连接丢失立即停止，禁止自动重连后继续变更。数据库锁不代替业务停写。未完成作业不允许另起 migration_id 抢占。

### 6.2 分配和提交

v3 不使用 manifest 从 1 分配或 MAX(id)+1，也不往业务表插缺 locator 的占位消息。以 entries.id 的 AUTO_INCREMENT 作为统一整数 ID 票据：为每个需提前分配 ID 的源实体（Session/Chat/Turn/Message/Publication）插入 entries，取得 lastrowid，再在同事务将 target_id 设为该 id 并提交；只有持久化后才构造不可变 bundle。目标业务行显式使用该票据 ID，publication 预留同样使用已持久票据。这五张实体表共用更稀疏但全局不重复的数值空间；纯 share/batch 日志消耗的 ID 和回滚空洞都允许。

Retention 不使用票据显式插入：gc_guard/dependency 等继续沿 RetentionStore 的业务唯一键和本表 AUTO_INCREMENT 分配，日志保存实际 ID 与业务键，通过完整 service/scope、关联 publication 和依赖集合证明归属。Retention 的日志 id 与 target_id 可以不同，不要求每个在线临时 pin 都有源实体映射；只读验收继续使用非持久 pin。未知 scope 或无法证明的关联阻断。不得混用票据和本表自增写 Retention。

首次作业在六表为空时启用票据分配，后续只接受五类业务行均由本作业 entries 映射证明、Retention 按上一段归属规则证明，且上述五类整数实体 target_id=id；禁止控制表重建后继续导入。业务旧 AUTO_INCREMENT 高水位不影响显式票据插入，只要对应实体 ID 不存在；每批仍检查实际冲突。禁止新版权威启动后再追加迁移，因在线自增未使用票据，不能假设二者永不冲突。需要已上线库增量导入时必须另行设计分配器交接。离线中间状态不能开放运行时。

Publication 票据按发布依赖拓扑顺序首次分配并持久化：所有被引用 Publication 的 ID 先确定，依赖方后分配，保持现有 target_id < source_id 约束。不能按源键排序提前分配全部 Publication。

publication 首次预留固定 operation_key、ID 和路径；NAS durable 后沿原协议在同事务提交 typed rows、关联更新、publication committed 与批次完成状态。外部 manifest 为审计镜像，数据库是续跑权威。控制表是离线入口依赖，不注入普通在线 publication 路径。

NAS 完成但 SQL 未提交时，重试校验既有文件并继续；SQL 已提交而外部日志缺失时从 SQL 重建镜像。源、计划、既有路径内容或目标行不同必须阻断，不能生成新 operation 掩盖冲突。继续沿用有界 pin/dependency，不因长期会话产生超过 4,096 publication 的批次。

### 6.3 分享原位 CAS

不能复用当前插入 token 的 ShareInstall。按 token 锁原行，逐项比较冻结八字段及 token collation 语义；只更新 service_unit_key/publication_id/byte_offset/byte_length。禁止 REPLACE 或宽松 upsert，不改变计数、时间和 token。

扩展列原值进入 entries；publication 提交、分享 CAS 和批次日志同事务完成。相同 locator 仅在本作业拥有且 bundle 通过校验时幂等，未知非 NULL 值阻断。计数变化意味着仍有写入，停止。

## 7. 验收、切换与失败

状态 planned → importing → imported → verified → ready，可 blocked 后同绑定恢复。崩溃保持 importing，由 resume 对账。ready 表示离线就绪，不等于业务已开放。

verify 使用只读 facade，不通过改日志伪装只读：比较所有 Session/Chat/消息 occurrence、归档、事件、checkpoint、分享和引用闭包，核对候选覆盖和全库旧 token 的唯一服务归属。不能仅证明单个服务导入成功。首次开放入口之前，源/结构/导入行变化使先前验收结果失效。正常上线后不再比较冻结时的业务行指纹；ready 是历史完成证明，允许正常写入后的重启。

DBA 收紧分享 NOT NULL 后，finalize 重跑最终结构及全量语义验收，finalize 按自己重跑的结果推进 imported → verified → ready，不信任外部报告中的 passed 字段；SQL 持久化 ready 及完整目标绑定，原子写每服务 runtime scope 文件和完成凭证。文件中断可按 SQL 状态重建；缺文件、服务/根/作业不符均拒绝启动。

本次迁移绑定服务启用 mysql authority 前增加 ready/完成集合/绑定校验，保留原 readiness；该检查不强迫全新空环境拥有历史作业。各服务新版先在入口关闭状态自检，全部满足后再统一开启既有入口。重启只检查完成证明、服务/NAS/模式绑定和当前 schema，不重验冻结时期的行集合。

备份/恢复必须同步携带最小不可变迁移完成证明（migration_id、协议版本、完成服务集合及安装绑定），纳入现有备份完整性校验，不包含全部 entries 审计日志。恢复只在完整数据验证成功后、激活前，以同一次元数据恢复事务写入对应 jobs.ready 证明，目标服务器/根变更须明确重绑定并生成相应 runtime 文件。多 scope/多服务恢复在整体成功后才安装凭证，部分恢复不能标记为原完整服务验收成功。恢复作业使用新的恢复证明身份及明确完成集合，保留来源 migration_id 供审计，不伪造原作业的全部进度。恢复后的日志不支持继续旧导入；缺日志不能自动判为全新空环境而放行。控制表不属于七表原始行备份集合，但恢复目标由 DBA 预建它们，备份格式需升级以支持完成证明。旧备份缺证明时不能绕过门禁，必须经过明确的离线验证升级流程。

v3 不执行 install rename，旧 install --apply 提示使用 finalize。旧 v2 只保留 inspect/只读验证等审计入口，生产迁移写入口拒绝其 DDL 模式；原 probe 如保留，仅用于明确开发测试工具。

默认修复方式为同作业续跑，不清空整表、不触碰其他服务。源变化需重新冻结时必须保留故障现场，不能新 ID 吞并半成品。上线前撤销依靠一致性备份和 DBA 审核恢复，原文件没改不代表共享库没改；上线产生新写入后不提供自动回旧格式。本次不增加未经验证的逐行逆操作。

## 8. 拟定命令和改动范围

下列为新接口设计，当前代码尚不支持：

1. discover --source-root ... --config-root ... --nas-root ... --output ...；多服务使用 --services。
2. freeze --discovery ... --manifest ...，仅异常时 --overrides。
3. preflight --manifest ...：只读结构、数据和绑定检查。
4. import/resume --manifest ... --apply：DML 导入及对账。
5. verify --manifest ...：只读全量校验。
6. DBA 执行分享归属收紧脚本。
7. finalize --manifest ... --apply：重新验收、更新作业、生成 runtime 文件。

模块变更：CLI/manifest v3；新增纯 discovery 和 journal；importer/install_records 改 SQL 票据全局 ID、事务日志及分享 CAS；拆开 install 结构检查和 DDL；verify/runtime 增加覆盖验收和启动门禁；backup/restore 升级完成证明及原子安装契约。保留现有原 workspace 和不可变 bundle 协议。同步六表 SQL 说明、新增控制表草案、分享扩展/收紧说明；不把草案误标为已支持。

## 9. 验收计划

- 归属：新旧编码、default_source、裸 tenant 多 source、disabled Agent、删除配置、空 registry、孤立 session/分享、重复物理根/同 scope 多根、符号链接/越界、文件名碰撞、枚举顺序变化。
- 共享库：另一服务既有数据、全局 ID、同名跨服务会话、未知既存行、分享零归属/多归属/旧字段变化、原 collation token 冲突。
- 故障：映射事务、publication 预留、NAS durable、SQL committed、外部日志写入、finalize 文件输出逐点中断；锁连接丢失；长期会话超过 4,096 publication。
- 权限：MySQL 集成使用仅有 DML 权限账号；建表由独立 fixture 管理账号执行。单元代理拒绝所有 DDL，覆盖连接初始化和依赖入口。
- 完成：源和其他服务数据前后不变；全量语义/覆盖一致；未 ready 拒绝启动；旧 v2 写命令拒绝；生成 runtime 文件保持原业务路径；上线写入后重启；带完成证明的全量及多 scope 恢复、部分恢复不越权、旧备份缺凭证阻断。

本轮交付设计和人工 SQL 草案。后续实现需按上述用例在隔离 MySQL 与模拟 NAS 验证，不能用文档审阅替代运行验收。
