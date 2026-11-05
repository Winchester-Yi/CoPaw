# 会话软租约＋代次校验实施记录

日期：2026-11-05。工作区：`.worktrees/conversation-mysql-nas`，代码基线 `8609a1f9a`，在前一轮尚未提交的续租预算修复之上调整。用户已选择[软租约方案](../specs/2026-11-05-conversation-soft-lease-design.md)。

## 当前规则

`lease_until` 只记录心跳新鲜度。过期不自动撤销 owner、清除 active Turn 或提升 generation，也不允许其他问题抢占；同一个问题重试仍只观察已有执行，不重放模型/工具。

执行资格以数据库事务中当前的 owner、Session generation、active Turn、Turn generation/state、完整 scope/关联、revision/epoch、Chat 删除状态及 scope 写屏障为准。明确撤销、更换 owner/Turn、版本变化仍拒绝旧执行；Stop 与终态仍按原规则竞争。原有恢复流程要求可信 owner stopped 和 IO drained 证据，再在 Session 锁下 fence、对账，不以过期时间替代停止证据。

因此，原执行在租期过后完成时，如果执行权和上下文仍匹配，可以正常发布并提交成功；如果恢复流程已经撤销代次，结果仍被拒绝。无需新建“补交旧内存结果”接口或绕过原 publication 协议。

## 代码入口

| 入口 | 改动 |
| --- | --- |
| `execution.ExecutionStore._validate_authority/start/finish` | 去除 lease_valid 时间授权，保留身份和状态验证；start 仍要求 admitted，不允许重放 running Turn |
| `execution.ExecutionStore.renew` | 单次 lease/3 SQL 总预算不变；超时、网络故障、连接/读写超时、连接数耗尽、锁超时和死锁转为带原异常 cause 的 StorageUnavailable；编程/完整性/执行权错误保留 |
| `execution.ExecutionStore._renew` | 允许过期后恢复心跳；UPDATE 只按 id/generation/owner/active Turn 条件更新新鲜度，影响行数必须为 1，reserved 可心跳但不能直接执行模型 |
| `publications.PublicationStore._validate_turn_authority` | reserve 与最终 commit 均改为身份/代次/状态授权，状态、消息、compact/reset 和终态使用同一规则；不跳过 revision/epoch、Stop 或 durable NAS 验证 |
| `execution._session`、`publication_associations._lock_associations`、Chat metadata 关联锁 | 删除已无消费者的 lease_valid 计算列，不修改持久化 schema |
| `runner_integration.IndexedQuery._renew` | 心跳暂态失败仅 warning 并按间隔重试；恢复 info。明确冲突或非预期错误继续取消父请求，UnknownCommit 明确排除在可重试错误之外 |
| `IndexedQuery._admit` | NAS admission 前启动心跳的改动保留，去除本地租期锚点及时间拒绝，慢 admission 最终由 SQL start 校验当前资格 |
| `IndexedQuery._own_terminal_committed` | 仍只认可本请求精确且字段匹配的 committed terminal，查询有独立 lease/3 总预算和控制容量；查询超时不覆盖原拒绝原因，外部取消继续传播 |

驱动错误分类在 renew 内延迟导入，避免修改旧存储模式的导入依赖。所有业务 publication 的 StorageUnavailable/UnknownCommit 仍按原路径报错和对账；只有心跳任务可以把规范化可用性错误当作可重试。

## 成本和失败边界

保留默认 30 秒新鲜度、10 秒调度间隔及 10 秒单次 SQL 总预算。失败后等待至少一个间隔，成功按尝试开始时间调度；若已错过下一时刻则跳过并等待下一间隔，不补发积压心跳。只在进入故障/恢复状态时记录日志，不逐次故障刷屏。

前一轮同池动态控制容量保留：活跃 IndexedQuery 共享一个位置，普通请求在默认 10 连接池内最多同时占 9 个；max_connections=1 退化共享。仍不抢占已有事务，不扩大连接上限，也不提前释放取消抵抗 IO 所在的隔离连接。

移除严格版本的“锚点后第 25 秒停止”规则。单次 SQL 预算只是资源等待上限，不是业务执行权的到期条件。心跳提交结果不明确时可在相同代次下重新心跳，但不会因此重试未知业务提交。

数据库不可用时可以继续正在运行的计算；数据库仍是结果提交和协调的必要条件，正常业务保存可能失败，软租约不保证断网时保存成功。代次只能拒绝存储写入，无法撤销已发生的工具副作用。因此不新增基于过期的自动抢占、自动重跑或后台清理。

没有修改 schema、索引、NAS 格式或 HTTP 返回契约，也没有新增前端失联状态。旧版本仍执行硬过期检查，部署必须统一版本后验收迟到结果；不能假设混合版本具备相同语义。

## 验证

先观察到严格版本在过期 start/renew/finish、过期 publication reserve/commit 和心跳可用性错误方面的 12 项预期失败；随后补充驱动错误分类、未知业务提交不得重试、延迟后不得补发积压的红绿回归。

单元测试覆盖：过期仍可执行/提交、各类 Turn publication 权威检查、durable NAS 后撤销拒绝、Stop/删除/备份屏障、owner/generation/active Turn/revision 变化、心跳故障后计算及终态成功、故障日志限频、恢复日志、按间隔重试、外部取消、精确 terminal 竞态、单次 SQL 各阶段总预算、未确认提交连接隔离及排空。

真实 MySQL 用例已调整/补充：过期完成与再次新准入、同问题不重跑/不同问题仍 busy、过期后恢复心跳、旧代次拒绝、NAS 写入期间过期后成功提交、SELECT 后过期仍可心跳，以及 writer/abort 在新鲜与过期心跳下的两种锁顺序。没有专用测试库时跳过，不能作为验收通过。

最终扩大回归：**1084 passed、51 skipped，59.16 秒**。

```bash
../../venv/bin/python -m pytest tests/unit/database tests/unit/app/conversation_storage tests/unit/routers/test_tracing_chat_history_parity.py tests/unit/routers/test_tracing_chats_api.py tests/integrated/conversation_storage/test_execution_mysql.py tests/integrated/conversation_storage/test_runner_integration_mysql.py tests/integrated/conversation_storage/test_publications_mysql.py -q
```

51 项跳过来自未配置专用库的 MySQL 模块，不算验收通过。6 个变更涉及的运行时文件以 Python 3.10 AST 模式解析通过，但这不等同于 Python 3.10 运行时测试。

本包显式文件 pre-commit 已通过，包含 mypy、Black、flake8、pylint。单独使用测试套件已文档化的 trace_sdk 替身、阻断 MySQL 导入后，旧 runtime 模块导入检查通过；这只是依赖边界验证，不是部署启动验收。

当前共享 venv 的 MySQL 驱动为 9.4.0，manifest 锁定 26.7.0，版本差异仍未消除，不升级共享环境；需在匹配部署的隔离环境验证真实 MySQL 行锁、网络故障、跨 Pod 和 NAS。未建立业务 SLO，不提供未经测量的吞吐或延迟保证。

本包按用户后续指令提交，其他既有未提交文件保持原状。
