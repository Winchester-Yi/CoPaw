# Conversation summary query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans in this worktree, task-by-task. Steps use checkbox syntax; no automatic branch integration.

**Goal:** Console 首屏/翻页和 Tracing Chat 映射使用不读 NAS 的轻量查询，旧完整接口保持不变。

**Architecture:** 独立 ChatSummary / ChatSummaryPage 类型；SQL 显式选择身份、标题、时间、运行状态、轻量展示 metadata 和 typed binding，查询 limit+1，不 COUNT、不 pin、不 hydrate。新接口仅接受最近更新 v2 cursor；每次查询仍限定可信 workspace scope、user、channel，可按单 Session 过滤。文件 repository 使用兼容实现。Console 用摘要填充列表，详情中的完整 metadata 覆盖摘要并保留未冲突的本地草稿；Tracing 仅查询选中 Session，保留同 Session 多 Chat 按旧列表顺序最后一个胜出的映射。

**Tech Stack:** Python/FastAPI/MySQL、React/TypeScript、pytest、Vitest。

---

## 已确认约束与字段映射

- 不改 schema、索引或存储格式；列表不保证并发更新后的跨页稳定性。
- Summary 顶层：id、session_id、user_id、channel、name、created_at、updated_at、status。
- `display_metadata`：plan_mode_enabled、goal_mode_enabled、session_title_generated、session_kind、task_job_id、management_owner、creator_user_id、source_id、agent_id；未知内容字段不读 NAS 补齐。
- `expert_binding`：现有 typed revision/expert_id，未设置时 null；不查询专家定义文件。
- 响应 `items/page_size/has_more/next_cursor`，不提供 total/page；page_size 1..200。
- 保留 `/chats` 数组/页码/v1/v2 cursor/完整 metadata 和 `/tracing/chats` 数组；新增 `/chats/-/summaries`、`/tracing/chats/-/summaries`，注册在动态 Chat ID 路由之前。
- Console 数量展示为“已加载”，不能把已加载数量伪装成精确 total；保留失败重试、重复加载合并、任务识别、身份切换隔离。
- 授权是 SQL scope 和请求 user 过滤，摘要不是正文访问凭据；详情仍使用完整授权。Cursor 中的值始终是参数，不能替换 scope。

## Task 1: 后端摘要类型与 SQL 入口

Files:
- Create `src/swe/app/runner/chat_summary.py`：独立模型、展示白名单和文件兼容转换。
- Modify `src/swe/app/runner/repo/base.py`：文件兼容 `list_summaries`。
- Modify `src/swe/app/runner/manager.py`：转发摘要查询。
- Modify `src/swe/app/conversation_storage/chat_repository.py`：显式列投影、SQL keyset 查询。
- Modify `src/swe/app/runner/api.py`, `src/swe/app/routers/tracing.py`：新路由及错误映射。
- Create `tests/unit/app/conversation_storage/test_summaries.py`。

- [x] 写失败测试并运行：实际 MySQL repository 配合 SQL 替身，NAS/pin/full hydrate 一经调用即失败；断言仅一次 fetch_all、LIMIT size+1、无 COUNT/c.*，可信 scope/user/channel/session 参数完整。

```python
async def test_summary_query_uses_one_sql_and_no_nas(tmp_path):
    db = ReadDatabase([chat_row()])
    repo = repository(db, tmp_path)
    repo._hydrate = AsyncMock(side_effect=AssertionError("NAS hydrate"))
    repo.retentions.pin = AsyncMock(side_effect=AssertionError("reader pin"))
    page = await repo.list_summaries(page_size=1, user_id="", channel="console")
    assert len(page.items) == 1
    assert len(db.queries) == 1
    query, params = db.queries[0]
    assert "c.*" not in query and "COUNT(" not in query
    assert params[-1] == 2
    assert "meta" not in page.items[0].model_dump()
```

- [x] 实现 `list_summaries(*, page_size=100, cursor=None, user_id=None, channel=None, session_id=None)`；SQL 路径不通过 `_rows/_hydrate/_count`，运行状态复用 active Turn 的既有 CASE。
- [x] 单元验证空页/末页、相同 timestamp 的 ID tie-break、删除/user/scope 过滤 SQL 与参数、无效/v1 cursor、page_size 范围、typed binding、SQL 轻 metadata 损坏、请求不可越权、SQL 故障 503。兼容文件存储并叠加原运行状态；旧接口回归通过。真实数据库隔离验收仍待专用库。

## Task 2: Console 摘要读取与完整详情合并

Files:
- Modify `console/src/api/types/chat.ts`, `console/src/api/modules/chat.ts`：显式摘要类型与新请求。
- Modify `console/src/pages/Chat/sessionApi/index.ts`：摘要转换、cursor 翻页、详情合并及已加载数量。
- Modify `console/src/pages/Chat/components/ChatSidebar/index.tsx`, `ExpandablePanel/index.tsx`：显示已加载数量。
- Modify `console/src/pages/Chat/sessionApi/index.test.ts`；Create `console/src/api/modules/chat.summaries.test.ts`。

