# 会话迁移真实 MySQL 验收

日期：2026-11-06。工作树：`.worktrees/conversation-mysql-nas`，起始提交：`c95316488`。

**结论：整体验收未通过。** 345 个会话存储用例分轮执行、按 nodeid 去重并计入有效定向复测后，335 passed、9 failed、1 skipped。9 项失败归为两个实现问题：恢复校验的连接跨任务使用（8 项），以及 v3 迁移 CLI 验证的 DMLDatabase 缺少 acquire（1 项）。本轮只修改测试与报告，未修复运行时代码。

后续详细根因与推荐方案见[离线验证两个阻断修复草案](../specs/2026-11-06-conversation-offline-verification-blockers-design.md)。草案尚未实施，不改变本文验收结论。

## 范围与环境

使用用户本轮提供的 MySQL 连接，在新建的随机测试库中运行项目真实数据库集成测试。未将凭据写入仓库或修改项目数据库配置；未修改服务器全局参数。夹具逐用例创建随机 schema，并在 finally 中清理。正文与故障注入只使用本地临时目录，未连接业务 NAS。

| 项目 | 实测值 |
| --- | --- |
| 服务端 | MySQL Community Server 8.0.46 |
| max_allowed_packet | 67,108,864 字节（64 MiB） |
| lower_case_table_names | 0 |
| 服务端默认事务隔离 | REPEATABLE-READ |
| Python | 项目虚拟环境，3.12.12 |
| 验收驱动 | mysql-connector-python 26.7.0，与 pyproject.toml 一致 |
| 驱动安装方式 | 独立临时目录，通过子进程 PYTHONPATH 优先加载；共享 venv 的 9.4.0 未升级 |
| 并行方式 | 临时安装 pytest-xdist，4 个独立进程、独立用例 schema |
| 账号 | 用户提供的 root；本轮不构成最小 DML 权限验收 |

原始日志、JUnit XML 与不含口令的轮次元数据位于 `/private/tmp/swe-conversation-db-acceptance-20261106-fb198z0y/`。

测试环境使用 SWE_TEST_DB_HOST/PORT/USER/PASSWORD/NAME，清除继承的业务连接环境；没有将用户原始口令直接套入应用 SWE_DB_ACCESS 的前缀解码流程。随机空控制库与逐用例数据库分开，业务 schema 未作为测试目标。

主体命令为项目虚拟环境的 `python -m pytest tests/integrated/conversation_storage -vv -n 4 --dist load`；首轮带 `--maxfail=8`，达到阈值后仍等待已分配用例结束。第二轮以首轮 JUnit 的 nodeid 排除已经执行的 187 项，运行剩余 158 项，未设 maxfail。失败的旧测试在最小修正后单独复测，最终统计按 nodeid 去重；原始失败日志保留，不表述为一次全量通过。

第二轮连接异常后，后续按节点拆分为迁移复测、其余入口复测和定向复测；余下入口最终使用 2 个独立 worker 完成。切换执行方式时保留正常清理和原始中断报告；其中一项服务隔离测试因人为 SIGINT 产生 CancelledError → UnknownCommit，不据此判为独立产品缺陷，安排完整重跑。所有中断轮均不算全量通过，分轮重叠按 nodeid 去重。

## 执行记录

- schema 初验：7 passed，70.87 秒；该轮随机数据库全部清理。
- 首次串行全量启动：8 passed 后主动中断，正常清理，用于改为 4 进程执行；退出码 2，不计为全量验收通过。
- 全量会话存储：收集 345 项，所有节点均有执行记录；去重结果为 335 passed、9 failed、1 skipped，没有遗留未执行节点。
- 独立驱动 CRUD/事务/prepared 验证：1 passed，5.33 秒。
- Tracing 路由相关单元回归：65 passed，2.46 秒；这是单元回归，不替代真实数据库结果。

| 去重范围 | 通过 | 失败 | 跳过 |
| --- | --- | --- | --- |
| 使用真实数据库 fixture 的用例 | 277 | 9 | 1 |
| 同目录中不依赖真实库的用例 | 58 | 0 | 0 |
| 会话存储合计 | 335 | 9 | 1 |

287 项使用真实库 fixture，其中应用启动用例因缺少既有 workspace/secret 样本在主体断言前跳过，不能算该启动场景已验收。58 项为清单、协议和故障边界等非真实库测试，不能全称为数据库测试。独立驱动 smoke 和 65 项单元回归不加入 345 的去重统计。

