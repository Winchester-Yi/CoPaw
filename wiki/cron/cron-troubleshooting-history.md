# Cron 排查与提交脉络

本文汇总 cron 常见问题的第一排查入口、相关提交脉络和维护建议。排查时建议先按问题类型定位到对应章节，再回到源码确认当前实现。

返回 [Cron 定时任务模块索引](README.md)。

## 常见排查入口

### 创建后没有到点执行

优先检查：

1. 是否配置了 `SWE_CRON_SCHEDULER_BASE_URL`。未配置时是 Noop，不会外部到点触发。
2. `jobs.json` 里 job 的 `meta.external_job_id` 是否存在。
3. 外部平台的 `jobParam` 是否包含正确 `tenant_id/source_id/agent_id/task_type/job_id`。
4. `SWE_SERVER_DOMAIN` 拼出的回调地址是否外部平台可访问。
5. 网关／网络是否允许调度平台访问 `/api/internal/cron/callback`；该入口不校验内部 token。

如果任务已经切到批调度，不要继续按普通 timer 排查：

1. SWE 与 Scheduler 的 `SWE_CRON_DISPATCH_INTENTS_ENABLED` 是否都为 true。
2. 父任务是否有 `batch_dispatch_external_job_id`，外部平台回调是否指向 `/api/scheduler/cron/callback`。
3. Scheduler 是否创建 batch/intents，模型作用域是否有 capacity。
4. 父/子任务的旧普通 timer 是否已暂停；SWE 会跳过批调度任务的非 dispatch 自动回调。

### 批调度 batch 已创建但 intent 不执行

优先检查：

1. `swe_cron_dispatch_intents` 的 status、attempt、due_at 和最近 event。
2. source/provider/model 是否组成了预期作用域，`swe_cron_dispatch_worker_capacity.effective_workers` 是否大于 0。
3. `swe_cron_dispatch_scope_leases` 是否存在未过期 lease；`effective_workers` 是容量槽位，不是进程数。
4. Scheduler 回调的任务级 SWE domain 是否可达，可信内网访问规则是否允许回调；不再依赖 Scheduler token。
5. SWE execution meta 是否带完整 intent/batch/attempt，`/api/scheduler/cron/execution` 回执是否成功。
6. intent 是否超过 `SCHEDULER_CRON_DISPATCHED_STALE_SECONDS`，或已达到默认 3 次尝试上限。

不要等待 Scheduler 后台扫描父任务：批次只能由外部批调度物理 timer callback 创建。

### 回调到了但找不到任务

优先检查：

1. `jobParam.source_id` 是否缺失或错误。
2. `resolve_runtime_tenant_id(tenant_id, source_id)` 对应的 workspace 是否存在。
3. 该 workspace 的 `jobs.json` 是否包含对应 `job_id`。
4. 旧任务是否缺少 `scope_id/source_id`，需要依赖回调 source 补齐。

### 任务执行报 cron auth 过期

优先检查：

1. 当前 runtime tenant 下是否存在 `cron_auth.json`。
2. `user_info_expires_at` 是否过期。
3. 是否通过 `/api/auth/cron-auth` 重新提交包含 `com.cmb.dw.rtl.sso.token` 的 cookie。
4. 任务执行时 `scope_id` 是否和写入授权时的 workspace 运行 scope 一致。

### 成功执行但没有完成通知

优先检查：

1. execution 是否同时满足 `status=success`、`async_status=success`、`need_notification=1`、`notification_status=pending`。
2. `notification_due_at` 是否已经到期；批调度执行要核对 `cron_dispatch.parent_scheduled_fire_at`。
3. SWE 进程的 `CronNotificationWorker` 是否启动。
4. `SWE_CRON_NOTIFICATION_SOURCE_IDS` 是否过滤掉了该 job 的 `source_id`。
5. `monitor/src/monitor/app/services/cron/notification_service.py` 领取 SQL 是否能查到这条 execution。
6. `CronManager.send_task_success_notification()` 是否能找到 job、task chat 和 zhaohu 通道。

### 广播 POST 成功但结果为空

广播现在是异步任务。首次 POST 返回的 `task_id/status/progress/results/reused` 只代表任务已创建或复用，不代表所有目标租户已完成。

优先轮询：

- `GET /api/cron/jobs/{job_id}/broadcast/tasks/current`
- `GET /api/cron/jobs/{job_id}/broadcast/tasks/{task_id}`

