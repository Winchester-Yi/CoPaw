# CronManager 业务解耦（保留旧任务行为）

> 后续实现更新（2026-11-05）：[旧页勾选迁移与聊天页最小兼容](../brainstorms/2026-11-04-cron-selected-migration-requirements.md)已完成本地开发，操作及待行内验收内容见[迁移说明](cron_selected_migration.md)。本文的“聊天页未切换”等说明仍记录此前已提交的业务解耦阶段，不表示当前方案排除聊天兼容。

## 本轮交付

1. SWE 新任务通知直接使用 `CronNotificationSender`；旧通知仍经 CronManager 读取旧任务并调用同一发送算法。
2. 心跳、梦境执行使用 `SystemTaskRuntime`；会话裁剪使用独立 `session_cleanup`，新版维护不再以旧清理成功为前置条件。旧文件任务维护分支仍保留，失败继续计入维护结果。
3. 心跳/梦境配置先保存，再经 `system_bridge` 直接提交刷新。工作区 priority 45 的独立挂点提交新版系统刷新；旧 CronManager priority 40 仍恢复旧用户任务和启动凭据预热，不再重复提交系统刷新。
4. 招呼任务进度改查 Scheduler；技能绑定检查及任务模型引用改查 Scheduler，新版年份和工作日表达式使用扩展模型解析，不修改旧模型。

## 新增接口

`GET /api/scheduler/cron/progress?date=2026-11-04&limit=200`

Header 沿用现有网关身份：`X-User-Id`、`X-Source-Id` 必填；`X-Tenant-Id` 缺省为用户，`X-Agent-Id` 缺省为 default。查询权限与新版列表相同，不能跨 tenant/source/agent 读取。

- `date` 是北京时间业务日；排程使用任务时区，工作日和运行日期约束按北京时间判断。
- 返回 `items`、`next_cursor`、`date`、`timezone`；每个有排程且未停用的任务一条摘要，`scheduled_time` 是当天首个符合条件的计划时间。
- `status_basis=latest_task_state`：状态来源是任务最近状态，不是逐次执行历史。不能把一条任务摘要解读成当天每一轮执行均已完成；批提前执行的完整归属仍应看批看板/执行追踪。
- 每项含 job_id、名称、完整任务身份、状态文案、时间文案、chat/session 原绑定及 `calendar_source`。不返回任务正文、凭据或私有调度元数据。
- 按任务分页；某页全部因假期/停用被过滤时，items 可以为空但 next_cursor 仍非空。调用方必须继续翻页。游标绑定日期及原仓库身份，修改日期要重新开始。
- 六段尾部年份、W/LW、补班工作日、非工作日开关、可执行日期首尾及批成员原始业务 cron 均由 Scheduler 计算；不额外生成未来三次预览。
- 数据库不可用返回 503；不会回退旧 jobs.json 或内存。日历服务仍沿用既有缓存/降级策略，并暴露 calendar_source。
- SWE 客户端严格校验身份、管理归属、分页和日期，不将请求失败当成空任务列表。招呼查询失败不会发送“无任务”的成功卡片；技能检查返回自检失败。

## 过渡边界（本轮不删除）

| 保留项 | 原因 / 后续移除条件 |
|---|---|
| Workspace 的 CronManager 与 JsonJobRepository 挂载 | 旧任务仍通过它执行和管理；入口切换完成后再断开 |
| SWE 旧 cron/api.py、batch_operations.py、internal cron callback | 旧 HTTP 合同、文件读写、启停、分发和回调保持现状 |
| CronManager 的兼容业务方法 | 旧调用方继续使用原方法；共享纯业务实现，不把管理逻辑复制到新服务 |
| notification_worker 的 legacy 分支 | 旧通知仍读取原任务文件；新通知只读数据库冻结定义 |
| Source 清理的 legacy 分支 | 原清理仍执行；可独立退役，不应阻止新版清理 |
| migration_source.capture_loaded_states | 保留存量内存快照，未做接管或改 ID/会话绑定 |
| AgentConfigWatcher 的旧注入、Monitor 的旧预热入口 | 仍属旧入口范围，不在本轮停用 |
| SWE 旧对外列表、聊天页新旧双查、旧管理页 | 均未切换；切入口是后续工作 |
| swe_cron_jobs 同步投影、执行历史及通知记录写入 | 不变，后续由对应负责人处理 |
| Scheduler 旧调度运行时与共享模型池 | 不变，另一个实施环节 |
| 财富规划旧后端、Workflow 配置路由 | 不迁移、不删除；最终清理时需保留/拆出仍用的能力 |

