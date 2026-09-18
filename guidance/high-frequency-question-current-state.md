# 高频问题分析现状

本文记录当前仓库里“高频问题分析”的实际实现状态。这里的“当前”以代码为准，主要覆盖 Monitor 后端接口、Console 入口、异步任务流、结果缓存和源消息查询契约。

## 功能定位

高频问题分析用于从用户消息里提取咨询主题，生成 Top 10 高频问题结果。当前链路分成两层：

- 源消息接口：给外部 AI 工作流取数，读取 `swe_tracing_traces` 中可分析的用户消息。
- 任务与结果接口：给 Console 弹窗使用，先查已有结果，再按需提交异步任务，最终展示 `swe_high_frequency_question_result` 中的 Top 10。

Console 的用户入口在“用户消息”页面。用户点击“高频问题分析”后打开弹窗，默认查询最近 7 天、全部机构的结果。

## 入口与路由

Monitor 路由定义在 `monitor/src/monitor/app/routers/high_frequency_question.py`，router 前缀是：

```text
/monitor/high-frequency-question
```

Monitor 应用统一挂载 `api_router` 到 `/api`，所以对外路径是：

```text
POST /api/monitor/high-frequency-question/messages
POST /api/monitor/high-frequency-question/results
POST /api/monitor/high-frequency-question/tasks
POST /api/monitor/high-frequency-question/prewarm
GET  /api/monitor/high-frequency-question/results
```

这些接口的 `source_id` 解析规则一致：优先使用 Header `X-Source-Id`，没有时使用 body/query 中的 `source_id`，再没有则使用 `default`。

## 源消息查询接口

`POST /api/monitor/high-frequency-question/messages` 用于查询待分析的用户消息。它读取 `swe_tracing_traces`，不是读取高频问题结果表。

请求体：

```json
{
  "source_id": "RMASSIST",
  "start_time": "2026-07-23 00:00:00",
  "end_time": "2026-07-30 00:00:00",
  "bbk_id": "110"
}
```

`bbk_id` 可为空。该接口允许查询的最大时间跨度是 31 天。

当前返回体：

```json
{
  "total": 3,
  "message_count": 3,
  "user_count": 2,
  "data": [
    {
      "user_id": "136807",
      "bbk_id": "110",
      "content": "查保险",
      "skills_used": ["保险助手", "客户分析"]
    }
  ]
}
```

字段说明：

- `total`：兼容字段，当前等于返回消息条数。
- `message_count`：返回的消息条数。
- `user_count`：返回行里非空 `user_id` 的去重数量。
- `data[].user_id`：消息所属用户。
- `data[].bbk_id`：消息所属机构。
- `data[].content`：`swe_tracing_traces.user_message`。
- `data[].skills_used`：`swe_tracing_traces.skills_used` 解析后的 `list[str]`。

该接口当前不返回：

- `message_id`
- `session_id`
- `message_time`

过滤条件：

- `source_id = resolved source_id`
- `start_time >= request.start_time`
- `start_time < request.end_time`
- `status = 'completed'`
- `session_id NOT LIKE 'cron-task%'`
- `user_message IS NOT NULL`
- `TRIM(user_message) != ''`
- `TRIM(user_message)` 不在无意义短文本黑名单中，例如“好”“好的”“收到”“继续”“谢谢”“ok”等。
- 如果传入 `bbk_id`，追加 `bbk_id = request.bbk_id`。

排序为 `start_time ASC, trace_id ASC`。服务最多读取 `10001` 行用于判断超限；如果匹配超过 `10000` 条，返回 400，提示缩小时间范围或机构范围。

## 结果保存接口

`POST /api/monitor/high-frequency-question/results` 用于外部 AI 工作流保存完整分析结果。它写入 `swe_high_frequency_question_result`。

请求结果项包含：

```json
{
  "scope_type": "ALL",
  "bbk_id": "ALL",
  "rank_no": 1,
  "topic_name": "查询客户保险持仓",
  "message_count": 520,
  "valid_message_count": 4000,
  "user_count": 1200,
  "total_skill_used_count": 2600,
  "skill_used_count": 380,
  "top_skill": "保险助手",
  "bbk_dis": {},
  "sample_questions": ["查询客户目前有哪些保险产品"]
}
```

