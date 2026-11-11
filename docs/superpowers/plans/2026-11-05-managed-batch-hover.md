# 新批调度看板状态与悬停详情

**Goal:** 已批准的三处增强：子任务状态标签、全局失败详情统计、事件完整详情悬停。

**Architecture:** 复用Monitor现有批次筛选、JSON物化及身份校验；新增只读失败汇总，按当前尝试统计类型和错误详情。Console悬停时请求，不在首屏加载时增加查询；旧看板与调度写逻辑不变。

**Tech Stack:** FastAPI/MySQL，React/Ant Design，pytest/Vitest。

## 约束

- 失败定义仍为主执行error或整轮settled_status=error；不累计历史重试，不混入dispatch_failed。
- 来源、时间、状态、关键词与顶部统计一致，不按当前分页求和。
- 权限沿用manager_source；保持关系损坏时报错，不返回假空数据。
- 新样式仅用于新版组件，兼容Chromium108的既有CSS/API，不增加依赖、DDL或配置。
- 按用户AGENTS顺序在主线程实施、自检；本轮不自动提交、推送。

## 步骤

1. 新增`tests/unit/scheduler/test_managed_board_failures.py`，使用真实SQLite查询验证全局分组、历史归属、当前尝试、筛选、损坏关系和HTTP权限；运行至缺少failure_summary方法的预期失败。
2. 在`monitor/.../cron/managed_board_failures.py`实现`failure_summary(db,source,start,end,status,query)`；复用`managed_board_sql._aggregate_ctes`进行一次物化和聚合，路由`GET /monitor/cron/managed-dispatch/failure-summary`返回`total/groups/queried_at`。
3. 在`BatchDetail.test.tsx`先断言标签色彩语义和事件hover全量JSON，再实现`settledStatusTag`、事件Tooltip及新版局部样式。
4. 在`FailureSummary.test.tsx`覆盖首次hover加载、筛选变化丢弃旧响应、失败重试、全局类型/详情数量；新增`FailureSummary`和API类型，将其挂到顶部失败数。
5. 运行受影响后端/前端测试、tsc、生产构建、Python复杂度和静态检查；用本地浏览器模拟截图核验，不声称已验证行内MySQL或真实Chromium108。

测试入口：`pytest tests/unit/scheduler/test_managed_board*.py`；`vitest run src/pages/Monitor/ManagedBatchDispatch src/api/modules/managedBatchBoard.test.ts`。