首轮原始结果为 177 passed、9 failed、1 skipped；第二轮因连接异常并主动中断，原始结果为 99 passed、9 failed、41 errors，另外 9 项未完成。随后按节点完成迁移复测、入口复测、2 worker 收尾与定向验证，覆盖原跳过配置项以外的所有节点。完整节点、最终采用的 XML 文件和原始失败文本见 final-summary.json；不是一次完整全绿运行。

### checkpoint 真实边界

`test_checkpoint_reader_mysql.py` 的 16 项全部通过：无 active 和独立 active 分别覆盖 4,095、4,096、4,097、8,193、16,385 条；共享 active/event publication 覆盖两种分页大小；另覆盖捕获 high 后 append、epoch/revision 变化拒绝续读、取消等待实际 IO 排空期间真实 pin 保留且 GC 拒绝回收。数据库为真实 MySQL，正文 IO 为本地临时目录。

## 本轮发现并校正的旧测试预期

### Tracing 消息排序

`test_http_tracing_target_scope_permissions_and_approval_overlay` 独立复现失败：期望 `approval, task, error, anchor`，实际为 `approval, anchor, task, error`。

根因是测试期待旧顺序：当前 Tracing 已复用 Chat 的 current 装配，按照共用的 `_message_sort_key` 排序。有时间戳的 approval/anchor 在缺失时间戳的 task/error 前。测试后续本就要求 Chat 与 Tracing 返回完全相等。

只调整该测试的预期顺序，运行时代码不变。修正后真实库单独复测 1 passed；保留修正前 `tracing-repro.xml` 与修正后 `tracing-verified.xml`。

### 跨 Session 归档引用的拒绝阶段

`test_archive_rejects_cross_session_historical_turn` 独立复现失败：测试在 commit 阶段等待 IntegrityError，但 reserve 的引用归属检查已经提前抛出 `archived Turn does not belong to Session`。

只将拒绝断言移至 reserve，并验证 publication 不存在。没有延后或放宽实现校验。修正后真实库单独复测 1 passed；保留 `archive-repro.xml` 与 `archive-verified.xml`。

### 删除候选捕获的竞态注入

`test_delete_does_not_capture_chat_created_after_candidate_read` 独立复现为等待事件超时。B1b 批量查询改为复用事务 session 后，旧 PausedDatabase.fetch_all 不再被调用，导致测试根本未注入预期竞态。

将注入点移至实际的 DatabaseSession.fetch_all，仍在候选读取后插入新 Chat，再放行删除，验证新 Chat 保留且本次删除返回 False。修正后真实库单独复测 1 passed，8.92 秒；保留 `delete-repro.xml` 与 `delete-verified.xml`。运行时代码不变。

### 远程 SQL 等待与测试目的分离

- GC 锁顺序用例的 3 秒等待在远程 COMMIT 阶段超时；保持 writer 持有 Session 锁、GC 锁 publication、writer 检查 deleting 的断言，将测试等待上限改为 30 秒后单独复测通过。此测试检查锁顺序，不宣称延迟达到 SLO。
- 软租约“校验后过期仍可续约”用例人工将 lease 设为 3 秒，间接把整个 renew 的 SQL 预算降为 1 秒，远程 COMMIT 阶段超时。改为测试中的 30 秒 lease，仍在锁内强制将数据库 lease_until 设为过去，真实复测通过。实际锁等待总预算的独立测试仍保留；生产实现和默认配置均未调整。

### 分享保护夹具缺少服务归属

两个 GC 分享保护用例的手工 INSERT 未填 service_unit_key，构造了尚未完成离线归属回填的分享。GC 当前按服务归属查引用，正式启用前的 finalize 则要求该字段非空。只为测试夹具补入对应 GC scope 的服务 ID；两个用例真实复测均通过。没有放宽 GC、删除服务隔离条件或修改数据库正式 schema。

### 查询计划夹具与当前服务隔离一致

查询计划测试的五张表手工 INSERT 缺少正式 schema 要求的 service_unit_key，首先触发 MySQL 1364。为合成数据与 EXPLAIN 条件同步补入固定测试服务 ID；保持不使用 FORCE INDEX、关键查询不能全表扫描且必须命中索引的断言。修正后真实复测 1 passed，13.45 秒。

