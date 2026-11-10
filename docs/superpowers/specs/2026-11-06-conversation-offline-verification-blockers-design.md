# 会话离线验证两个阻断：原因与修复方案草案

日期：2026-11-06。工作树：`.worktrees/conversation-mysql-nas`。状态：方案草案，未实施运行时修复。

## 目标与约束

修复真实 MySQL 验收的两个实现阻断：8 条恢复路径的 task ownership 错误，以及 1 条 v3 CLI verify 的 acquire 缺失。原始证据见[验收报告](../reports/2026-11-06-conversation-real-mysql-acceptance.md)。

保持现有 schema、索引、NAS 格式、HTTP/CLI 成功语义与软租约规则。非必要减少 SQL/NAS 交互；完整验读不得被缓存、抽样或日志替代。Compact 优化继续延期。本包不改在线 reader 的 pin/GC 生命周期，不重构通用连接池。

## 阻断一：恢复事务与在线 reader 清理冲突

### 当前调用链与根因

1. restore_backup 验证显式 RestoreEvidence、隔离目标、空库和备份集合，将正文复制到恢复 NAS 目录。
2. 开启一个外层 SQL 事务，插入恢复元数据；_verify_restored_metadata 完整比对数据库。
3. 外层事务未提交，_verify_restored_readers 逐一运行 Session、Chat、History、Archive/checkpoint、Turn 和 Share 实际读取器。
4. 读取器内部的 transaction 复用外层事务；局部 async with 结束不代表恢复安装事务已提交。在线 RetentionStore.pin 也在该外层事务中写入 reader retention。
5. finish_reader 在 NAS IO 排空后用 asyncio.create_task 执行 release，并 shield 该子任务，保证在线请求取消时仍能释放 pin。
6. Python 子任务继承 ContextVar 中的数据库 session 绑定，但 DatabaseSession._owner 仍是原任务。子任务 acquire 查到原 session，_check 拒绝跨任务使用，抛出 Database session belongs to a different task。

这是稳定的调用契约冲突，不依赖有其他用户并发更新，也不是 lease 过期、4096 上限或数据库权限不足。正常在线 reader 在短事务结束后做 NAS IO、再独立释放 pin；恢复却把这些步骤包在未提交的安装事务中。

潜在的第二层问题：若只把 release 改成当前任务执行，仍会新增 reader retention、写 released tombstone 并推进自增序列。现有元数据比对发生在 reader 验证前，可能漏掉这些额外验证痕迹，且离线验读额外付出 INSERT/UPDATE 成本。

### 方案比较

| 方案 | 影响 | 结论 |
| --- | --- | --- |
| 离线只读验读，使用内存读取批次 | 保留原外层事务、完整 reader 语义；不写验证 retention；复用已有迁移机制 | 推荐 |
| finish_reader 在当前事务任务直接释放 | 仍需新增事务内取消协议，且产生额外 retention 行 | 不作为本包修复 |
| 先提交恢复，再执行在线 reader | 验读失败时恢复数据已提交，改变原子性与失败语义 | 不采用 |

删除 ownership 检查、清空 ContextVar 后另开连接也不能解决契约：新连接看不到原事务未提交数据，数据库连接原有跨任务保护也不能取消。

### 推荐设计

复用 migration/readonly.py 的 ReadOnlyDatabase、ReadOnlyRetention、read_only_adapter，给恢复增加一个内部、显式的只读验证构造入口。

边界要求：

- 保留迁移的 SourceBoundary 构造语义；恢复使用既有 RestoreEvidence，禁止伪造 snapshot ID、冻结时间或旧镜像 digest 来凑 SourceBoundary。
- 恢复只读 facade 在 restore_backup 已完成目标身份/空库检查，且已进入安装事务后构造。显式绑定当前数据库、当前任务和当前事务；只用于本次恢复验读。
- RestoreEvidence 是外部操作证据引用，字符串非空本身不会停止进程。运行手册须保证恢复目标未启动业务和 GC，恢复目录在验收完成前不对外可写；不新增租约、Redis 协调或在线 skip_pin 参数。
- facade 内只允许经过校验的 SELECT；嵌套 transaction/acquire 复用当前连接，不 commit、另开连接或返回裸 session。离线只读指禁止元数据写入，可保留已有 SELECT FOR UPDATE 引用检查。

