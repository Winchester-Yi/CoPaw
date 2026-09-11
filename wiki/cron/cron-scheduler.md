# Cron 存储、调度与入口

本文说明 cron 任务如何落盘、普通/批调度物理 timer 如何同步外部调度平台、创建更新时写入哪些派生字段，以及 Console、CLI、外部回调分别如何进入同一套生命周期。

返回 [Cron 定时任务模块索引](README.md)。

## 存储与初始化

每个 Workspace 都有自己的 `jobs.json`：

```text
<workspace_dir>/jobs.json
```

创建入口在 `src/swe/app/workspace/workspace.py`：

- `Workspace._register_services()` 注册 `CronManager`。
- `CronManager` 的 repo 是 `JsonJobRepository(str(ws.workspace_dir / "jobs.json"))`。
- `CronManager.initialize()` 会加载系统任务 ID、恢复外部任务 ID、启动 auth token 预热循环，并注册 heartbeat / dream。

外部调度平台的系统任务 ID 单独保存在：

```text
<workspace_dir>/system_jobs.json
```

`JsonJobRepository` 使用单文件 JSON 存储，保存时先写临时文件再替换。它没有跨进程文件锁，所以多实例环境下不应把它当成强一致共享数据库。

## 调度平台适配

本地或测试环境没有 `SWE_CRON_SCHEDULER_BASE_URL` 时，使用 `NoopSchedulerAdapter`。这种模式只保存任务、支持手动运行和展示 next run，不会真正注册外部到点触发。

配置了 `SWE_CRON_SCHEDULER_BASE_URL` 时，`Workspace._build_scheduler_adapter()` 会创建 `RealSchedulerAdapter`。

常用环境变量：

| 环境变量 | 用途 |
| --- | --- |
| `SWE_CRON_SCHEDULER_BASE_URL` | 外部调度平台地址 |
| `SWE_CRON_SCHEDULER_JOB_GROUP` | 外部平台 jobGroup |
| `SWE_CRON_SCHEDULER_AUTHOR` | 外部平台 author |
| `SWE_CRON_SCHEDULER_ALARM_EMAIL` | 外部平台 alarmEmail |
| `SWE_CRON_SCHEDULER_CLIENT_NO` | 外部平台 clientNo |
| `SWE_CRON_SCHEDULER_CLIENT_KEY` | 外部平台 clientKey |
| `SWE_CRON_SCHEDULER_CLIENT_REMARK` | 外部平台 clientRemark |
| `SWE_SERVER_DOMAIN` | 拼接回调地址，默认 `http://localhost:8000` |
| `SWE_INTERNAL_TOKEN` | 其他受保护的内部接口使用；Cron 回调不读取或校验此 token |
| `SWE_CRON_DISPATCH_INTENTS_ENABLED` | 是否允许广播源任务切换到独立 Scheduler 批调度 |
| `SWE_SCHEDULER_API_URL` | 批调度 Scheduler API 基址，默认 `http://localhost:9100/api` |

外部平台注册时，`RealSchedulerAdapter` 会调用：

| 操作 | 外部接口 |
| --- | --- |
| 新增任务 | `/job-admin/v2/add-job` |
| 更新任务 | `/job-admin/v2/update-job` |
| 启停任务 | `/job-admin/v2/update-job-run-states` |

写入外部平台的 `jobParam` 是 base64 JSON，包含：

```json
{
  "tenant_id": "<tenant_id>",
  "source_id": "<source_id>",
  "scopeId": "<tenant_id>-<source_id>",
  "agent_id": "<agent_id>",
  "task_type": "job|heartbeat|dream",
  "job_id": "<job_id>",
  "fromId": "<tenant_id>"
}
```

普通任务到点后，外部平台把 `jobParam` 原样带回 `/api/internal/cron/callback`。SWE 用它恢复租户、来源、Agent 和任务类型。

批调度是另一条物理 timer 合同：源任务切换后，普通 timer 暂停，外部平台改为回调 `/api/scheduler/cron/callback`。独立 Scheduler 创建父任务与广播子任务 intents，再回调各自所属 SWE。详见 [Cron 批调度与独立 Scheduler](cron-batch-dispatch.md)。

## 创建与更新流程

创建任务走：

```http
POST /api/cron/jobs
```

更新任务走：

```http
PUT /api/cron/jobs/{job_id}
```

核心步骤：

1. API 层调用 `_inject_request_tenant()`，把 `request.state.tenant_id/source_id/scope_id/bbk_id/user_name` 写入任务。
2. API 层调用 `_inject_creator_user()`，保存 `meta.creator_user_id`。
3. 如果是 `agent` 且带 `model_slot`，API 层调用 `_validate_cron_job_model_slot()` 校验 provider 和 model 是否存在。
4. `CronManager.create_or_replace_job()` 调用 `_ensure_task_binding()`，为任务绑定或复用 task chat。
5. `CronManager._sync_job_to_external_scheduler()` 先注册或更新外部调度平台，并把 `external_job_id` 写入 `meta`。
6. `JsonJobRepository` 写入 `jobs.json`。
7. `CronManager.refresh_next_run_at()` 用 `croniter` 计算后续 3 次运行时间，仅用于界面展示。
8. `MonitorSyncClient.sync_job()` 异步同步任务定义到 Monitor。

任务卡片相关的 `meta` 字段主要有：