| 查询 | access type | 实际索引 | 估算行数 |
| --- | --- | --- | --- |
| 用户/通道侧栏 | ref | ix_conv_chat_channel_recent | 50 |
| 归档分页 | range | uq_conv_message_archive_order | 167 |
| 问题定位 Turn | const | uq_conv_turn_question | 1 |
| 来源 Message | ref | ix_conv_message_source | 1 |
| publication 反向引用 | ref | ix_conv_message_publication | 3 |
| 当前上下文 | ref | uq_conv_message_session_order | 200 |
| pending publication | range | ix_conv_publication_pending | 1 |
| 管理员全量范围 | ref | ix_conv_chat_channel_recent | 734 |

管理员全量查询仍有 filesort。以上为小规模合成数据的 EXPLAIN 估算，不是生产扫描行数、实际耗时或 P95/P99。证据为 query-plans-verified.xml 中的 query_plans 属性。

### 保留 v3 ledger 的迁移清单

`test_retained_v3_ledger_new_write_exports_and_old_reader_continues` 的清单遗漏必填 service_unit_id，在 MigrationManifest 校验阶段就被拒绝。清单补入与 inventory scope 相同的服务 ID，不放宽运行时校验；定向复测单独保留 ledger-verified 证据。

修正后真实库复测 1 passed，442.77 秒；包括新写入、备份、导出、旧 reader 继续写入和原 ledger 字节保留断言。

### 终态清理测试的极小预算与连接故障

剩余批后段出现连接握手断开、MySQL 1040 和本机 Errno 49。服务器 max_connections 为 151，事后观察 Threads_connected 已回落到 4；不能根据累计 Aborted_connects 值断言全部由本次测试产生。

当时在执行的终态清理测试把 lease_seconds 设为 0.001，使续租总 SQL 预算与重试间隔均约为 0.00033 秒。软租约遇到存储不可达会继续重试，这个设置既不能完成真实 SQL，也可能持续创建/取消连接。该测试已改为保留默认真实 SQL 预算，并以 asyncio.wait_for 约束 30 秒整体等待；仍在 terminal SQL 提交后、publication 清理前调用真实 renew 并验证不会取消父任务。生产 lease、重试策略与池配置均未改。

停止第二轮调度后，针对未执行及连接故障项另起串行复测；先前 schema 初验的 7 项真实通过证据保留，不把连接故障错误改写成通过。连接故障轮的原始 JUnit 与日志同样保留。

终态清理的整体等待修正补齐 asyncio 导入后，真实库定向复测通过；同轮 reserve 丢失提交响应的复测也通过（terminal-reserve-verified：2 passed，52.49 秒）。之前 reserve 用例在连接归还 baseline 查询超时并返回 UnknownCommit 的原始记录保留；一次通过不能证明网络故障已永久消除。

v3 消息批次中断续迁用例曾在第二次 importer.run 的 guard SQL 超时后返回 UnknownCommit；不放宽超时、不修改实现，随后定向复测 1 passed，544.82 秒（migration-interruption-verified）。原超时证据保留，不能据此推断生产故障恢复达到某个可用性 SLO。普通 v3 与双服务迁移也完成真实回读验证。

## 已确认的运行时缺陷

多范围恢复独立复现失败：`restore_backup` 在安装事务内调用完整 reader 验证；reader 的 `RetentionStore.finish_reader` 创建 release 子任务，该子任务继承父任务 SQL 连接上下文，违反连接的 task ownership，抛出 `RuntimeError: Database session belongs to a different task`。

已确认的 8 条失败路径均为此根因：

| 测试文件 | 受阻路径 | 数量 |
| --- | --- | --- |
| test_backup_set.py | 多 scope/孤立分享联合恢复；多服务同 NAS 相对路径恢复 | 2 |
| test_rollback.py | 新备份与旧 reader 往返；GC tombstone 恢复；active Turn 恢复；孤立分享恢复；restore verifier 回调 | 5 |
| test_runner_integration_mysql.py | 恢复后 Stop 拒绝迟到 prepared completed 结果 | 1 |

上述测试在恢复验读阶段提前退出，后续业务断言未完成，不能将未执行的后半段视为通过。普通请求的 pin/GC、checkpoint 或其他恢复前步骤通过，也不能替代这 8 条端到端路径。