接入必须覆盖全部读取器：

| 验证入口 | 接入方式 |
| --- | --- |
| MySQLSessionAdapter 的完整 Session 与 Turn 读取 | 只读数据库 + read_only_adapter |
| MySQLChatRepository | 只读数据库 + read_only_adapter |
| HistoryStore，包括其内部 chats | 只读数据库 + read_only_adapter，复用已有递归注入 |
| ArchiveReader，包括 checkpoint 完整 iterator | 只读数据库 + read_only_adapter |
| ShareStorage，包括 legacy_share scope | 只读数据库 + retention_factory=ReadOnlyRetention，使用 verify_snapshot，不累计访问次数 |

ReadOnlyRetention.pin 继续按原 4096 单批上限，批查真实 publication，验证 scope、完整引用集合、committed/live；返回 PinBatch 跟踪 NAS worker。它省略的是数据库 retention 写入，不能省略 publication、extent、正文格式或 reader 组装校验。

finish_reader 只 drain 内存批次；取消时必须先等实际 NAS worker 退出，再传播 CancelledError。数据库 SELECT 仍在原任务执行，线程只处理 NAS。所有批次/iterator 正常关闭后，异常才能离开安装事务并触发原回滚流程。

恢复顺序维持：复制正文 → 安装事务 → 插入恢复元数据 → 完整元数据比对 → 全部只读 reader 验证 → 安装迁移 authority（有凭据时）→ 提交 → 返回恢复报告。活动 Turn 的 settling/needs_settlement 规则照旧，完整验读成功不自动等于 restore_ready。

失败时仍遵守现有 SQL 回滚与隔离 NAS 目录处理语义。SQL rollback 不会自动删除已复制文件，不把本修复扩展为新的文件回滚协议；失败目录不得被当作 ready 或与活动目录混用。

性能收益是省去离线验证生成 pin/dependency guard 相关写入与释放；保留真实 cold NAS 读回。本包不引入缓存、NAS 批读或事务外提前提交。大恢复仍会长时间占用一个安装事务，这是原子恢复现有成本，后续如要改变需单独设计。

## 阻断二：DMLDatabase 未实现批量查询的 session 契约

### 当前调用链与根因

v3 CLI _execute 用 control.named_lock 固定物理连接，在 DMLDatabase(control) 上执行导入/验证；verify_import 再包装 ReadOnlyDatabase，实际 reader 调用 reference_queries 或 collection_queries。

批量查询需要 database.acquire，读取该 session 初始化时已知的 max_allowed_packet，再在同一 lease 上执行有界 SQL。ReadOnlyDatabase 已有 acquire，但调用内部 DMLDatabase.acquire 时失败。DMLDatabase 目前只有 CRUD/prepared/transaction，未提供 acquire 或 session 的 packet/事务状态能力。

普通迁移验证直接包装 DatabaseConnection 因而可运行，v3 CLI 多一层 DMLDatabase 才触发缺口；只测 CRUD 转发或裸数据库替身无法覆盖两层 facade 的组合。该问题不会通过提升数据库权限、放宽 packet 或增加超时解决。

### 方案比较

| 方案 | 影响 | 结论 |
| --- | --- | --- |
| 为 DMLDatabase 补齐受限 acquire/session | 沿用批量查询和真实 packet 预算，维持 SQL 策略及锁连接 | 推荐 |
| verify 绕过 DMLDatabase 改用 control | 单路径可修，但封装仍不满足公共批查接口，其他调用可能再次失败 | 不推荐作为完整修复 |
| acquire 直接返回裸 session 或 __getattr__ 全透传 | 调用方可绕过 DML SQL 校验，违背直接迁移无 DDL 约束 | 不采用 |

### 推荐设计