这里的 `valid_message_count`、`user_count`、`total_skill_used_count` 是批次级字段，同一个 batch 下每个 topic 行保持一致。`message_count`、`skill_used_count`、`top_skill` 是 topic 级字段。

保存策略：

- 校验所有结果项后再写库。
- 同一个请求内不允许重复 `source_id + batch_id + scope_type + bbk_id + rank_no`。
- `rank_no` 限制为 1 到 10。
- `sample_questions` 最多 4 条，每条最长 1000 字符。
- 一个事务内先删除同 `source_id + batch_id` 的旧结果，再批量插入新结果。
- 日志不打印完整 `sample_questions`。

## 任务提交与预跑

`POST /api/monitor/high-frequency-question/tasks` 是 Console 按需生成分析使用的接口。请求条件最大日期跨度是 7 天。

提交时会先标准化条件：

- `bbk_id` 为空：`scope_type = ALL`，`bbk_id = ALL`
- `bbk_id` 非空：`scope_type = ORG`，`bbk_id = 原始机构 ID`
- 匹配结果时按 `start_time.date()` 和 `end_time.date()` 做日期级匹配

如果没有传 `force=true`，提交接口会先查 24 小时内是否已有同条件成功结果：

- 命中则直接返回 `state = AVAILABLE` 和结果，不创建新任务。
- 未命中则创建 `swe_async_tasks` 任务，状态为 `running`，返回 `state = RUNNING`。

新任务字段：

- `service = monitor`
- `task_type = monitor.high.freq.question`
- `title = 用户高频问题分析`
- `source_id = resolved source_id`
- `result_json.request` 保存标准化后的 `source_id/start_date/end_date/scope_type/bbk_id`
- `task_id` 是 Monitor 内部异步任务 ID；Monitor 使用同一个值作为传给外部工作流的 `batch_id`

当前后端提交接口不阻止相同条件的重复 running 任务。也就是说，如果没有 24 小时成功结果，多次提交可能创建多个任务。Console 在查询结果为空后，会额外查 TaskCenter 的 running 任务并在前端复用匹配任务，减少用户侧重复提交。

`POST /api/monitor/high-frequency-question/prewarm` 是预跑入口。未指定时间时默认提交最近 7 天、全部机构的任务，actor 记录为系统定时任务。

## 外部工作流调用

任务创建后，Monitor 通过 `asyncio.create_task` 在后台调用外部工作流。工作流配置来自 Monitor 环境配置，关键项包括：

- `HFQ_WORKFLOW_URL`
- `HFQ_WORKFLOW_API_KEY`
- `HFQ_WORKFLOW_OPEN_ID`
- `HFQ_WORKFLOW_RESPONSE_MODE`
- `HFQ_WORKFLOW_TIMEOUT_SECONDS`

发送给工作流的 payload 包含：

```json
{
  "inputParams": {
    "source_id": "RMASSIST",
    "batch_id": "uuid",
    "start_time": "2026-07-23 00:00:00",
    "end_time": "2026-07-30 00:00:00",
    "bbk_id": ""
  },
  "openId": "...",
  "responseMode": "noStreaming"
}
```

全部机构时 `bbk_id` 传空字符串；具体机构时传实际机构 ID。

工作流 HTTP 调用成功且响应 JSON 中 `message == "success"` 后，Monitor 继续检查 `swe_high_frequency_question_result` 是否已有对应 `source_id + batch_id` 的结果行。存在结果行才把任务标记为 `succeeded`。

如果工作流异常或响应不符合预期，Monitor 会继续轮询结果表一段时间。只要等待窗口内出现对应结果行，任务仍会标记为成功；否则标记为 `failed`，并写入截断后的安全错误信息。

## 结果查询与缓存

`GET /api/monitor/high-frequency-question/results` 是 Console 弹窗默认使用的查询接口。它只查成功结果，不负责创建任务，也不直接返回 running 任务。

返回状态：

- `AVAILABLE`：24 小时内有同条件成功结果。
- `AVAILABLE_STALE`：没有 24 小时内结果，但有更早成功结果。
- `EMPTY`：没有同条件成功结果。

结果返回字段：