如果部分租户缺失，再看分发快照的 `failed_tenants/failure_summary`，必要时调用 `POST /api/cron/jobs/{job_id}/broadcast/children/refresh`。请求和历史快照里的已知租户失败属于严格失败；额外发现租户失败只形成 warning。

### Source 归档维护没有执行

优先检查 source 系统配置里的 `archive_maintenance.enabled/cron`、`swe_source_system_task_binding` 中 `task_type=archive_maintenance` 的外部任务 ID，以及 callback 是否由 `SourceSystemTaskScheduler` 处理。单次执行还可能因为 workspace/file 限额或 `timeout_seconds` 正常提前结束，要看汇总里的 `timed_out` 和 errors。

### 手动运行后通知时间不对

手动运行走 `is_manual=True`。当前代码要求手动运行不套广播的 `broadcast_offset_minutes` 通知延迟。相关修复是 `9f4362f1 fix(cron): skip manual broadcast notification delay`。

如果再次出现手动运行继承自动广播延迟，优先检查 `CronManager._sync_execution_to_monitor()` 里 `not is_manual` 的判断。

### 工作日 cron 到外部平台后变成每天执行

内部 cron 的 day-of-week 会先归一化为英文缩写，外部平台需要数字格式。优先检查：

- `src/swe/app/crons/models.py` 的 `_crontab_dow_to_name()`。
- `src/swe/app/crons/scheduler_adapter.py` 的 `_normalize_scheduler_dow()`。
- 相关提交：`cc8c5863 fix(cron): preserve weekday schedule for external scheduler`、`80d3a315 fix(cron): convert weekdays for external scheduler`。

### 结果出来了但状态是 cancelled

优先检查：

- `src/swe/app/crons/executor.py` 的 `AgentStreamState`。
- `_has_agent_completed_output()` 是否正确识别 completed message。
- `_end_trace_on_success()` 是否在取消期间成功完成。
- `src/swe/app/crons/manager.py` 的 `_handle_cancelled_after_success()`。

相关提交：`a258e8e4 修复定时任务cancelled异常`、`79445bc6 解决定时任务cancelled问题`。

### Agent 执行链式 swe cron 命令时参数注入到错误位置

Agent 通过 shell 工具执行 `echo ready && swe cron list` 或 `swe cron list && echo done` 时，shell 拦截器需要只给真正的 `swe cron` 命令段注入 `--tenant-id`、`--source-id`，不能把参数追加到整条命令末尾。

优先检查：

- `src/swe/agents/tools/shell_interceptor.py` 的 `_split_by_shell_and()`。
- `_intercept_command_segment()` 是否只处理单个命令段。
- `tests/unit/agents/tools/test_shell_interceptor.py` 里的链式命令回归用例。

相关提交：`f0ed1c9e fix(cron): handle chained swe cron commands`。

## 相关提交脉络

下面只列和当前 wiki 内容直接相关的提交，不是完整历史。