## 部署与验证

本轮没有新增表、DDL、必填配置或前端构建。先部署 Scheduler，再部署 SWE；SWE 先部署会因进度接口未上线而查询失败，不会静默使用旧任务。

预发布负责人在上线后首轮业务触发及一次服务重启后核对：

1. 用 Postman 调用 progress：普通任务、年份、W/LW、日期范围、补班日；核对 IDs 和会话绑定不变，跨来源读不到其他任务。
2. 招呼任务进度能出现新版任务；临时不可用时日志出现 `zhaohu task progress query failed`，不展示空任务成功卡片。
3. 技能绑定检查能识别只有新表定义的任务；模型连接与凭据检查仍由 SWE 执行。
4. 新任务成功通知仍使用原渠道、名称、收件人和跳转链接；任务状态/未读/执行历史不因发送服务提取改变。
5. 新系统心跳/梦境、会话清理可正常执行；配置保存后出现原有 scheduler_sync 结果；工作区启动只提交一次系统刷新。
6. 用一条旧任务回归列表、详情、启停、手动运行、已读和通知；检查旧平台恢复及旧表同步仍正常。

失败信号：新版通知持续失败、系统刷新无记录、progress 持续 503、技能任务查询失败、旧接口回归差异。查询日志关键字 `Managed task progress query`、`Failed to submit workspace system cron refresh`；与原通知失败记录、系统执行表一起定位。若出现旧接口行为回归，回退本轮 SWE/Scheduler 代码；无本轮数据库变更需要回滚。

## 验证范围

使用测试先行、真实 SQLite 查询与 ASGI HTTP 链路验证。按工作区约定由主线程自检，不宣称独立审查。真实行内网关、外部日历/平台和招呼/W+ 网络发送需要预发布验证。

2026-11-04 本地验证记录：

- Scheduler 全部单测及 SWE 新查询客户端：1077 passed（含 SQLite/FastAPI 既有弃用警告）。
- SWE 新旧 cron、系统任务、技能检查、招呼、工作区及相关路由扩大回归：841 passed、1 failed。唯一失败为 `test_router_is_registered_without_bootstrapping_runtime`：基线 `9fe411881` 的 SWE 路由清单未注册旧 migration_api，而测试要求存在；路由文件和该测试均与 HEAD 相同，本轮不恢复旧迁移入口。
- 最终查询相关回归：54 passed。
- pre-commit 全部适用检查通过，包括复杂度上限 15、mypy、Black、Flake8、Pylint；git diff --check 通过。
- 旧取消测试曾因本机模型初始化约 6 秒、测试固定 50ms 取消而失败；用 HEAD 版 CronManager（执行方法起始行 3564）重现后，仅将该测试改为隔离模型/凭据依赖并等待真实输出事件，原业务代码和断言不变。修改后的相关 53 项及扩大回归均通过该用例。
- AST 核对旧 CronManager 的 137 个方法：132 个方法体保持相同；其余为 initialize 的系统刷新挂点迁出、会话锁/清理的委托和 every_to_cron 别名。旧管理/回调/迁移快照/旧表投影及结果写入文件没有差异。
- 自检覆盖旧接口合同、身份隔离、日历/日期、通知与清理错误路径、启动生命周期；不是独立审查，也不是行内验收。
