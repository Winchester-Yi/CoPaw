# 定时任务批调度简报接入

Scheduler 异步受理调度平台的作业回调，Monitor 读取新版批次和共享模型池快照，SWE 通过招乎逐人投递。字段、统计和状态口径见 [详细设计](../superpowers/specs/2026-11-24-cron-batch-status-report-design.md)。

## 部署

1. 在 Scheduler 使用的共库人工执行 `scripts/sql/cron_batch_status_reports.sql`。Monitor 继续读取现有新版看板、异步子任务和共享模型池相关表。核对看板升级 SQL 中的 `idx_managed_receipt_board` 已存在。
2. 部署 Monitor、SWE 和 Scheduler。在 Scheduler 设置 `SCHEDULER_SWE_INTERNAL_API_BASE_URL`，填写 Scheduler 可直连的 SWE 内网根地址，供简报取数和投递使用。SWE 通过 `SWE_MONITOR_API_URL` 向 Monitor 取数。已配置 `SWE_INTERNAL_TOKEN` 的环境继续沿用同一令牌。简报后台在共库表和独立地址就绪后启动。
3. 在调度平台注册作业，直接调用 Scheduler 内网地址。平台接口依赖现有内网访问边界，可达调用方可以提交来源和收件人。Scheduler 简报入口及 SWE、Monitor 内部接口仅供可信内网使用，招乎投递路径关闭代理自动重放。

关注筛选随 Monitor 与 Scheduler 共同上线，判据为父任务关注等级 `1`–`5`。按 [关注上线顺序](../superpowers/specs/2026-11-25-cron-batch-follow-design.md#6-实施顺序和验收) 先执行 `scripts/sql/cron_batch_follow.sql` 并为各来源设置关注等级。批次概况和明细使用关注范围，共享模型池展示全来源占用。

SWE 读取 `default_<source_id>` 下默认 Agent 的招乎配置。渠道需已启用，员工号查询须返回匹配的 `sapId` 和有效的 `ystId`。新版简报先发送单独一条汇总，再按顺序发送逐批明细。每条正文最多 2048 个 UTF-8 字节，查询接口中的 `part_no=1` 对应汇总。升级时部署新版 Monitor 和 Scheduler，即可为新生成的快照使用此格式。

如果测试环境已执行上一版建表 SQL，可把原作业标识和计划时刻列改为可空，保留历史数据。首次部署直接执行当前建表 SQL。

```sql
ALTER TABLE swe_cron_batch_status_reports
  MODIFY COLUMN report_job_id VARCHAR(128) NULL COMMENT '历史平台作业标识',
  MODIFY COLUMN scheduled_at DATETIME(6) NULL COMMENT '历史计划触发时间';
```

## 平台作业

`POST /api/scheduler/cron/batch-status-reports`

请求头使用 `Content-Type: application/json`。

```json
{
  "source_id": "RMASSIST",
  "recipient_ids": ["00001234", "00005678"]
}
```

可在调度平台给作业配置固定请求体。Scheduler 按收到请求时的北京时间确定业务日，每次受理生成一个新的 `report_id`，再异步取数和发送。HTTP 202 表示请求已登记。平台重发 POST 会生成另一份简报。用 `GET /api/scheduler/cron/batch-status-reports/{report_id}?source_id=RMASSIST` 查看报告及逐人状态。每份报告的每段只尝试投递一次。渠道拒绝、超时或开始投递后进程中断的结果保留在查询接口，其他未尝试的投递继续处理。

真实作业启用前，用测试来源和测试收件人验证平台触发、员工号解析、消息长度与渠道回执。停用时关闭平台作业或既有入口，保留投递记录供核对。