该问题影响恢复验读与 ready 生成，不能用此前单元测试通过代替真实数据库验收。本轮未修改该实现，原始失败及独立复现证据 backup-set-repro.xml 均已保留。后续修复必须同时保留完整验读、事务失败回滚和 reader 生命周期；不能简单移除 task ownership 检查，或另开连接读取未提交的恢复数据。

建议优先评估复用 `migration/readonly.py` 已有的 ReadOnlyDatabase / ReadOnlyRetention 思路：在经过验证的停写、停 GC、隔离恢复目标边界下，同任务、同安装事务完整遍历 Session/Chat/History/Archive/Turn/Share reader；不插入或释放在线 reader retention，不改写捕获的 retention 元数据。现有 SourceBoundary 与恢复 RestoreEvidence 的证据并非可直接互换，需要明确适配和封装，禁止给在线路径开放“跳过 pin”开关。复测还须证明损坏/缺失正文、取消和中途失败均回滚，完整 checkpoint iterator 正常穷尽后才生成 ready。

### v3 迁移 CLI 的受限数据库接口缺失

`test_v3_discovery_import_resume_verify_finalize` 已完成发现、preflight、注入 SQL 提交后中断及 resume，但真实 CLI verify 返回 exit_code=1，异常为 `AttributeError: 'DMLDatabase' object has no attribute 'acquire'`；后续 finalize 及 ready 断言没有执行。

调用路径为 `conversation_storage_v3_cmd._execute` 的 named_lock → DMLDatabase(control) → verify_import → ReadOnlyDatabase → 完整 reader 的 acquire。B1b/B2 的有界查询现在需要获取 session 的 packet 预算；ReadOnlyDatabase.acquire 转调被包装对象 acquire，但 preflight.DMLDatabase 仅提供 CRUD、prepared 与 transaction，没有 acquire。该错误由实际 CLI＋真实库测试发现，不是旧测试排序或等待预算问题；证据为 tail-final.xml。

建议统一受 SQL 策略限制的 session facade，补齐 acquire 所需的读取与 packet 能力，保持 named_lock 的原物理连接、原事务边界与 DML/只读 SQL 校验。不能通过返回裸 session 或无条件透传 __getattr__ 绕开无 DDL 约束。修复后须重新执行完整 CLI verify/finalize，并证明 DDL/OUTFILE、非法 session 逃逸和跨 task 使用仍被拒绝。本轮未修改该实现。

## 清理与交付检查

- 所有测试进程结束后再次查询服务器：本轮随机控制库、用例库、restore/staging/install 库均无残留，没有测试连接残留。连接故障遗留的一个本轮随机库经创建记录和无活跃连接确认后单独清理。
- 保留两个 2026-11-02 已存在的测试数据库，未把“名称看起来像测试库”当作删除授权。最终审计见 final-database-audit.json 与 owned-orphan-cleanup.json。
- 仓库外权限 0600 的 connection.json 已删除；本轮测试、文档、日志、XML 与 JSON 输出扫描未发现原始口令。未修改项目连接配置，未升级共享 venv；共享 venv 驱动终检仍为 9.4.0。
- 本轮修正 8 个测试文件中的旧夹具、断言、注入点和测试预算，运行时代码未改。8 个测试文件及 3 份文档的 pre-commit 全部通过，git diff --check 通过。未自动提交。

主要证据：[去重结果](/private/tmp/swe-conversation-db-acceptance-20261106-fb198z0y/final-summary.json)、[首轮 JUnit](/private/tmp/swe-conversation-db-acceptance-20261106-fb198z0y/full-parallel.xml)、[收尾 JUnit](/private/tmp/swe-conversation-db-acceptance-20261106-fb198z0y/tail-final.xml)。其余原始轮次和定向复测 XML 同样保留在该临时目录。

## 验收边界

- 本轮不能替代跨节点 NAS、真实 Kubernetes、多 Pod 退出与滚动部署验证。
- 当前实际 packet 为 64 MiB；不能由此声称已验证服务端真实小 packet 配置。
- 使用随机合成历史验证迁移及备份恢复，不代表完整生产历史已迁移或已完成停机演练。
- 使用 root，不代表 DML 最小权限账号已经验证。
- 现有 workspace/secret 样本未配置，应用真实启动用例在全量轮次中跳过；最终结果须明确保留该未验收项。
- Compact 流程优化继续按用户决定延后，本轮不修改其运行时逻辑。