- [x] 测试先行：摘要含 task/plan/goal/typed binding，详情含摘要没有的自定义 metadata；普通 ID、本地时间戳已解析 ID、applyChatSnapshot 都必须使用完整详情，不能丢字段或让旧摘要覆盖后端 false/清空 binding。
- [x] 列表改用 `api.listChatSummaries`，每页只使用服务端 cursor；保留在途去重/身份 generation fence/任务过滤/本地草稿。页面消费 `display_metadata` 和 `expert_binding`，不把摘要作为完整 ChatSpec 写回。
- [x] 数量改成已加载记录数；不再读取旧精确 total。添加/调整前端回归，确认初始/更多/失败重试/深链/工作区切换和完整详情都保持正确。

## Task 3: Tracing 选中 Session 映射

Files:
- Modify `console/src/api/modules/tracing.ts`：增加 `getUserChatSummaries`，保留 getUserChats。
- Modify `console/src/pages/Analytics/BusinessOverview/components/UserDetailModal/index.tsx`：选中 Session 后加载摘要映射，取消旧请求写回；遍历该 Session 的 cursor 页以保留原多 Chat 选择语义。
- Modify corresponding `index.test.tsx`, `console/src/api/modules/tracing.test.ts`。

- [x] 先写失败测试：请求只含目标 user/选中 session；旧请求晚返回不能覆盖新的目标；多页相同 Session 保留旧排序下最后一个 Chat 的选择。
- [x] 实现 Session 过滤、cursor 遍历及 effect 清理；不再在弹窗打开时加载所有完整 Chat。

## Task 4: 验证、文档与交付

- [x] 添加真实 MySQL/NAS metadata 下 summary 零 NAS 回归（`tests/integrated/conversation_storage/test_api_mysql.py`），只连接 SWE_TEST_DB 专用库；本环境未配置，执行结果 skipped。
- [x] 运行项目 pytest 相关单元与旧分页/Tracing/context 回归，前端摘要/API/session/sidebar/modal 回归，typecheck、必要 build 与受影响文件 lint/format；限制见下方验证记录。

```bash
../../venv/bin/python -m pytest tests/unit/app/conversation_storage/test_summaries.py tests/unit/app/conversation_storage/test_chat_repository.py tests/unit/app/test_chat_pagination.py tests/unit/routers/test_tracing_chats_api.py tests/unit/routers/test_tracing_chat_history_parity.py -q
pnpm --dir console test:run src/pages/Chat/sessionApi/index.test.ts src/api/modules/chat.summaries.test.ts src/api/modules/tracing.test.ts src/pages/Analytics/BusinessOverview/components/UserDetailModal/index.test.tsx src/pages/Chat/components/ChatSidebar/index.test.tsx src/pages/Chat/components/ChatSidebar/ExpandablePanel/index.test.tsx
pnpm --dir console typecheck
```

- [x] 同步整体设计和 `analysis/playbook/conversation-storage-entry-inventory.md`；记录实际检查结果、未验证的真实 Chromium 108/360 与 MySQL/NAS 项，不声称已压测。
- [x] 只检查本工作包文件，不改原有环境配置，不自动提交/推送。

## 执行与验证记录（2026-11-05，Asia/Shanghai）

- 后端相关单元回归：**160 passed**，覆盖摘要、旧分页、Tracing、history 和前一个 context-usage 工作包。摘要新增测试独立执行 **19 passed**。
- 前端摘要/API/session/sidebar/modal 和只读历史回归：**9 files / 114 passed**。修正测试类型断言后，SessionApi 再次执行 **48 passed**。
- `pnpm --dir console build` 通过（含 `tsc -b`）；仅有现存的大 chunk 提示。本工作包 8 个 Python 文件 pre-commit 全部通过。
- 其余受影响 TypeScript 文件 ESLint 通过；`sessionApi/index.test.ts` 仍有 4 个原有 `no-explicit-any`，用 HEAD 内容执行同一检查确认基线已有，本工作包未扩大。
- 专用库用例 `-k 'context_with_content_metadata or summaries_keep_content_metadata'`：**2 skipped / 51 deselected**，未配置专用测试库，不能视为真实 MySQL/NAS 验收。未运行真实 Chromium 108/360 和当前浏览器验收，也未做目标部署环境压测或 EXPLAIN。
- 只读审查发现 summary 迟到会覆盖请求期间新加载的详情 metadata。增加两个延迟响应回归并验证先失败，再以请求时 metadata 引用快照保留新详情的 false/清空绑定；复核未发现其他问题。
- 路由采用 `/-/summaries`，增加合法旧 Chat ID `summaries` 仍返回完整详情的回归。没有修改 schema/索引/存储格式；旧完整 API 保留。
