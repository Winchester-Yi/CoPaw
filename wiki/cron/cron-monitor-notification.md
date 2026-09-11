# Cron Monitor 与通知

本文说明 cron 如何生成任务卡片、维护未读状态、把 job / execution 同步给 Monitor，以及如何通过后台 worker 推送任务完成通知。

返回 [Cron 定时任务模块索引](README.md)。

## 任务卡片、未读与自动暂停

当任务带 `creator_user_id` 且 workspace 有 `chat_manager` 时，`CronManager._ensure_task_binding()` 会为任务创建或复用一个 task chat。

成功执行后，`CronManager._record_task_execution_success()` 会：

- 对 text 任务，把固定文本写入 task session 的 `task_messages`。
- 对 agent 任务，从 session memory 中提取最近助手输出作为预览。
- 更新 `task_has_scheduled_result`。
- 更新 `task_last_scheduled_preview`。
- `task_unread_execution_count += 1`。
- 更新 `task_last_scheduled_run_at`。

自动暂停由 source system config 决定，入口是：

```text
resolve_cron_unread_auto_pause_config(get_current_source_system_config())
```

如果配置启用，并且未读次数达到阈值：

- job 的 `enabled` 会被改为 `False`。
- `pause_reason = "auto_unread_threshold"`。
- `auto_paused_at` 和 `unread_count_at_pause` 写入 meta。
- 如果有外部调度平台 ID，会暂停外部任务。
- 更新后的 job 会同步到 Monitor。

手动恢复任务时，`resume_job()` 会同步 Monitor job，并把历史未读执行记录标为已读。

## 完成通知

完成通知分两阶段。

第一阶段，执行结束时写 Monitor：

- 普通执行由 `MonitorSyncClient.record_execution()` 同步 execution；带完整 dispatch 身份的批调度执行先同步独立 Scheduler，再由共享 execution 数据进入 Monitor 查询面。
- 只有调用方明确写入 `need_notification=1` 的成功异步 execution 才需要通知。
- 手动执行成功默认 `is_read = true`。
- 自动执行成功默认产生 `notification_status = "pending"`。
- 广播任务如果 `broadcast_notification_policy == "original_schedule"`，会按原始任务时间计算通知 due time；手动运行不会套这个延迟。

第二阶段，SWE 后台 worker 领取并推送：

- 应用启动时 `_app.py` 创建 `CronNotificationWorker` 并启动。
- worker 调 `MonitorSyncClient.claim_due_notifications()`。
- Monitor 侧 `CronNotificationService` 按 `notification_due_at, id` 升序选择符合条件的 due execution，用 `FOR UPDATE SKIP LOCKED` 原子领取；正在被其他 worker 持有且未过期的通知锁会被过滤，数据库行锁会被跳过。
- worker 解析 `tenant_id/source_id`，用 `resolve_runtime_tenant_id()` 找到 workspace。
- 调 `CronManager.send_task_success_notification(job_id)`。
- 最终通过 `zhaohu` channel 推送“定时任务已完成”消息。

同一批已领取记录按每个 worker 的并发上限处理，名额覆盖 workspace 获取、通知发送和 Monitor 状态回写。某条处理完成后，排队记录立即接手空闲名额；批内完成顺序不保证。单条处理或失败状态回写抛出异常时，不中断其他记录；worker 停止时会取消并等待在途及排队处理结束。

非空批次处理结束且没有报告处理异常时，worker 立即领取下一批，不要求上一批领满。空队列、领取异常或批内处理异常时，才进入可被停止操作打断的等待；处理失败的记录不会因连续领取而在同一 worker 上立即耗尽重试次数。

