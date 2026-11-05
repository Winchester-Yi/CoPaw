# 会话 MySQL/NAS 迁移分支合并影响审计

审计日期：2026-11-04（Asia/Shanghai）。只分析、复现和验证，没有修改实现或操作生产数据。

## 范围与结论

目标工作树：`/Users/shixiangyi/code/Swe/.worktrees/conversation-mysql-nas`。
合并提交：`099ecaeb847dd174a7f2e5fce0739775745dcdb6`。
迁移分支原基线：`436a105056ecb9ca3c692958a0b914859584c2aa`。
合入分支：`51a90a4d3442277a32fb20af5e34174e6b7b9c45`。
共同祖先：`5c8e96e5c3645457b66a72a4a52d8bd1b1921af0`。

没有发现本次合并直接改写会话迁移的数据格式、SQL authority、NAS publication/CAS、执行权续约或 GC 协议；这些核心实现相对合并前未变。发现两项新功能与旧 SQL reader 的集成缺口，集中在运营看板历史展示，不能据此声称 NAS 数据丢失。

结论不代表生产验收：真实 MySQL 及跨节点 NAS 未在本轮验证。

## 已确认影响一：运营看板无法继续读取归档（P1）

触发：启用 MySQL authority，Chat 已有 compact 归档，通过运营看板打开用户聊天。

调用链：

`ReadOnlySessionChat -> GET /tracing/chats/{id} -> indexed.tracing_view -> _snapshot(layers=(memory,task))`。

`history.py:297` 仅在 layers=None 时读取归档 boundary；tracing_view 指定 layers，因此 snapshot.boundaries 永远为空。`history.py:497` 再按 boundaries 生成 archive.has_more，结果恒为 false。

新前端 `ReadOnlySessionChat.tsx:213` 用该值初始化 hasMore，`:306` 在 hasMore=false 时直接拒绝 loadEarlier。因此新合入的 `/tracing/chats/{id}/history` 虽已接通 SQL archive_page，正常页面流程不会调用它。

复现：使用真实 HistoryStore._snapshot/_assemble/_convert，SQL/NAS IO 使用隔离桩，同一组数据：current 返回 has_more=true、1 个 boundary；tracing 返回 has_more=false、0 个 boundary。

影响：运营看板缺少较早的归档消息与压缩边界；聊天主页面及数据库/NAS 数据本身不因该问题丢失。

## 已确认影响二：运营看板与聊天页的消息/metadata 契约不一致（P2）

新上游功能意图是运营看板和聊天页共用完整历史展示。但合并后的 SQL 分支仍调用旧 tracing_view：

- `history.py:518` 只选 memory/task，排除 error 和 anchor。
- anchor 可承载已持久准入、尚未进入 memory 的用户问题；error 可承载失败记录。
- `history.py:502` 在 tracing 模式返回 chat=None，而新前端使用 history.chat.meta/name 转换会话消息。任务元信息/名称缺省是附带兼容风险；本轮没有声称已单独复现所有任务卡片差异。
- tracing 模式还跳过聊天页的 hidden-context redaction，说明两种投影不能只凭名称当作等价。

隔离复现结果：current 返回 answer、question、failure，tracing 仅返回 answer。过滤行为在迁移分支原基线已存在；新增影响来自本次引入的新看板契约与它不兼容，不是本次凭空删除了存储记录。

现有真实 MySQL API 测试 `test_api_mysql.py:764` 还明确断言只返回 approval/task、chat=None、has_more=false。该断言与上游的新完整历史要求相冲突，必须同步调整。此前合并新增的两个测试只验证调用 tracing_view 及 archive_page，没有验证真实 reader 产生的 has_more/完整消息内容，因此没有发现这两个组合问题。

## 推荐解决方案：统一目标用户历史装配
专项修复设计见[运营看板历史读取修复设计](../specs/2026-11-04-tracing-history-parity-design.md)，当前为设计草案，尚未实施。


按上游这次合入的“与聊天页一致”产品行为处理，而非只在前端强行设置 has_more。

