# 外部调度回调示例

普通模式下，外部调度平台到点后回调 SWE，不直接执行 Agent，入口是 `POST /api/internal/cron/callback`。批调度模式下，父任务物理 timer 改为回调独立 Scheduler；不要把两种 callback 合同混在一起。

返回 [Cron 示例索引](../README.md)。

## jobParam 解码后的格式

`RealSchedulerAdapter` 写入外部调度平台时，会把上下文编码成 base64 JSON 放进 `jobParam`。解码后结构类似：

```json
{
  "tenant_id": "tenant-a",
  "source_id": "RMASSIST",
  "scopeId": "tenant-a-RMASSIST",
  "agent_id": "default",
  "task_type": "job",
  "job_id": "0f6f7c62-4c51-4c5c-93b1-6a8dd2d9a0c1",
  "fromId": "tenant-a"
}
```

回调接口收到 `jobParam` 后会：

1. base64 解码。
2. 取出 `tenant_id`、`source_id`、`agent_id`、`task_type`、`job_id`。
3. 用 `resolve_runtime_tenant_id(tenant_id, source_id)` 找运行时 workspace。
4. 找到对应 `CronManager`。
5. 对 `task_type=job` 调用 `run_job(job_id, is_manual=False, source_id=source_id)`。

## 直接参数格式

接口也支持不传 `jobParam`，直接把参数放在 body 顶层。这个格式适合本地验证回调路由：

```powershell
$baseUrl = "http://127.0.0.1:8088"

$body = @{
  tenant_id = "tenant-a"
  source_id = "RMASSIST"
  agent_id = "default"
  task_type = "job"
  job_id = "0f6f7c62-4c51-4c5c-93b1-6a8dd2d9a0c1"
} | ConvertTo-Json

curl.exe -X POST "$baseUrl/api/internal/cron/callback" `
  -H "Content-Type: application/json" `
  -d $body
```

成功返回：

```json
{
  "status": "ok",
  "task_type": "job"
}
```

## heartbeat 和 dream

系统任务也走同一个回调入口，只是 `task_type` 不同：

```json
{
  "tenant_id": "tenant-a",
  "source_id": "RMASSIST",
  "agent_id": "default",
  "task_type": "heartbeat"
}
```

```json
{
  "tenant_id": "tenant-a",
  "source_id": "RMASSIST",
  "agent_id": "default",
  "task_type": "dream"
}
```

`task_type=job` 必须带 `job_id`；`heartbeat` 和 `dream` 不需要。

## 常见误区

- 不要把外部平台的 `tenant_id` 当成运行时 workspace ID；当前代码会用 `tenant_id + source_id` 解析运行时租户。
- 不要用手动运行接口代替调度回调排查；`/api/cron/jobs/{job_id}/run` 是 `is_manual=True`，回调是 `is_manual=False`。
- 如果返回 404 `CronManager not found`，优先检查 `tenant_id`、`source_id`、`agent_id` 是否和任务创建时一致。
- 已启用批调度的父任务或子任务收到这种不带 dispatch 身份的普通自动回调时会被跳过，以免旧 timer 重复执行。

## 批调度父 timer 回调

批调度物理 timer 的入口是：

```http
POST http://127.0.0.1:9100/api/scheduler/cron/callback
```

它只负责创建 batch/intents，不直接运行 Agent。Scheduler 随后用带 `callback_source=dispatch_service`、intent/batch/attempt 的 payload 回调 SWE `/api/internal/cron/callback`。完整示例见 [批调度模式](../batch-dispatch/README.md)。