通知 worker 相关环境变量：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SWE_CRON_NOTIFICATION_SCAN_SECONDS` | 300 | 空队列或异常后的等待间隔，代码限制在 300 到 600 秒；正常处理非空批次后立即继续领取 |
| `SWE_CRON_NOTIFICATION_BATCH_SIZE` | 20 | 每次领取数量，限制在 1 到 100 |
| `SWE_CRON_NOTIFICATION_CONCURRENCY` | 5 | 每个 worker 同时处理的通知数量，限制在 1 到 20；设为 1 恢复串行处理 |
| `SWE_CRON_NOTIFICATION_MAX_ATTEMPTS` | 3 | 通知失败最大重试次数 |
| `SWE_CRON_NOTIFICATION_SOURCE_IDS` | 空 | 当前 SWE 实例允许领取通知的 source 列表 |

多实例部署时，如果不同 SWE 实例负责不同 source，必须配置 `SWE_CRON_NOTIFICATION_SOURCE_IDS`。Monitor 领取 SQL 会只领取这些 source 的记录，同时允许 `source_id` 为空的 legacy 记录被任意实例竞争。

并发上限在 worker 初始化时读取，调整后需重启对应 SWE 实例。多实例的并发量会叠加，领取顺序不等于并发发送的完成顺序。领取锁仍按原有 600 秒过期规则处理，没有续租；整批耗时过长时仍可能发生过期记录被再次领取，不能用增大批量替代耗时排查。

当前领取硬条件是：

```text
status = success
async_status = success
need_notification = 1
notification_status = pending
notification_due_at 已到期
```

因此仅看到 `notification_status=pending` 还不够。`need_notification=0`、同步执行未完成或 execution 失败都不会被领取。

## Monitor 同步与查询

SWE 侧 `MonitorSyncClient` 默认 base URL 是：

```text
http://localhost:9090/api
```

可通过 `SWE_MONITOR_API_URL` 覆盖。

SWE 会调用 Monitor：

| SWE 调用 | Monitor 接口 | 作用 |
| --- | --- | --- |
| `sync_job()` | `POST /monitor/sync/job` | upsert 定时任务定义 |
| `delete_job()` | `DELETE /monitor/sync/job/{job_id}` | 标记删除 |
| `record_execution()` | `POST /monitor/sync/execution` | 写执行记录 |
| `record_execution()`（完整 dispatch 身份） | `POST /scheduler/cron/execution` | 向独立 Scheduler 回传完成/失败，驱动重试与补位 |
| `claim_due_notifications()` | `POST /monitor/sync/notifications/claim` | 领取待通知记录 |
| `mark_notification_sent()` | `POST /monitor/sync/notifications/{id}/sent` | 标记通知成功 |
| `mark_notification_failed()` | `POST /monitor/sync/notifications/{id}/failed` | 标记通知失败或重试 |
| `mark_job_as_read()` | `POST /monitor/cron/jobs/{job_id}/mark-read` | 标记任务执行记录已读 |

Monitor 数据表主要是：

- `swe_cron_jobs`
- `swe_cron_executions`

Monitor 写入时间统一处理为北京时间 naive datetime，SWE 侧也会把 UTC 时间转换为 Asia/Shanghai 后再同步，避免表格里出现时区不一致。

Monitor 查询入口在 `monitor/src/monitor/app/routers/cron.py`，提供任务列表、执行历史、概览、订阅概览、导出、已读和未读数查询。

## 批调度批次与 worker 查询

Monitor 直接读取 Scheduler 写入的批调度表，提供：

| 接口 | 主要参数 | 用途 |
| --- | --- | --- |
| `GET /api/monitor/cron/dispatch/batches` | `start_time`、`end_time`、`status`、`page`、`page_size` | 分页查询当前 source 的批次 |
| `GET /api/monitor/cron/dispatch/batches/{batch_id}` | `intent_limit`、`event_limit` | 查询批次统计、intents 和 events |
| `GET /api/monitor/cron/dispatch/workers` | - | 查询模型作用域 worker 策略和当前容量 |

这些接口沿用 `X-Source-Id` 隔离。批次状态只能说明 Scheduler 编排状态；要定位单个任务为什么没有执行，还需要结合 intent event、SWE execution 和 trace。

详细派发合同见 [Cron 批调度与独立 Scheduler](cron-batch-dispatch.md)。

## 网关侧最新 execution 子任务数

Monitor 还提供一个面向网关的外部接口：

```http
GET /api/monitor/external/cron/jobs/{job_id}/latest-execution/subtask-count
X-Source-Id: <source_id>
```

返回：

```json
{
  "job_id": "<job-id>",
  "execution_id": 123,
  "trace_id": "<trace-id>",
  "subtask_count": 8
}
```

边界规则：

- 必须提供非空 `X-Source-Id`；Monitor 即使假设网关已完成外部鉴权，也仍按 source 过滤 job。
- job 不存在返回 404。
- job 存在但没有 execution 时返回 200，`execution_id`、`trace_id` 为 null，`subtask_count=0`。
- 最新 execution 按 `actual_time DESC, id DESC` 选择；没有 trace 时计数为 0。
- 子任务数来自 `swe_cron_subtasks` 中该最新 trace 的记录数，而不是广播子任务数量。
