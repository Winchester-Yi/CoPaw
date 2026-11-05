# Chat task source snapshot Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in the existing conversation-mysql-nas worktree. Execute inline, task-by-task with failing tests first; do not integrate unrelated dirty files.

**Goal:** 会话分页不再读取或等待任务源，任务区域共用当前身份快照，保留原接口与动作语义。

**Architecture:** 独立来源 loader + 当前身份共享协调器；SessionApi 保留未过滤的已加载缓存，统一投影视图；React 生命周期维护唯一任务刷新入口并同步共享会话视图。

**Tech Stack:** React 18、TypeScript、现有 fetch/API adapters、Vitest。

设计：[任务来源解耦](../specs/2026-11-05-chat-task-source-snapshot-design.md)。原 HTTP 接口、参数、成功/错误响应以及旧 API 方法保持兼容；轻量服务端状态接口单独新增，不在本包替换旧端点。

## Task 1: 独立来源读取与取消

Files: `console/src/api/modules/{cronTaskSources,cronjob,schedulerCron}.ts` 及相邻测试。

- [x] 新增读取测试，证明 legacy 显式捕获 context/signal，scheduler 取消后不能启动下一页；旧无参方法和聚合导出保持兼容。
- [x] 运行失败测试：`pnpm --dir console test:run src/api/modules/cronTaskSources.test.ts src/api/modules/schedulerCron.test.ts`。
- [x] 增加独立 `loadTaskSource(owner, context, signal, accessKey)`，沿用 tagging/managed 身份校验；新增 frontend 查询方法 `listCronJobsForContext`，旧 `listCronJobs()` 不变。

```ts
type TaskOwner = "legacy" | "scheduler";
// 返回完整成功的单来源数组，失败抛错，不能交付部分页。
loadTaskSource(owner, context, signal, accessKey): Promise<SourcedCronTask[]>;
```

- [x] 同命令转绿；检查两套旧路由与新调度器写方法没有改协议。

## Task 2: 当前身份任务协调器

Files: 新建 `console/src/stores/chatTaskSourceStore.ts`、`chatTaskSourceStore.test.ts`。

- [x] 先测独立来源发布、同轮合并、失败保留展示但 membership=null、合法空集合、动作前响应不覆盖、A→B→A、隐藏暂停与恢复。

```ts
const pending = store.refresh("legacy");
const duplicate = store.refresh("legacy");
await Promise.all([pending, duplicate]);
expect(legacyRequests).toBe(1);
// 请求边界替身只代替 HTTP，实际协调器和过滤逻辑参与测试。
```

- [x] 运行 `pnpm --dir console test:run src/stores/chatTaskSourceStore.test.ts`，确认功能缺失导致失败。
- [x] 实现 `activate(context,key)`、`retain(context,key)`、`refresh(owner?,reason?)`、`beginMutation(owner,key)`、`setVisible`、同步 `presence(key)` 与稳定 `subscribe/getSnapshot`。
- [x] 每来源保留 items/optimistic override/membership/error/loading；epoch 隔离身份，来源 revision 隔离动作，单个在途 Promise 合并，动作后确认不复用动作前读取。乐观展示不发布 membership。
- [x] 自动观察 user/source/agent/角色上下文，切换只重置不隐式 IO；任务生命周期才开始读取。退出/认证清空时使快照失效，不持久化任务或凭据。
- [x] 上述测试转绿，所有异步分支都必须检查 epoch/revision，来源失败独立发布。

## Task 3: SessionApi 去除任务 IO，缓存与视图分离

Files: `console/src/pages/Chat/sessionApi/index.ts`、`index.test.ts`、现有 `taskSessions.ts`。

- [x] 新增失败测试：任务 HTTP Promise 永不完成，summary 仍可返回首屏与下一页；成功空集合隐藏任务，失败转 unknown 后从已加载缓存恢复，且 summary 调用数不变。

