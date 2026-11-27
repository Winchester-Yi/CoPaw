# 模型请求可观测首批测试报告

## 结果

| 范围 | 结果 |
| --- | --- |
| 可观测、Provider、Tracing、模型工厂、会话、工作区和 SubAgent 单测 | 1,290 项通过。最终流清理调整后定向复跑 184 项通过 |
| 隔离 MySQL 8.4.8 可观测集成 | 最终复跑 59 项通过 |
| Console 可观测页面 | 81 项通过。目标 ESLint、Prettier 与生产构建通过 |
| 后端代码质量 | 本批变更的 24 个 Python 源文件通过目标 pre-commit |
| 真实浏览器嵌入验收 | Chrome 158.0.8127.88 和 Chromium 108.0.5359.29 均通过三个场景 |

部分本地提交直接运行了定向校验。后端最终源文件另行执行完整目标 hook。Console 生产构建输出了 Rollup 分包和体积警告。

## 端到端与边界场景

- 生产 SSE 经实际 OpenAI SDK、模型工厂、JSONL、倒序入库、隔离 MySQL、Source 授权 API 和 Console。两种浏览器都在同源 iframe 中使用 `hideMenu=true`，通过键盘进入请求详情。验证实际模型 `deepseek-v4-flash`、结束原因 `stop`、输入 333、输出 11、合计 344，以及跨 Source 404。 [集成测试](../../tests/integrated/observability/test_model_request_sse_mysql.py)
- 首次 HTTP 失败，第二次请求获得 20 个输入 Token。两种浏览器均验证摘要「已观测 20」、首次请求「未获取」、第二次请求显示 20。流在用量帧前取消时验证取消终态与「未获取」。MySQL 另验证失败请求和后续请求各有 10 和 20 时累计为 30，以及真实零、历史空值、重复入库、迟到开始事件和真实 SQL 行错误隔离。 [投影回归](../../tests/integrated/observability/test_review_fixes_mysql.py)
- SDK 单测覆盖 OpenAI 通用与 Kimi、双流交错、跨任务消费、消费前关闭、采集关闭且旧追踪开启时关闭连接、结构化 SDK 异常及部分流式事实保留。 [SDK 回归](../../tests/unit/observability/test_model_request_facts.py)
- 模拟阻塞 NAS 和观测数据库提交，聊天仍执行，登记成功后新证据可关联。真实 worker 子进程测试覆盖元数据转发。并发回收测试验证补偿终态只接受一次。 [正文证据回归](../../tests/unit/observability/test_outbox_runtime.py) · [后台回归](../../tests/unit/subagents/test_background_supervisor.py)

模型 HTTP 响应使用受控传输重放样例，浏览器使用本地授权测试宿主。现场 360 的 108.0.5359.95、生产 Provider 和部署侧采集器未纳入本次实测。