在 preflight.py 内扩展现有 facade，不引入通用权限框架。DMLDatabase.acquire 从原 owner 的 acquire 获取当前 task-owned lease，并返回一个明确列出能力的受限 session；transaction 返回的 DMLDatabase 也必须支持 acquire。

| 能力 | 约束 |
| --- | --- |
| max_allowed_packet | 委托当前原始 session 的已校验属性，读取不新增 SQL，保留 task/可用性检查 |
| _in_transaction | 只读反映同一 session 状态，满足集合 UPDATE 的调用方事务检查 |
| fetch_one/fetch_all/execute/execute_result/insert/execute_many | 每次先执行原 DML SQL 校验，再调用绑定 session |
| 嵌套 acquire/transaction | 复用原 owner/current lease；不能切换连接、提前提交或让 named_lock 失效 |
| 外层 ReadOnlyDatabase.acquire | 仅暴露 packet 与经过只读校验的查询方法，不能经内层 DML facade 获得写能力 |

不暴露原始 connection/cursor、commit/rollback、连接池或无条件属性转发。底层 session 生命周期与 task ownership 保留；新 facade 的读取属性同样要遵守它，不能把失效 session 的 packet 缓存成可继续使用的凭据。session 若作为可保存对象返回，还需验证离开所属 acquire 后的误用不会重新从 owner 取得另一条连接。

批查逻辑、256 IDs/64 KiB 和服务端 packet 的切批算法保持不变，不退回逐 publication 查询，不在每批 SELECT packet。

## 实施顺序与验收

建议先修 DML session 契约，再接入恢复只读验读。前者是后者复用只读 facade 的基础，也可先独立提交和审查。两包均不修改线上读取接口。

| 阶段 | 必须证明 |
| --- | --- |
| 最小单元回归 | DML acquire/transaction 与 ReadOnly 外层组合可以真实经过批查 helper；SELECT 正常，DDL/多语句/OUTFILE 和只读层 UPDATE 仍拒绝 |
| 连接契约回归 | named_lock 内 session 身份不变；事务内集合 UPDATE 整体回滚；退出 lease/跨 task 使用拒绝；packet 低边界正确切批且不新增探测 SQL |
| 恢复单元回归 | 六类 reader 全部注入只读 retention；验证阶段 SQL mutation 为零；取消和损坏正文传播失败，NAS 排空后回滚；Share 不增长访问计数 |
| 真实 CLI | 重跑 test_v3_discovery_import_resume_verify_finalize，完整通过 verify/finalize/ready；失败不得写 verified/ready 或允许绕过语义校验 |
| 真实恢复 | 原 8 条失败路径全部通过，执行到原先被阻断的后半段断言；多 scope、多服务、legacy orphan 和 active Turn 均覆盖 |
| 完整性补充 | 最后一页 checkpoint 损坏/超限导致失败；4,097/16,385+ 的尾部不能漏验；取消不提前结束 NAS；失败后目标 SQL 仍为空，未生成 ready |
| 元数据保持 | 验读前后 retentions 内容和自增值不变；原备份 pin、dependency、released tombstone 和服务隔离均保留 |
| 兼容回归 | 在线 pin/取消/GC、软租约、checkpoint record/prefix/page/full、批写回滚、旧版迁移验证和 share 访问统计仍通过 |

真实库定向验收后，再执行完整会话存储测试。使用匹配部署的驱动和独立随机库，控制测试并发与 lease/SQL 预算，保留原始失败及复测结果，不重复极小 lease 设置。真实 NAS/多 Pod、最小权限和既有 workspace 启动仍是独立验收边界。

## 本轮分析验证

本轮在项目虚拟环境、工作树 src 和已有 trace_sdk 测试替身环境下，用实际 ReadOnlyDatabase/DMLDatabase/reference helper 组合复现 acquire 缺失；用实际 RetentionStore.finish_reader 与 DatabaseSession._check 复现清理子任务 ownership 错误。未连接数据库，未创建或修改运行时代码。这些最小复现支持根因分析，不代表推荐修复已实现或真实库已转绿。