| meta 字段 | 含义 |
| --- | --- |
| `creator_user_id` | 谁创建或拥有这个任务卡片 |
| `task_session_id` | 任务 chat 的 session ID，默认 `cron-task:{job_id}` |
| `task_chat_id` | chat 存储里的 chat ID |
| `task_has_scheduled_result` | 是否已有定时执行结果 |
| `task_last_scheduled_preview` | 最近一次结果预览，当前只保存很短片段 |
| `task_unread_execution_count` | 未读成功执行次数 |
| `task_last_scheduled_run_at` | 最近一次定时执行完成时间 |
| `pause_reason` | `manual` 或 `auto_unread_threshold` |
| `auto_paused_at` | 自动暂停时间 |
| `external_job_id` | 外部调度平台任务 ID |
| `broadcast_dispatch_intents_enabled` | 是否由独立 Scheduler intents 管理自动执行 |
| `batch_dispatch_external_job_id` | 批调度物理 timer ID，关闭模式后保留以便复用 |
| `batch_dispatch_cron` | 提前触发后注册给物理 timer 的 cron |
| `batch_dispatch_offset_minutes` | 相对父任务原 cron 的提前分钟数 |
| `batch_dispatch_cron_warning` | 无法安全平移 cron 时的 fallback 原因 |

## Console 与 CLI

Console 前端接口集中在 `console/src/api/modules/cronjob.ts`：

| 前端方法 | 后端接口 |
| --- | --- |
| `listCronJobs()` | `GET /api/cron/jobs` |
| `createCronJob()` | `POST /api/cron/jobs` |
| `replaceCronJob()` | `PUT /api/cron/jobs/{job_id}` |
| `deleteCronJob()` | `DELETE /api/cron/jobs/{job_id}` |
| `pauseCronJob()` | `POST /api/cron/jobs/{job_id}/pause` |
| `resumeCronJob()` | `POST /api/cron/jobs/{job_id}/resume` |
| `runCronJob()` | `POST /api/cron/jobs/{job_id}/run` |
| `markTaskRead()` | `POST /api/cron/jobs/{job_id}/task/mark-read` |
| `broadcastCronJob()` | `POST /api/cron/jobs/{job_id}/broadcast` |
| `getCurrentBroadcastTask()` | `GET /api/cron/jobs/{job_id}/broadcast/tasks/current` |
| `getBroadcastTask()` | `GET /api/cron/jobs/{job_id}/broadcast/tasks/{task_id}` |
| `enableBatchDispatch()` | `POST /api/cron/jobs/{job_id}/batch-dispatch/enable` |
| `disableBatchDispatch()` | `POST /api/cron/jobs/{job_id}/batch-dispatch/disable` |

`console/src/pages/Control/CronJobs/helpers.ts` 负责把表单上的 daily / weekly / custom cron、执行模型选择项和 `CronJobSpec` 互相转换。

CLI 入口是 `src/swe/cli/cron_cmd.py`。它会通过请求头传：

```http
X-Agent-Id: <agent_id>
X-Tenant-Id: <tenant_id>
X-Source-Id: <source_id>
```

如果没有显式 `--source-id`，CLI 会优先使用当前 source context，而不是随便发一个 `default` source。

CLI 支持通过：

```text
--model-provider <provider_id>
--model <model>
```

生成和 Console 一致的 `model_slot`。

## 普通外部回调与自动执行

外部调度平台统一回调：

```http
POST /api/internal/cron/callback
```

`src/swe/app/routers/internal.py` 的 `internal_cron_callback()` 会：

1. 不校验 Scheduler/internal token；部署需保证入口仅供可信内网调用。
2. 优先读取 `jobParam` 或 `job_param`，按 base64 JSON 解码。
3. 如果没有 `jobParam`，直接读取 body 顶层字段。
4. 提取 `tenant_id`、`source_id`、`agent_id`、`task_type`、`job_id`。
5. 调用 `resolve_runtime_tenant_id(tenant_id, source_id)` 找到 runtime tenant。
6. 通过 `MultiAgentManager` 找到对应 workspace 的 `CronManager`。
7. 按 `task_type` 分发：
   - `heartbeat` -> `CronManager.run_heartbeat()`
   - `dream` -> `CronManager.run_dream()`
   - 其他 -> `CronManager.run_job(job_id, is_manual=False, source_id=source_id)`

这里的 `source_id` 很重要。旧任务可能没有持久化 source，`CronManager._with_execution_source_identity()` 会在执行前用回调里的 source 补齐 legacy job 的执行身份。

## 批调度回调边界

批调度源任务和子任务的自动执行必须来自 Scheduler dispatch callback。`internal_cron_callback()` 会校验 dispatch 身份，并把 `dispatch_intent_id`、`dispatch_batch_id`、`dispatch_attempt`、模型作用域和父计划时间传给 `CronManager.run_job()`。

如果一个已经由批调度管理的父任务或子任务收到没有 dispatch 身份的旧普通 timer 回调，SWE 会跳过执行，避免模式切换期间重复触发。手动运行仍直接走 `run_job(is_manual=True)`，不经过 Scheduler intent。

模式切换规则：

- 启用批调度：暂停源任务普通 timer，创建或更新 `[批调度]` 物理 timer；广播子任务的普通 timer 随后台同步暂停。
- 关闭批调度：暂停批调度物理 timer，恢复源任务普通 timer，并后台恢复广播子任务 timer。
- 物理 timer 提前 1-24 小时触发；不支持平移的 cron 原样注册并记录 warning。
- `batch_dispatch_external_job_id` 关闭后不删除，以便下一次开启时更新并复用同一外部任务。

独立 Scheduler 自己不会扫描 `swe_cron_jobs` 判断哪些父任务到点；`enqueue_due_parent_intents_once()` 是兼容保留的 no-op，批次唯一触发源是外部物理 timer callback。
