# Cron 定时任务模块索引

本文档面向 SWE / CoPaw 的使用者、接入方和维护者，说明 cron 模块在当前 `v1.0.0` 代码里的真实边界、运行链路、关键接口、数据字段、批调度、通知机制和排查入口。

本轮按 `7a9aac4fc fix(cron): add need_notification` 核对，并以 `src/swe/app/crons/`、`scheduler/`、`monitor/`、Console 和相关测试为事实来源。

## 新人推荐阅读顺序

1. [Cron 总览与代码入口](cron-overview.md)：先理解普通调度、批调度和手动运行的边界。
2. [Cron 存储、调度与入口](cron-scheduler.md)：理解 `jobs.json`、外部平台和回调入口。
3. [Cron 批调度与独立 Scheduler](cron-batch-dispatch.md)：理解批次、intent、模型作用域容量、回执和重试。
4. [Cron 执行上下文](cron-execution-context.md)：理解 tenant、source、model、auth 和 dispatch meta。
5. [Cron Monitor 与通知](cron-monitor-notification.md) 与 [Cron 通知延迟](cron-notification-delay.md)：理解执行记录、批次看板、通知领取和 due time。
6. [Cron 广播与系统任务](cron-broadcast-system.md) 与 [Cron 分发子任务管理](cron-distribution-management.md)：理解异步广播、模式同步、归档维护和子任务管理。
7. [Cron 排查与提交脉络](cron-troubleshooting-history.md)：按症状定位源码与提交。
8. [Workflow 类型定时任务](cron-workflow.md)：配置技能关联的同步接口、身份映射、结果渲染与批调度模型。
9. [Workflow 绑定配置操作手册](workflow-binding-guide.md)：按请求头和 JSON 示例发布、验证技能到接口的绑定。

## 文档目录

| 文档 | 适合回答的问题 |
| --- | --- |
| [Cron 总览与代码入口](cron-overview.md) | cron 覆盖哪些场景、三条执行链路是什么、核心模型与源码在哪 |
| [Cron 存储、调度与入口](cron-scheduler.md) | 任务如何落盘、普通/批调度 timer 如何注册、Console/CLI/回调如何进入 |
| [Cron 批调度与独立 Scheduler](cron-batch-dispatch.md) | 批次与 intent 如何创建、排序、限流、回执、重试和查询 |
| [Cron 执行上下文](cron-execution-context.md) | 单次执行如何恢复 source、model、auth、B3 和 dispatch 身份 |
| [Cron Monitor 与通知](cron-monitor-notification.md) | execution、批次看板、外部子任务统计、未读、通知领取与推送 |
| [Cron 通知延迟](cron-notification-delay.md) | 普通、广播和批调度执行怎样计算 `notification_due_at` |
| [Cron 广播与系统任务](cron-broadcast-system.md) | 异步广播、批调度模式、source 级清理/归档维护、heartbeat/dream |
| [Cron 分发子任务管理](cron-distribution-management.md) | 快照刷新、分发子任务反查、批量删除/重跑、模式同步 |
| [Cron 排查与提交脉络](cron-troubleshooting-history.md) | 没有触发、intent 卡住、通知缺失、同步不完整等问题怎么查 |
| [Workflow 类型定时任务](cron-workflow.md) | workflow 配置发布、运行身份、版本冻结及记录链路 |
| [Workflow 绑定配置操作手册](workflow-binding-guide.md) | 调用哪个接口、提交哪些参数、如何验证绑定和排查常见错误 |

## 示例目录

可复制示例见 [Cron 示例索引](examples/README.md)：

