# 剩余埋点测试报告

| 验证项 | 结果 |
|---|---|
| Python 定向回归 | 310 项通过。其中观测及压缩、Checkpoint 单测 245 项，独立观测集成 60 项，审批审计集成 5 项。使用本地隔离 MySQL 8.4.8。 |
| 采集健康 | 验证队列满时拒绝入队并计数，写出恢复后在仍有积压时输出累计摘要，关闭时补发新增计数。结构化记录通过正式投影协议校验。 |
| 上下文和 Checkpoint | 验证实际摘要压缩、候选安装及降级路径，真实文件提交及投影加载，缺失、取消和敏感正文边界。 |
| 重试与恢复 | 验证实际模型请求重试与取消，隔离 MySQL 投影保留失败、重试和成功恢复，授权 API 提供关系。Agent 后继 Attempt 记录前驱。 |
| 外部审批 | 本地 HTTP 请求通过实际路由、审批服务、协调器和 ToolGuard，恢复一次真实工具执行并保存签名正文引用。覆盖未授权提交和重复提交。Console 通道传输使用本地受控适配器。 |
| Console | Vitest 82 项通过。Chrome 158.0.8127.88 和 Chromium 108.0.5359.29 实测 1280、1440 宽度下的 `hideMenu=true` 总览与键盘交互，API 响应使用受控数据。 |
| 静态检查 | 每个功能提交通过仓库 pre-commit 检查。 |

上下文压缩和 Checkpoint 详情追加验证。AttemptView 定向测试 11 项通过，TypeScript、目标 ESLint 和生产构建通过。Chrome 158.0.8127.88 和 Chromium 108.0.5359.29 使用受控 API 数据，在 1280 和 1440 宽度的 `hideMenu=true` 页面通过键盘选择，验证压缩策略、零值、缺测值和完整 Checkpoint ID。

## 目标 MySQL 验收

在用户提供的 MySQL 8.0.46 实例上使用 TLS 连接。服务器使用 16 KiB InnoDB 页和严格 SQL 模式。独立测试库成功执行五张基础表、两张恢复表与 ACK 索引的 DDL。完整运行 `tests/integrated/observability` 和 `tests/integrated/conversation_storage`，共 **74 项通过，0 失败，0 跳过**，耗时 17 分 10 秒。覆盖模型请求使用量、重试与取消、乱序投影、正文证据恢复、租约和 ACK、会话存储、审批审计。结束后删除本轮创建的两个测试库，并核验残留为 0。

## 现场待验收

目标集群内应用到 MySQL 的连接、共享 NAS、实际日志采集器、正式外部审批通道和现场 360 Chromium 108.0.5359.95 待实测。现场还需验证采集入库和 ACK、签名正文读取、数据库断连与实例重启、租约接管，以及真实外部审批触发工具恢复。 [部署验收](../../docs/deployment/agent-observability-operations.md#工作区路径或-authority-离线变更)