```json
{
  "state": "AVAILABLE",
  "task_id": "batch-id",
  "batch_id": "batch-id",
  "status": "succeeded",
  "source_id": "RMASSIST",
  "stat_start_time": "2026-07-23T00:00:00",
  "stat_end_time": "2026-07-30T00:00:00",
  "scope_type": "ALL",
  "bbk_id": "ALL",
  "result_updated_at": "2026-08-04T03:07:00",
  "message_count": 4000,
  "user_count": 1200,
  "total_skill_used_count": 2600,
  "topic_count": 10,
  "skill_gap_topic_count": 4,
  "topics": [
    {
      "rank_no": 1,
      "topic_name": "保险产品咨询与购买",
      "message_count": 520,
      "valid_message_count": 4000,
      "skill_used_count": 380,
      "top_skill": "保险助手",
      "bbk_dis": {},
      "sample_questions": ["我想给自己买一份重疾险，应该怎么选择？"]
    }
  ],
  "message": null
}
```

其中 `skill_gap_topic_count` 表示技能承载率低于 50% 的高频主题数，
计算口径为 `skill_used_count / message_count < 50%`。

`AVAILABLE_STALE` 会带提示文案：

```text
最近一次更新失败，当前展示历史结果
```

`result_updated_at` 来自同一批结果行的 `MAX(created_at)`。

## Console 当前行为

Console 入口在 `console/src/pages/Analytics/Messages/index.tsx`，API 封装在 `console/src/api/modules/monitor.ts`。

弹窗打开后：

1. 默认时间为最近 7 天。
2. 默认机构为全部机构，若当前页面存在机构锁定则按锁定机构。
3. 自动调用 `GET /monitor/high-frequency-question/results` 查询缓存结果。
4. 如果返回 `EMPTY`，前端会查询 `monitor.high.freq.question` 的 running async task，尝试复用相同 `result_json.request` 的任务。
5. 用户点击“生成分析”后调用 `POST /monitor/high-frequency-question/tasks`。
6. 如果任务 running，前端轮询 TaskCenter 任务详情；任务成功后重新查询结果。

用户修改日期或机构后不会自动提交任务，只会重置当前结果状态。自定义日期跨度超过 7 天时，前端直接提示：

```text
高频问题分析最多支持 7 天的数据范围
```

结果展示：

- 标题显示“高频问题 TOP10”。
- 每个 topic 展示排名、主题名、最多 3 条代表问题。
- 右侧展示 `message_count / valid_message_count` 计算出的占比文本。
- 如果 `bbk_dis` 有数据，展示机构分布条。
- 当前 Console 代码尚未展示后端已返回的 `user_count`、技能承载汇总和 topic 技能字段。

## 当前容易混淆的点

1. `messages` 接口的 `user_count` 是源消息查询结果里的去重用户数，只在 `POST /messages` 顶层返回。
2. `results` 保存接口和 Top 10 结果项当前不包含 `user_count`。
3. `messages` 接口最大跨度是 31 天，任务提交和 Console 弹窗最大跨度是 7 天。
4. 后端 `GET /results` 不返回 running 任务；Console 自己通过 async task 列表做 running 任务复用。
5. 后端提交接口目前不做 running 任务去重；如果绕过 Console 直接重复提交，可能创建重复任务。
6. 根目录 `high_frequency_questions.md` 更偏早期用户流程描述，其中“后端复用相同 running 任务”的说法与当前 task-flow 实现不完全一致，应以当前代码和本文为准。

## 主要代码位置

| 位置 | 作用 |
|------|------|
| `monitor/src/monitor/app/routers/high_frequency_question.py` | Monitor 高频问题路由 |
| `monitor/src/monitor/app/models/high_frequency_question.py` | 请求和响应模型 |
| `monitor/src/monitor/app/services/tracing/high_frequency_question.py` | 源消息查询、结果保存、任务提交、工作流回调处理 |
| `monitor/tests/test_high_frequency_question.py` | 高频问题后端单测 |
| `console/src/api/modules/monitor.ts` | Console 高频问题 API 类型与请求封装 |
| `console/src/pages/Analytics/Messages/index.tsx` | 用户消息页面的高频问题弹窗 |
| `monitor/openspec/changes/add-high-frequency-question-apis/` | 基础 messages/results 接口 OpenSpec |
| `monitor/openspec/changes/add-high-frequency-question-task-flow/` | 任务流、缓存、预跑、Console 弹窗 OpenSpec |
