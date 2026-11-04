# Tracing MySQL/NAS 历史读取修复

日期：2026-11-04。工作树：`.worktrees/conversation-mysql-nas`。
基线：`099ecaeb847dd174a7f2e5fce0739775745dcdb6`。

## 修复行为

SQL Tracing 详情使用目标 workspace 的 `HistoryStore.current`，返回 memory/task/error、适用 anchor、目标 ChatSpec、SQL 状态及归档边界，复用普通 Chat 的隐藏上下文过滤、审批覆盖和读取保护。内部 `tracing_view` 保留原有含义。

初次 metadata 查询和后续 current/archive reader 共用 chat ID、目标 user、source 和 agent 对象校验。metadata 缺失的历史记录保持原兼容规则；SQL 查询的可信 WorkspaceScope 仍负责存储范围隔离。首次 hydrate 及正文读取故障经过现有 404/503 映射，归档 cursor/参数错误保持 422；不吞取消，不回退文件读取。

生产改动仅在 `src/swe/app/routers/tracing.py`。未修改 schema、publication、NAS bundle、迁移程序、checkpoint、compact 或列表接口。未修改前端生产组件。

## 失败复现与回归

修复前先增加测试并观察失败：实际 HistoryStore 读取链路仅返回 answer，缺少 question/error；SQL 分派仍调用旧视图；后续 source/agent 校验缺失；首次 hydrate 故障返回 500。

修复后的实际读取器组合测试使用真实 `_snapshot/_assemble/_convert`，为数据库/NAS IO 和 pin 清理边界提供替身，不冒充真实 SQL。测试比较普通 Chat 与 Tracing 的完整响应，覆盖 memory/task/error/anchor、Chat metadata、归档边界及 hidden-context；并检查成功、对象拒绝、NAS 错误、最终 revision 冲突和取消时调用清理。

额外测试覆盖初次/reader 两阶段 chat/user/source/agent 拒绝，存储/MySQL/OSError 错误映射。现有 retention 测试继续验证 NAS worker 取消排空和 pin 释放。前端沿用真实 converter，验证详情驱动分页、页末停止，以及目标 Chat name/meta 对任务结果卡片的转换。

真实 MySQL HTTP 用例已调整为当前完整历史语义，新增两页真实归档数据，并与普通 Chat 响应对照。直接 store.tracing_view 的旧语义测试保留。

## 验证记录

- 修复与相关读取保护：111 passed、64 skipped。命令包含 Tracing 两个路由测试文件、HistoryStore、retention、chat repository，以及两个 MySQL 集成测试文件。
- 跳过的 64 项需要显式 `SWE_TEST_DB_HOST` / `SWE_TEST_DB_NAME` 专用隔离测试库；当前未配置，不构成真实 MySQL 验收证据。
- ReadOnlySessionChat 三个测试文件：17 passed；新增测试文件 ESLint、Prettier 校验通过。
- 扩大检查 `tests/unit/routers tests/unit/app/conversation_storage`：1254 passed、10 failed。6 项 Console 测试替身缺少 current_identity，1 项 Cron router 注册断言，1 项静态资源返回 404；仅在测试进程加载原始 Tracing 路由后，这 8 项同样失败。另 2 项因当前环境缺少 swe_async_tasks 表失败，路径不经过本次路由；未修改数据库来绕过。
- 本次文件的 pre-commit 检查全部通过，包含 TypeScript typecheck、mypy、black、flake8、pylint 及基础文件检查；最终重跑仍为后端 111 passed / 64 skipped、前端 17 passed。

## 尚需环境验收

使用专用隔离 MySQL 执行 test_api_mysql.py / test_history_mysql.py，并在测试页面打开有归档的目标会话，确认详情后发起 history 请求、分页停止、失败记录可见、越权 404 和 NAS 故障 503。本报告随修复代码一并提交；未启动生产服务或部署。

本次修复不解决 checkpoint 的 4096 引用边界、compact 容量契约或列表 NAS hydrate；这些仍应按独立问题推进。
