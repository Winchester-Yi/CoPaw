# 定时任务通知领取 source 范围

多实例部署时，如果不同 SWE 实例负责不同 `source_id` 的租户，通知 worker 领取任务必须限制在当前实例允许处理的 source 范围内。

## 配置入口

- `SWE_CRON_NOTIFICATION_SOURCE_IDS`：当前 SWE 实例允许领取通知的 source 列表。
- 支持逗号、空格或换行分隔，例如：`SWE_CRON_NOTIFICATION_SOURCE_IDS=source-a,source-b`。
- 未配置时保持历史行为，不向 Monitor claim 请求传 `source_ids`，Monitor 侧也不会追加 source 过滤条件。
- 已配置 source 列表时，`source_id` 为空字符串或 `NULL` 的通知记录仍会被所有实例竞争领取。

## 代码入口

- SWE 通知 worker：[`src/swe/app/crons/notification_worker.py`](../../src/swe/app/crons/notification_worker.py)
- SWE 调用 Monitor 领取通知：[`src/swe/app/crons/monitor_sync_client.py`](../../src/swe/app/crons/monitor_sync_client.py)
- Monitor 领取接口：[`monitor/src/monitor/app/routers/sync.py`](../../monitor/src/monitor/app/routers/sync.py)
- Monitor 领取 SQL：[`monitor/src/monitor/app/services/cron/notification_service.py`](../../monitor/src/monitor/app/services/cron/notification_service.py)

## 排查顺序

1. 确认 SWE 实例环境变量是否包含预期 `source_id`。
2. 查看 SWE 请求 Monitor 的 `/monitor/sync/notifications/claim` 请求体是否带 `source_ids`。
3. 查看 Monitor 领取 SQL 是否 join `swe_cron_jobs` 并追加 `j.source_id IN (...) OR j.source_id IS NULL OR j.source_id = ''`。
4. 如果某条成功执行记录未被领取，先确认对应 `swe_cron_jobs.source_id` 是否属于当前实例配置范围。

## 领取后处理速度

- `SWE_CRON_NOTIFICATION_CONCURRENCY` 控制每个 SWE 通知 worker 的并发处理数，默认 5，范围 1–20；设为 1 恢复串行，修改后重启实例生效。
- 并发名额覆盖 workspace 获取、通知发送和 Monitor 状态回写；单条异常不影响同批其他记录，停止 worker 会取消并等待批内处理结束。
- 每批默认领取 20 条，按 `notification_due_at, id` 升序选取当前 source 范围内符合条件且可获取锁的记录。非空批次处理完成且没有报告异常时立即领取下一批，不足 20 条也会继续查一次；空队列、领取异常或批内处理异常后才默认等待 300 秒。多个实例交错领取时，全局已锁 pending 数量不必降为 0，并发完成顺序不保证。
- 判断是否积压，应同时查看全部到期 pending 数量、通知 sent/failed 状态变化以及同一批 execution 的锁时间。锁非空只代表已领取，pending 减少也可能来自失败次数达到上限。
- 600 秒锁过期规则保持不变；耗时超过锁有效期仍可能重复领取。扫描连接超时与单条发送/回写失败需分别查 `Cron notification scan failed` 和 `Cron notification processing failed: execution_id=...` 日志。
