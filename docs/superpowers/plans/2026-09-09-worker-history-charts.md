# Worker 调整历史与模型折线图

目标：按所选 source/时间范围获取全部 worker 调整记录，展示每个 provider/model 的有效 worker 阶梯折线，并能查看任意一条记录的详情。

- Monitor `/dispatch/workers` 保留现有响应字段，新增可选 `capacity_cursor` 请求参数与 `capacity_events_next_cursor` 响应字段。
- 每页 100 条，通过 created_at/id 游标翻页。首次查询固定 max(id)，后续记录 id 不得越过边界；游标绑定 source 与起止时间，避免跨范围误用。时间过滤采用闭区间，与现有查询一致。
- 首屏返回实时容量/策略以及第一页历史；翻页只读取历史。前端逐页拉取，显示已加载条数；切换 source、时间或刷新会停止旧分页链，失败时清空不完整历史并显示错误。
- 去掉前端 8 条截断，保留逐条翻阅和详情展开，增加直接跳转分页。
- 每条线代表 provider/model，以真实时间升序、同时间按 id 排列；纵轴为整数有效 worker 数量。采用阶梯线，无数据时不伪造零值；提供图例选择、区间缩放和 tooltip。
- 图表复用已安装 ECharts，图表辅助函数和分页辅助函数单独测试。保持已有批调度优先级、失败重试与 worker 决策逻辑。

实现顺序：
1. 新增 Monitor capacity_history.py，SQLite 验证超过 100 条、时间/source 隔离、同时间 id 顺序、快照边界和无效游标。
2. 更新 query_service/models/router 的增量分页契约，跑 Monitor dispatch 测试。
3. 前端分页加载、全量记录翻阅和图表；验证大于 8 条、多个 provider 的同名模型、乱序时间、空数据与请求失效。
4. 相关 pytest/Vitest、TypeScript、ESLint/Prettier、浏览器验证，更新说明。已有未提交内容保留，不自动提交或改写此前交付包。

## 本地验证

- Monitor 历史分页与 dispatch 查询：16 passed，覆盖 205 条、同时间跨页、区间闭边界、source/时间游标误用、新增记录水位隔离。
- Console 页面与分页/图表 helper：19 passed，覆盖分页取齐、第 8 条之后的跳转、请求失效、分页失败、同名模型按 provider 分组。
- Console build（含 TypeScript）通过；相关 ESLint、Prettier、新 Python 文件 Flake8 通过。构建仍有已有的大 bundle 提示，jsdom 仍提示不支持伪元素。
- 浏览器使用实际页面与 ECharts、120 条模拟记录，确认两条模型曲线、120 条历史、直接跳到最后一条；1280/1440 布局及页面无横向溢出检查通过。截图保存在 output/playwright/worker-history-chart-*.png。
- 未对线上数据库执行查询或修改。只修改 Monitor 历史读取和 Console 展示，未修改 Scheduler worker 决策；临时 UI 验收入口已清理。
