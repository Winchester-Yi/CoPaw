# 会话续租总预算与控制容量修复

> 本文记录先前的严格租约版本。用户已选择并切换为[软租约＋代次校验](../specs/2026-11-05-conversation-soft-lease-design.md)：过期不撤权、心跳可用性失败重试；本地 25 秒截止和过期拒绝规则已移除。单次 SQL 预算、控制容量和取消隔离保留。

日期：2026-11-05。基线：`8609a1f9a`。范围：`.worktrees/conversation-mysql-nas`。

## 根因与影响

原租约为 30 秒，Runner 每隔 10 秒进入一次续租；共享池 acquire_timeout 为 30 秒，read/write_timeout 默认为 600 秒。只约束任一 SQL 或队列阶段，都不能防止整次续租跨过剩余租期。旧调度还把上次操作耗时加到下次等待前，不能建立稳定的剩余预算。

同时存在两个缺口：reserve 后 admission 发布可能执行 NAS IO，但 renewer 在 publish 和 start 完成后才启动；续租 SELECT 得到的 lease_valid 在后续 Chat/Turn 锁等待后可能过时，最终 UPDATE 未再次检查当前数据库租期。

## 已实现

- `ExecutionStore.renew` 使用原 task 内的 AnyIO 总超时，预算为 lease/3，覆盖队列、连接建立/探活、事务开始、guard/关联锁、UPDATE、COMMIT 与连接归还检查。保持 Python 3.10 支持，不用 wait_for 将绑定事务搬进新 task。到期抛出带明确文案的 TimeoutError；事件循环来不及投递 timer 时，返回前也复核单调时间。
- IndexedQuery 在 reserve 尝试前记录保守锚点，取得新 authority 后、NAS admission publish 前启动续租。reserved 状态只增加续租能力，start 仍要求 admitted。admission 与模型入口都检查本地剩余预算；续租失败时取消请求，admission 未确认结果继续保留原 operation key 以供对账。
- 每次调度锚定上次成功尝试的开始时间，不以返回时间估算新租期。周期截止为 anchor+5lease/6，terminal 查询也在此预算内。默认租约下是 10 秒调度间隔、最多 10 秒续租操作、最迟第 25 秒结束等待，留出 5 秒安全余量。延迟唤醒不重置预算。超时不重试或推测提交成功。
- 精确 terminal 对账仍只认可本请求且字段匹配的已提交 terminal publication，避免取消正常结算后的 reader/NAS 清理。该查询也使用控制容量；外部取消继续原样传播。
- 最终 UPDATE 同时检查 Session id、generation、owner、active turn 和数据库当前 lease_until；影响行数不是 1 就拒绝。保留原 scope/backup fence、Session/Chat/Turn 关联验证与锁顺序。
- 活跃 IndexedQuery 在共享池注册控制容量作用域；所有请求共用一个预留位置，而非每会话加连接。普通获取在占用量小于 max_connections-1 时准入，控制获取可使用全部上限。max_connections=1 时退化共享。请求结束或取消会解除作用域并唤醒等待者。
- 不抢占已有普通事务，不提前释放仍有 IO 的隔离连接；新的控制容量也计入同一个 max_connections。连接超时后的 retained destroy/quarantine 保持原有安全协议，排空后才允许替换。

## 性能与可用性边界

没有改 schema、索引、NAS 格式或 HTTP 返回契约；续租路径没有 NAS IO。没有新增独立池、扩大总连接上限或全局缩短其他业务 SQL 的超时。代价是活跃会话期间普通请求可同时占用的位置少一个（默认池上限 10 时为 9），这是控制请求容量保障，不能据此声称整体吞吐提升。

动态预留无法撤销已经满占的事务，也无法绕过相同 scope guard 的锁竞争、数据库故障、隔离未排空或单连接限制。这些情况在预算内不能完成时停止执行，而非保证健康任务在任何负载下都不会中断。首次 reserve 等待也消耗保守本地锚点；过慢的 admission 会被拒绝，不因返回成功而虚构新租期。事件循环完全阻塞时无法按墙钟准时取消，恢复运行后检查耗时并拒绝迟到成功；数据库当前时间和 fencing 仍为最终授权依据。5 秒余量用于提前请求停止，不保证 NAS worker 在 5 秒内结束；原有 IO 排空与 pins 释放顺序保持不变。

## 验证

先新增 13 项测试，在旧实现上全部失败，分别暴露控制容量缺失、单次操作无总预算、reserved 拒绝续租、最终 UPDATE 缺少过期复核、admission 未保护及 terminal 查询无界。另一个同步阻塞测试在修复前确认超预算仍返回成功，再增加显式耗时检查。

定向测试覆盖满池下的控制获取、多个作用域共用容量、单连接退化、取消后释放和唤醒；池获取/事务开始/guard/关联/UPDATE/COMMIT/归还阶段阻塞、跨阶段共用预算；不确定提交和取消抵抗 IO 的隔离排空；admission 失败、保守锚点和调度漂移、外部取消、terminal 精确匹配及 terminal 查询截止。

新增 6 项专用 MySQL 用例：四类实际行锁等待、SELECT 与 UPDATE 之间租期变更、reserved admission 在 NAS publish 前续租。专用测试库未配置时跳过，不能视为真实锁行为或部署验收。

最终扩大回归：**1011 passed、38 skipped，60.15 秒**。命令：

```bash
../../venv/bin/python -m pytest tests/unit/database tests/unit/app/conversation_storage tests/unit/routers/test_tracing_chat_history_parity.py tests/unit/routers/test_tracing_chats_api.py tests/integrated/conversation_storage/test_execution_mysql.py tests/integrated/conversation_storage/test_runner_integration_mysql.py -q
```

38 项跳过来自未配置专用库的执行/Runner MySQL 模块，包含本轮新增 6 项，均不算验收通过。本包显式文件的 pre-commit（AST、mypy、Black、flake8、pylint 等）通过；4 个运行时文件另以 Python 3.10 AST 语法模式解析通过，但不等同于 Python 3.10 运行时测试。

当前 venv 驱动仍为 9.4.0，manifest 锁定 26.7.0；未升级共享环境。上线前仍需在版本匹配的隔离环境完成真实 MySQL/NAS、连接耗尽、跨 Pod 故障与停止/结算压测；未建立业务 SLO，不提供未经测量的 P95/P99 或吞吐承诺。

本轮严格租约版本随后调整为软租约；保留部分按用户后续指令随软租约包提交，既有其他未提交文件保持原状。