```ts
await api.getSessionList();
expect(cronJobApi.listCronJobs).not.toHaveBeenCalled();
expect(schedulerCronApi.listAll).not.toHaveBeenCalled();
const visible = api.getLoadedSessionView(); // 同步、零网络
```

- [x] 先运行 `pnpm --dir console test:run src/pages/Chat/sessionApi/index.test.ts` 观察失败，再移除分页中的 `listTaskSources`。
- [x] 一个原始 sessionList 缓存，`getLoadedSessionView()` 和 `projectSessionView(sessions)` 同步读取 membership；所有公开返回列表的路径使用该投影。不要过滤后覆盖原始缓存。
- [x] 保留旧页/草稿/详情 metadata 合并、cursor/has_more、临时 ID 和生成状态；旧存在性测试改为显式建立共享快照，不能再隐式假设分页会读取任务。
- [x] 运行 SessionApi、taskSessions、首屏/加载更多侧栏回归转绿。

## Task 4: Chat 生命周期、动作与共享会话桥接

Files: `pages/Chat/useChatTaskSources.ts`、`useChatTaskSources.test.tsx`、`index.tsx`、`index.test.tsx`、`useManagedTaskActions.ts`、`useTaskReadOnOpen.ts`、`components/ChatSidebar/index.tsx` 及相邻测试。

- [x] 写失败测试：两个 hook 共享一轮；一个来源悬挂另一来源仍展示；隐藏不轮询；恢复只一次；乐观删除失败保留 membership unknown 和旧展示；任务变化重投影不触发 summary。
- [x] `useChatTaskSources` 用共享状态订阅；唯一 lifecycle owner 处理初始/visibility/taskCreated/30 秒可见轮询。普通订阅者不注册计时器。保留旧事件。
- [x] Chat 任务动作开始调用 `beginMutation`，在 finally 中确认对应来源；新协议受理结果不能变成已删除；已读同样指定来源。保留原 request_id、版本和失败处理。
- [x] 乐观 setter 只作用于展示 override，确认后清除；旧 managed hook 调用方可继续使用原签名（新增回调为可选）。
- [x] Chat 根部订阅 membership，通过 SessionApi 内存缓存与共享消息合并重投影；Sidebar 的 currentOnly 合并之后再最终过滤，抽屉复用共享结果。
- [x] Task ID 导航区分 loading/完整空/失败，未知来源不能据旧快照自动导航；当前正文不因任务查询失败清空或跳转。
- [x] 运行任务 hook、动作、已读、Chat 页、侧栏/抽屉相关回归，确认原 API 参数与 route 测试仍通过。

## Task 5: 验证、文档与交付

- [x] 运行上述 Vitest，扩展到已有任务来源/任务卡、scheduler API 与上一 summary 工作包；再执行 typecheck/build 及受影响文件 ESLint/format。
- [x] 执行相关 Python 原接口兼容回归（不接业务数据库），`git diff --check`，文档相对链接检查。
- [ ] 真实 Chromium 108/360、当前浏览器与 hideMenu=true 按可用性验收；缺环境明确记录，不能用构建替代。
- [x] 更新设计状态和入口 playbook，记录结果、剩余可见运行状态每轮 L+S 成本、无实际 NAS/部署延迟验收。
- [x] 只交付本工作包；保留 dev.json、历史上线操作文档等无关变更；没有新的提交指令时不自动提交代码。

## 本轮执行结果

代码已实施，随本工作包提交；真实浏览器和部署验收未完成。相关前端 538 passed / 2 skipped（两项修改前即可复现的旧断言），后端兼容回归 58 passed，typecheck/build 通过。扩大运行另见已有/无关失败，不能宣称全仓测试或 lint 全绿。

补充实现：认证退出暂停直到 ready；创建事件废弃旧来源读取；显式删除防复活；消息引用保留；会话身份索引合并消除平方级扫描。详见[实现与验证报告](../reports/2026-11-05-chat-task-source-snapshot-implementation.md)。
