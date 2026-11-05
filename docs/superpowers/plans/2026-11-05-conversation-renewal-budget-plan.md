# 会话续租预算实施计划

> 本文记录先前的严格租约版本。用户已选择并切换为[软租约＋代次校验](../specs/2026-11-05-conversation-soft-lease-design.md)：过期不撤权、心跳可用性失败重试；本地 25 秒截止和过期拒绝规则已移除。单次 SQL 预算、控制容量和取消隔离保留。

**Goal:** 在保持 schema、存储格式和接口兼容的前提下，使续租拥有完整预算和受池上限约束的控制容量，失败及时停止执行。

**Architecture:** 使用现有 AnyIO fail_after，在原 asyncio task 内约束操作，不将绑定事务交给子 task。ExecutionStore 的续租事务含排队、连接建立、guard/行锁、UPDATE、COMMIT、归还检查；IndexedQuery 从 reserve 尝试前的单调时间建立保守锚点，覆盖 admission、运行及结算。连接池在存在会话租约时为控制请求保留一个容量位置，所有连接及隔离资源仍受 max_connections 限制。

**Tech Stack:** Python >=3.10、asyncio、AnyIO、MySQL InnoDB、pytest。

## 设计与取舍

- 仅缩短 acquire_timeout 无法约束锁/SQL/归还；独立控制池会增加连接上限与生命周期，本次不采用。选用同池动态预留和总 deadline。
- 动态预留按进程共享池计数，有任意活跃 IndexedQuery 时启用一个位置，不按会话额外开连接。普通获取仅在已占用位置少于 max_connections-1 时准入，控制获取可用全部上限；max_connections=1 退化共享，不让普通请求永久饥饿。已有占用不抢占，隔离未排空也不释放容量。
- reserve 之前记录单调时间；新建 authority 后立即启动 renewer，先于 admission NAS publish。reserved 允许续租，但不允许模型执行；start 仍要求 admitted。长 admission 同样受保护。
- 默认 L=30 秒：调度间隔 L/3=10 秒；每次 renew 总预算 L/3；周期硬截止为上次成功尝试开始时间 +5L/6=25 秒，余下 L/6 为停止/调度安全余量。成功后使用尝试开始时间作新锚点，不用返回时间，避免提交和归还耗时制造虚假的剩余租期。唤醒延迟消耗原预算，不重新获得完整等待时间。
- renew 自身超时或冲突时，在周期剩余时间内只查询本请求精确 terminal publication；已提交 terminal 不取消 reader/NAS 清理。硬截止到达则 fail closed，无提交结果推测、无无限重试。外部 CancelledError 原样传播；未知提交不获得本地成功授权。
- 最终续租 UPDATE 增加数据库当前租期、generation、owner、active turn 条件，零 affected rows 拒绝，防止前置读取后等待导致过期续命。原 scope/backup fence、Session/Chat/Turn 关联及锁顺序保留。
- AnyIO 超时只中断请求等待；数据库 pool 的 retained dispose/quarantine 继续清理，不绕过 socket/IO 排空校验。不能承诺阻塞 event loop 时按墙钟准时取消，恢复调度后必须检测过期预算。

## 实施步骤

- [x] 1. 在数据库与 runner/execution 测试中先复现：共享池占满、慢事务阶段、reserved 状态、UPDATE 最终过期、admission 期间续租、调度漂移与 terminal 查询超时。验证失败原因与缺失行为一致。
- [x] 2. ConnectionPool/DatabaseConnection 增加动态控制容量作用域和 task-local 控制标记；验证总连接数、取消归还、作用域退出及单连接退化。
- [x] 3. ExecutionStore 增加续租完整预算、控制获取和最终条件更新；IndexedQuery 提前启动 renewer，保守绝对调度及周期截止，保留精确 terminal 竞态处理。
- [x] 4. 运行相关数据库/执行/runner 回归，扩大 storage 回归；仅对本包文件运行 pre-commit，更新入口索引和实现报告。真实驱动、MySQL 锁和 NAS 验收未配置时明确未验证。

本计划完成后，用户选择软租约并要求提交；保留的 SQL 预算与控制容量随软租约包一起提交。
