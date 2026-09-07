# Cron 批调度详情全量分页计划

## 问题与边界

批调度详情目前固定读取前 500 条 Intent 和最新 500 条事件，前端筛选也只作用于已加载数据。本次让用户通过服务端分页查看全部 Intent 与事件，并让 Intent 文本、角色、状态筛选作用于完整结果集。Batch 列表、Scheduler 状态机、数据库表结构和事件顺序语义保持不变。

## 验收要求

- 单个 Batch 超过 500 条 Intent 或事件时，可以逐页访问全部记录。
- Intent 文本、角色和状态筛选由 Monitor 查询参与计数和分页；筛选变化回到第 1 页。
- Intent 返回匹配总数和 Batch 原始总数；事件返回完整总数。
- 页面切换 Batch、切换分页或快速筛选时，不允许旧请求覆盖当前详情。
- 既有调用不传新参数时仍可使用详情接口。

## 实施单元

### U1. 扩展 Monitor 批次详情分页契约

**Goal:** 为 Intent 与事件提供独立的服务端分页，并支持全量 Intent 筛选。

**Files:**

- `monitor/src/monitor/app/models/cron.py`
- `monitor/src/monitor/app/routers/cron.py`
- `monitor/src/monitor/app/services/cron/query_service.py`
- `tests/unit/monitor/test_cron_dispatch_monitor.py`

**Approach:** 保留详情路由，增加 Intent/事件页码与页大小；Intent 查询使用参数化条件匹配现有可搜索字段，并分别统计原始总数、匹配总数和事件总数。列表查询使用稳定排序和 offset，避免数据库结构变化。

**Execution note:** 先补分页、筛选、参数顺序及第二页结果的失败测试，再实现查询。

**Test scenarios:**

- Intent 第二页使用正确 offset，并返回 Batch 原始总数与筛选后总数。
- 文本、角色、状态组合筛选同时作用于 count 与 rows 查询。
- 事件第二页按倒序稳定分页并返回事件总数。
- 不传筛选时保留现有详情语义。

### U2. 将 Console 详情切换为服务端分页

**Goal:** 让用户在现有详情页逐页查看和筛选全部 Intent 与事件。

**Dependencies:** U1

**Files:**

- `console/src/api/modules/monitor.ts`
- `console/src/api/modules/monitor.test.ts`
- `console/src/pages/Monitor/CronBatchDispatch/index.tsx`
- `console/src/pages/Monitor/CronBatchDispatch/index.test.tsx`
- `console/src/pages/Monitor/CronBatchDispatch/index.module.less`

**Approach:** 使用受控分页状态请求详情；Intent 搜索保持防抖并发送服务端，分页器展示匹配总数；事件增加独立分页器。切换 Batch 或筛选时重置对应页码，沿用现有 request id 防止竞态。

**Execution note:** 先补超过一页、筛选重置、事件翻页和旧请求隔离测试，再实现 UI。

**Test scenarios:**

- 首次详情请求使用固定页大小而非 500 条全量上限。
- Intent 翻页、文本筛选、角色筛选和状态筛选发送正确参数。
- 筛选变化回到第 1 页，并显示匹配总数与 Batch 总数。
- 事件翻页请求独立页码，能够访问第 500 条之后的事件。
- 切换 Batch 时详情页码复位，旧响应不能覆盖新 Batch。

## 验证

- Monitor 定向 pytest 覆盖详情分页和筛选。
- Console 定向 Vitest、TypeScript、ESLint、Prettier 检查通过。
- 最终差异只覆盖计划中的 API、查询、页面、测试和必要样式。