| 提交 | 日期 | 关注点 |
| --- | --- | --- |
| `c9f87bcb` | 2026-04-30 | 增加 text 类型 cron 任务 |
| `7b93cc31` | 2026-05-09 | 增加 `SchedulerAdapter` 与 `NoopSchedulerAdapter` |
| `1f7a2391` | 2026-05-09 | 增加 `RealSchedulerAdapter`，接入外部调度平台 |
| `3f1620e5` | 2026-05-09 | 用 `jobParam` 缩短回调 URL，并把回调上下文编码进参数 |
| `28da199e` | 2026-05-11 | 收敛外部调度集成，处理 cron 格式、生命周期、持久化 |
| `16e0de08` | 2026-05-11 | 启动时自动迁移旧任务到外部调度平台 |
| `ffc2d049` | 2026-05-14 | 将定时任务 trace_id 和结果写入数据库 |
| `2d5711ec` | 2026-05-17 | 增加 source-scoped runtime isolation |
| `bc4af234` | 2026-05-21 | 支持 scope-aware cron 广播与刷新 |
| `573a3582` | 2026-05-25 | 增加任务完成通知队列 |
| `967c1c47` | 2026-05-25 | cron Monitor 查询从 query param 改为 `X-Source-Id` header |
| `cc8c5863` | 2026-05-25 | 保留外部调度平台的 weekday schedule |
| `450b8136` | 2026-05-28 | 增加 cron `model_slot` 合约校验与 ADR |
| `ea17fb89` | 2026-05-28 | 执行时绑定 cron `model_slot` |
| `5c3d2c56` | 2026-05-29 | 定时运行时绑定 Source System Configuration |
| `1a0039ec` | 2026-06-01 | 外部回调触发的 scheduled run 也绑定 source config |
| `f776f648` | 2026-06-02 | CLI 增加 cron 模型参数，复用 `model_slot` |
| `06b202b7` | 2026-06-03 | 通知领取按 source 范围过滤，并补充 playbook |
| `80d3a315` | 2026-06-05 | 外部调度平台 weekday 数字转换修复 |
| `5370c18c` | 2026-06-05 | 未读自动暂停改为 source 可配置 |
| `f24ad72a` | 2026-06-08 | 广播时跳过重复 child job |
| `0732cdea` | 2026-06-09 | 降低广播 API 复杂度 |
| `51febe0a` | 2026-06-10 | 广播时持久化目标租户 `tenant_name`、`bbk_id` 等展示身份 |
| `f0ed1c9e` | 2026-06-10 | shell 拦截器支持链式 `swe cron` 命令，只对命中段注入租户参数 |
| `67748c7e` | 2026-06-16 | 广播改为异步后台任务并提供任务状态查询 |
| `dbdc146f` | 2026-06-27 | 增加独立 Scheduler 管理的批调度与 dispatch intents |
| `0cc7d0f7` | 2026-07-01 | 按 source/provider/model 作用域协调派发 |
| `d5db8d7f` | 2026-07-02 | 阅读速度纳入批调度优先级 |
| `7428ed11` | 2026-07-03 | 增加 source 级 archive maintenance 调度 |
| `063f8a7c` | 2026-07-07 | 增加网关侧最新 cron execution 子任务数 API |
| `4136114b` | 2026-07-13 | 稳定广播子任务发现和同步 |
| `38544f7f` | 2026-07-14 | 只有完整 dispatch 身份的 execution 才路由 Scheduler |
| `7a9aac4f` | 2026-07-16 | 通知领取增加 `need_notification` 业务门控 |

## 维护建议

改 cron 相关代码时，建议按下面顺序自查：

1. 先判断改动属于任务定义、外部调度、执行上下文、Monitor 同步、通知、广播、授权还是 Console/CLI。
2. 如果改动影响 `CronJobSpec` 字段，必须同步 Console types、Monitor sync request 和测试 fixture。
3. 如果改动影响 source/tenant/scope，必须同时看 `internal_cron_callback()`、`CronManager._with_execution_source_identity()` 和 `CronExecutor._prepare_execution_context()`。
4. 如果改动影响模型选择，必须保持 `model_slot` 只通过上下文覆盖，不修改租户默认模型。
5. 如果改动影响成功/取消状态，必须同时检查 trace 结束、Monitor execution status 和任务卡片 meta。
6. 如果改动影响通知，必须同时检查 SWE `MonitorSyncClient`、SWE `CronNotificationWorker` 和 Monitor `CronNotificationService`。
7. 如果改动影响广播，必须验证异步任务 claim、快照刷新、cron 平移 fallback、模型 fallback、已有 child 刷新和模式同步。
8. 如果改动影响批调度，必须验证父 timer 唯一触发、作用域 lease/capacity、HTTP 失败、execution 回执、重试上限、stale 回收和补位。
9. 如果改动影响 source 系统任务，必须区分 task session cleanup 与 archive maintenance 的 binding、配置和执行范围。
10. 如果改动影响 Agent shell 中的 `swe cron` 命令，必须验证单条命令和 `&&` 链式命令的参数注入位置。

推荐测试入口：

```bash
& .\.venv\Scripts\python.exe -m pytest tests/unit/app/test_tenant_cron_api.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/app/test_tenant_cron_execution.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/app/test_cron_notification_worker.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/app/test_scheduler_cron_normalization.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler/test_cron_scheduling_service.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler/test_cron_dispatch_intent_service.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/monitor/test_cron_dispatch_monitor.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/monitor/test_cron_latest_subtask_count.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/app/test_source_system_task_scheduler.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/cli/test_cli_cron_tenant.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/monitor/test_cron_notification_service.py
& .\.venv\Scripts\python.exe -m pytest tests/unit/agents/tools/test_shell_interceptor.py
```
