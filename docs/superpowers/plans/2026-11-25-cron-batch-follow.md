# 批调度任务关注实施计划

以 [详细设计](../specs/2026-11-25-cron-batch-follow-design.md) 为准。在独立工作树实施，新增行为先用定向测试固定。

1. 父任务关注字段与管理员写接口。验证完整身份、幂等写入和跨来源拒绝。
2. 增加 `task_scope=followed_batch`。验证真实批次候选、普通执行复用、取消关注与入队互斥及旧范围兼容。
3. 接入 Monitor 批次投影和简报筛选。验证关注对下一次取数生效，汇总与明细一致，模型池维持来源口径。
4. 接入 Console 详情按钮及列表状态。验证请求来源、回读状态、失败和迟到响应。
5. 运行相关 pytest、Console 测试和类型检查，执行隔离 MySQL 回归。检查差异及浏览器兼容验收情况。

## 本地检视修复第 1、3 项

1. 在现有补跑、人工重跑、自动重跑测试中先改变父任务等级，再断言入队快照。三条路径在持有父任务定义锁的事务内读取当前等级。验证 `test_managed_batch_follow.py`、`test_managed_batch_retry.py`、`test_managed_batch_auto_retry.py`。
2. 失败明细沿用列表的关注等级区间。验证 `test_managed_board_failures.py` 的 HTTP 筛选，以及 `FailureSummary.test.tsx` 的请求范围与切换时取消旧请求。
