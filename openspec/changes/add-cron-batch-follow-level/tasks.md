## 1. 数据与写入

- [x] 1.1 把未部署的 `scripts/sql/cron_batch_follow.sql` 由布尔列改写为 `batch_follow_level` + 审计列，并附一次性的布尔版存量迁移（`1` → 约定默认等级），先确认目标环境是否已按布尔版建表。
- [x] 1.2 把 `scheduler/.../managed/batch_follow.py` 的关注写入改为等级写入：严格校验 `0`–`5` 整数、沿用现有父任务锁顺序、同值幂等、审计字段只在真正切换时更新。
- [x] 1.3 扩展 `PUT /api/scheduler/cron/managed-dispatch/batches/{batch_id}/follow` 的请求体为 `{ "level": 0..5 }`，保持 manager/admin 权限、同来源校验与错误码约定。

## 2. 派发排序

- [x] 2.1 在 `managed/scheduling.py` 冻结的 `dispatch_meta` 中写入 `batch_follow_level`（默认 `0`），并确认新增批次按父任务当前等级取值。
- [x] 2.2 在 `managed/work_query.py` 的 `_batch_order()` 中插入等级降序键，位置在 `urgent` 之后、`batch_created_at` 之前；缺失或非法值按 `0` 处理。
- [x] 2.3 在等级写入的同一事务内刷新该父任务下 `queued`/`dispatch_failed` 成员的 `dispatch_meta_json`，已派发成员不动。
- [x] 2.4 在 `managed/daily_retry_enqueue.py`、`managed/batch_retry.py`、`managed/executions.py` 的元数据拷贝清单中加入等级键。
- [x] 2.5 在 `managed/daily_retry_enqueue.py`、`managed/batch_retry.py` 的元数据拷贝清单中加入等级键（新触发批次由 `managed/scheduling.py` 直接取值）。
- [x] 2.6 恢复巡检单轮上限改为环境变量 `SCHEDULER_MANAGED_RECOVERY_LIMIT`（默认 32、取值 1–200、非法回退并告警），并在装配处传入。
- [x] 2.7 恢复巡检轮询间隔改为环境变量 `SCHEDULER_MANAGED_RECOVERY_INTERVAL_SECONDS`（默认 2、取值 1–60、非法回退并告警），并在启动循环处传入。

## 3. 读模型与过滤

- [x] 3.1 `monitor/.../cron/managed_board_sql.py` 与 `managed_board.py` 投影 `batch_follow_level` 整数（父任务被删除或非 Scheduler 管理时为 `null`）。
- [x] 3.2 当日失败补跑过滤器（`managed/daily_retry_filters.py`）由布尔改为 `batch_follow_level>=1`。
- [x] 3.3 批调度简报取数（`monitor/.../cron/batch_status_report.py`）同步改为 `>=1`，并在正文中标注批次等级。

## 4. Console

- [x] 4.1 批次行右键菜单提供 `0`–`5` 等级设置（选中项打勾），成功后刷新列表，失败保留原值；设置入口只保留右键一处。
- [x] 4.2 批次列表把「已关注」标签替换为任务名下方的黄色星标（等级 N 显示 N 颗）；`null` 或字段缺失时不展示。
- [x] 4.3 更新 `managedBatchBoard.ts` 类型与 follow 接口签名，旧响应（无等级字段）不崩溃。
- [x] 4.4 工具栏新增「关注等级筛选」（全部 / 已关注 / 未关注 / L1–L5），选中值转成 `follow_min`/`follow_max` 传给看板接口，并回到第一页。
- [x] 4.5 Monitor 看板查询新增 `follow_min`/`follow_max`（0–5），在 `batch_where` 里按父任务有效等级过滤，列表与概览统计使用同一范围。

## 5. 文档

- [x] 5.1 更新 `scheduler/CONTEXT.md` 的「批调度父任务关注」术语为「批调度父任务关注等级」，并补充「等级不保证调度时延、不做饥饿保护」的边界说明。
- [x] 5.2 更新 `docs/superpowers/specs/2026-11-25-cron-batch-follow-design.md` 的关注模型章节，衔接本次等级化改动。
- [x] 5.3 更新 `docs/integrations/cron-daily-failure-rerun.md` 与 `docs/integrations/cron-batch-status-report.md` 的过滤语义说明。

## 6. 验收

- [x] 6.1 单元测试：等级写入校验与幂等、`_batch_order` 顺序（`urgent → 等级 → 既有键`）、改级刷新已排队成员、三条重跑路径继承等级、等级 `0` 与 `null` 的排序兜底。
- [ ] 6.2 隔离 MySQL 集成测试：`JSON_SET` 刷新、排序表达式与索引命中、并发改级与派发的事务可见性。
- [x] 6.3 Monitor 测试：整数投影、简报只含 `>=1` 批次、等级标注出现在正文。
- [x] 6.4 Console 测试：右键设置等级与列表刷新、星标数量、关注等级筛选、旧响应降级。
- [ ] 6.5 验收：DDL → Scheduler → Monitor → Console 顺序上线，验证「改级后已排队批次立即优先」与「等级不会提前 `not_before_at`」；在 Chromium 108 或对应 360 内核复验看板交互。