| 示例 | 用途 |
| --- | --- |
| [API 创建 Agent 任务](examples/api-agent-daily/README.md) | 创建每日 Agent 定时任务并手动触发 |
| [CLI 创建固定文本任务](examples/cli-text-weekly/README.md) | 用 `swe cron` 管理固定文本任务 |
| [外部调度回调](examples/callback-jobparam/README.md) | 区分普通 timer 回调 SWE 与批调度 timer 回调 Scheduler |
| [批调度模式](examples/batch-dispatch/README.md) | 开关批调度、查询批次与 worker 状态 |
| [广播到多个租户](examples/broadcast-to-tenants/README.md) | 发起异步广播并轮询任务结果 |
| [Cron 授权状态](examples/cron-auth/README.md) | 写入和清理 `cron_auth.json` |
| [Monitor 与通知排查](examples/monitor-notification-debug/README.md) | 查询 execution、标记已读、定位通知未发送 |

## 先看结论

- SWE 是 cron 任务定义和执行逻辑的 owner；`CronManager` 管生命周期，`CronExecutor` 执行 agent/text，独立 workflow 引擎执行技能接口任务。
- 普通模式下，外部调度平台到点回调 SWE `/api/internal/cron/callback`。
- 批调度模式下，外部平台只触发父任务的批调度物理 timer，并回调独立 Scheduler `/api/scheduler/cron/callback`；Scheduler 创建 intents，再逐个回调任务所属 SWE。
- Monitor 保存 job/execution，并提供 cron 查询、批次看板、worker 状态和完成通知领取。
- Scheduler 的 `effective_workers` 是模型作用域容量槽位，不是操作系统进程数；它没有后台扫描所有父任务。
- `src/swe/app/crons/coordination.py` 仍未接入普通 `Workspace` / `CronManager` 装配；不要和已投入批调度链路的 Scheduler 数据库 lease 混为一谈。

## 关键源码入口

| 入口 | 文件 | 主要职责 |
| --- | --- | --- |
| Workspace 装配 | `src/swe/app/workspace/workspace.py` | 每个 Workspace 注册一个 `CronManager`，任务 repo 为 `workspace_dir / "jobs.json"` |
| SWE Cron API | `src/swe/app/crons/api.py` | CRUD、运行、异步广播、子任务、批调度模式切换 |
| 生命周期中心 | `src/swe/app/crons/manager.py` | 外部 timer、执行、任务卡片、Monitor/Scheduler 同步 |
| 单次执行 | `src/swe/app/crons/executor.py` | tenant/source/model/auth 上下文和 agent/text 执行 |
| 外部回调 | `src/swe/app/routers/internal.py` | 普通 timer、Scheduler dispatch、系统任务回调 |
| 外部调度适配 | `src/swe/app/crons/scheduler_adapter.py` | 对接外部 job-admin API |
| 独立 Scheduler API | `scheduler/src/scheduler/app/routers/cron.py` | 批调度父回调与 execution 回执 |
| Scheduler 编排 | `scheduler/src/scheduler/app/services/cron/scheduling_service.py` | intent 派发、重试、超时和容量 |
| Monitor Cron API | `monitor/src/monitor/app/routers/cron.py` | job/execution、批次详情和 worker 查询 |
| Monitor 外部 API | `monitor/src/monitor/app/routers/external.py` | 最新 execution 子任务数的网关接口 |
| Source 系统任务 | `src/swe/app/source_system_config/task_scheduler.py` | source 级清理与归档维护任务 |
| Console Cron | `console/src/pages/Control/CronJobs/` | 表单、列表、广播、模式切换和子任务管理 |

## 版本边界

- 当前 wiki 的代码基线是 `7a9aac4fc`，不是旧的 `f0ed1c9e` 快照。
- 当前实现包含独立 Scheduler 管理的批调度、模型作用域 capacity/lease、阅读热度排序、execution 回执和补位。
- 广播接口是异步任务接口；子任务列表来自带状态的分发快照，可显式刷新。
- Monitor 完成通知领取要求 execution 同时满足 `status=success`、`async_status=success`、`need_notification=1`、`notification_status=pending`。
- Source 系统任务除 task session cleanup 外，还包含 `archive_maintenance`；同一 source 各维护任务分别只有一条外部绑定。
- `skill_ids` 是任务和技能就绪度治理的关联标识，不代表 CronExecutor 会自动加载技能。
- 这里记录当前代码真实行为，不把未装配原语或已经移除的启动全量扫描描述成生产路径。
