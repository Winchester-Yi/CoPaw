# 会话分页与任务来源解耦：实现和验证

日期：2026-11-05，Asia/Shanghai。工作区：`conversation-mysql-nas`。

本包保留 schema、索引和持久化格式；保留原 HTTP 路径、参数、响应、分页和错误协议。代码随本工作包提交。它是[整体性能改造](../specs/2026-11-04-conversation-storage-performance-redesign.md)的一部分，不能视为整个迁移版本已经完成性能验收。

## 实现结果

- SessionApi 首屏、下一页和标题操作不再查询或等待任务来源。任务存在性只用于同步内存投影；未知与失败不解释为任务删除。
- 任务来源由当前身份的共享协调器读取，legacy 与 scheduler 独立发布；同来源在途读取合并，scheduler 必须完整遍历才能发布完整集合。
- 页面可见时保留既有 30 秒运行状态轮询，由一个生命周期维护；隐藏时停止新请求并取消在途读取，返回时每来源只启动一轮。
- 任务动作和已读在开始时使对应来源存在性失效，完成后只确认对应来源；动作前的读取不能覆盖动作后的结果。创建事件也使旧在途清单失效。
- user/source/agent/角色、A→B→A 和认证清除均隔离旧响应；认证清除后上下文变化不能重新启动查询，重新认证后才恢复。普通 token 续期不清空相同身份快照。
- 已加载缓存和可见视图分开，隐藏后转 unknown 可从内存恢复。UI 合并之后再次投影，保留消息和生成状态；显式删除的会话不能被旧 UI 或迟到页复活。
- 合并已加载会话改为身份索引，保持最早别名匹配规则，复杂度从 O(N×M) 改为 O(N+M)，避免每次任务快照发布重复扫描长列表。

## 兼容边界

本包没有修改后端运行时代码，没有替换 HTTP 端点。旧 `cronJobApi.listCronJobs()` 无参调用和 `listTaskSources` 聚合导出保留；新增 `listCronJobsForContext(context, signal)` 仅是前端查询适配方法，仍调用原 GET `/cron/jobs`，用于捕获异步请求身份和取消信号。

沿用此前独立的 `/chats/-/summaries`、`/tracing/chats/-/summaries`；旧列表、详情、完整 metadata、页码和 v1/v2 cursor 保留。Managed actions / mark-read hook 新回调为可选，旧调用签名仍可使用。未来轻量任务状态和单对象 Session→Chat 映射应新增接口，前端单独迁移，不能缩减旧接口响应。

## 验证证据

已观察到失败再修复的覆盖包括：独立来源读取、scheduler 取消阻止下一页、任务源悬挂不阻塞会话、共享刷新与身份隔离、动作失效、隐藏恢复、认证清除后上下文变化、显式删除、消息恢复和长列表成本。

- 前端相关回归：**538 passed、2 skipped**，59 个文件。运行 `pnpm --dir console test:run src/pages/Chat src/api/modules/cronTaskSources.test.ts src/api/modules/cronjob.query.test.ts src/api/modules/schedulerCron.test.ts src/api/authClear.test.ts src/stores/chatTaskSourceStore.test.ts`，附加下述已有失败的排除条件。
- 后端兼容回归：**58 passed**。使用项目虚拟环境运行 `test_summaries.py`、`test_legacy_contracts.py`、storage `test_context_usage.py`、`test_chat_api_update.py`、`test_tracing_chats_api.py`。这些使用测试替身，不代表真实 MySQL/NAS 验收。
- TypeScript typecheck 和生产构建通过；构建保留已有大 chunk 提示。
- 核心受影响实现与新增测试 ESLint 通过；已格式化本次编辑区及新增文件，`git diff --check` 通过。没有宣称全仓 lint/format 通过。
- 3,000 条会话的实际合并测试中，旧实现访问身份字段 9,009,000 次，超过线性预算而失败；索引实现通过 `< 50×N` 访问量检查。这是算法成本证据，不是部署耗时测试。

### 扩大回归发现的已有或无关失败

未排除的首次扩大运行结果为 **558 passed、8 failed**，60 个文件；增加索引测试后的相关成功结果如上。没有修改这些失败来凑全绿：

1. `Chat/index.test.tsx` 两项 ContextUsageIndicator 旧断言（立即存在/尚无数据时仍显示按钮）。临时换回 HEAD 的 Chat 实现后两项同样失败，并已恢复当前实现。本包相关回归使用负向名称过滤，明确记为 2 skipped。
2. 未改动的 `runtimeMessageHeader.test.tsx` 中助手名称的四项断言失败。
3. 未改动的 FileManager 测试在扩大运行中出现首个打开用例超时、删除弹窗未及时消失；独立复跑仍有删除弹窗失败。相关成功回归排除了这两个无关测试文件，不将它们计为通过。

已知 lint 基线也单独保留：`externalToken.ts` 的未使用 `WindowEnv`、`useManagedTaskActions.test.tsx` 的未使用 `waitFor`、SessionApi 和 Chat 大测试中的已有 `any`，以及 Chat 根组件已有 hooks 依赖警告。本包没有清理无关代码；新增核心文件无 lint 警告。

## 尚未完成的部署验收与剩余成本

- 未进行真实 Chromium 108/360 与当前浏览器的完整 UI 验收，包含侧栏、抽屉、身份切换和 `hideMenu=true`；没有用 jsdom、构建或修改 User-Agent 替代真实浏览器验收。
- 未连接专用 MySQL/NAS 或进行多实例、真实部署延迟与系统调用测量，不能给出毫秒、吞吐或整个侧栏零 NAS 的结论。
- 既有可见页面轮询每轮仍需要 legacy 全量清单 L 和 scheduler 全页 S。消除的是分页、重复消费者和重复生命周期造成的额外 L+S；后续需单独设计轻量批量运行状态接口。
- Tracing 的 Session→Chat 映射仍可遍历摘要页，单对象 SQL 查询尚未实施。checkpoint、plan 容量、正文批量 NAS IO 等不属于本包改动。

入口详见[设计](../specs/2026-11-05-chat-task-source-snapshot-design.md)和[实施计划](../plans/2026-11-05-chat-task-source-snapshot-plan.md)。