1. 保留目标 workspace 解析、user/source/agent 访问校验。
2. 在 SQL tracing 详情分支调用目标 workspace 的 `indexed.current(chat_id, authorize=...)`，继续通过 `_indexed_history_call` 转换存储异常。该路径已有完整可见层、anchor 去重、archive boundary、隐藏上下文过滤和 ChatSpec 返回。
3. authorize 应核对本次请求所需的 user/source/agent 约束，作为 reader hydrate/复核阶段的回调保留；不要直接切到当前操作员的 workspace，也不要仅在读取前做一次鉴权。
4. 保留 HistoryStore._assemble 中按目标 scope 执行审批状态覆盖的 tenant_context，不在操作员 context 里直接装配。
5. 新归档端点继续使用目标 indexed.archive_page。建议同样传入 target authorize，维持读取前后校验；保留现有 scope/chat/epoch/high cursor、pin/hydrate/revalidate 协议与 422/503 映射。
6. 可以保留旧 tracing_view 供明确需要旧投影的内部用途；本次修改不需要重构 publication、schema 或 NAS bundle，也无需重跑迁移或回填数据。

只补 boundaries 可以缓解归档入口，但不能解决 error/anchor/metadata/隐藏上下文语义，故不推荐作为最终方案。

验收需要新增或调整：

- mysql authority 下，用实际 HistoryStore 而非只用 AsyncMock，验证详情 has_more=true 后前端确实请求 archive 页面，直到读取完毕。
- 同一数据集比较 Chat 和 Tracing 的可见消息、failed/error、准入 anchor、重复 ID、task-run 与隐藏上下文结果；metadata 只暴露目标接口允许字段。
- 在操作员与目标用户不同 tenant/source/agent 时验证授权、审批状态覆盖，以及读取中删除/重绑等并发场景。
- 覆盖 cursor 跨 scope、epoch 变更、limit 越界、数据库/NAS 故障，确保明确失败且不回退旧文件。
- 在专用真实 MySQL 测试库及真实 NAS 上跑 HTTP/Runner/迁移回归；纯桩测试不是生产一致性验收。

## 核心链路核对

| 范围 | 本次合并影响 |
| --- | --- |
| conversation_storage 全目录 | 相对第一父提交没有代码变更 |
| 迁移 CLI、迁移控制表、会话业务表 DDL | 没有相关变更，无新增会话存储 schema 迁移要求 |
| runner/api.py、Session adapter、workspace service_factories | 没有相关变更，SQL 注入入口保留 |
| admission.py | storage 非空仍先进入 stream_indexed_query 并 return；新增 span 仅位于后续 legacy 分支 |
| query_attempt.py / runner.py | 增加 Tracing 捕获、取消标记；未改 SQL terminal/publication 顺序，回归通过 |
| Agent/model wrapper | 新增输入/输出采样及流观察，需要生产 SDK/带追踪长流压测；未发现其修改持久化正文的直接证据 |
| managed Cron | 日期/年份和错误日志扩展，未修改会话存储适配；相关回归已纳入 |
| 进程限制/用户机构过滤 | 不改变服务/租户/来源/Agent 的会话 storage key 或 NAS 布局 |

## 上线依赖变化与验证边界

`pyproject.toml:52` 从 AgentTraceSDK>=0.1.9 改为固定 0.2.2，适配层新增导入 OperationName/get_current_span。若只同步源码、镜像仍保留不兼容的旧 SDK，可能在 import 阶段失败，影响包括迁移 CLI 在内的依赖启动链；应在最终镜像中运行现有 `scripts/tracing/verify_agent_trace_sdk.py`。这是部署前提，并非已证明的 SQL/NAS 数据破坏。

本地共享 venv 没有该发行包 metadata；真实 SDK smoke 明确未通过依赖前置检查。pytest 自动加载 tests/fakes/trace_sdk，不能用单测通过替代真实 SDK 验证。

本轮结果：

- 单元/路由/查询/CLI/Tracing/Cron 回归：815 passed，39.62 秒。
- 专项真实 MySQL HTTP/history/Runner/v3 migration：87 skipped（未提供专用 SWE_TEST_DB_HOST/NAME）；没有获得真实数据库验收证据。
- 隔离 HistoryStore 行为探针：复现上述两项差异；SQL/NAS IO 为测试桩，不宣称真实数据库/NAS 复现。
- 工作树原有 3 个修改文件、5 个未跟踪文档保持；未修改业务实现。

证据：

- `/private/tmp/conversation_merge_audit_probe.py`
- `/private/tmp/conversation-merge-audit-regression.xml`
- `/private/tmp/conversation-merge-audit-mysql.xml`
