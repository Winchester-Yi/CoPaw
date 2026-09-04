# Chat 性能优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不增加持久化索引文件、不迁移 MySQL 且不改变完整 Chat History 默认 API 契约的前提下，减少 Chat 热点路径中的重复文件 I/O、锁竞争和重复状态组装。

**Architecture:** Chat Record 继续以 `chats.json` 为事实来源，Repository 维护可失效的进程内不可变快照。读路径从快照读取，写路径仍通过现有文件锁和原子替换序列化。Answer Turn、History 和 Console 启动路径仅复用既有 `turn_states`、请求上下文和 Workspace Skill Snapshot，不新增任何持久化索引。

**Tech Stack:** Python 3, asyncio, pytest, Pydantic, JSON 文件存储。

---

## 文件结构

- 修改：`src/swe/app/runner/repo/json_repo.py`：低成本文件签名、快照读取与索引访问。
- 修改：`src/swe/app/runner/repo/base.py`：基于 repository 快照的过滤和分页入口。
- 修改：`src/swe/app/runner/manager.py`：读写锁拆分和 Chat 查询委托。
- 修改：`src/swe/app/answer_turn/coordinator.py`：批量运行状态读取。
- 修改：`src/swe/app/runner/api.py`：列表状态批量组装、Answer Turn 单次读取、批量审批状态和 History state 复用。
- 修改：`src/swe/app/approvals/service.py`：批量审批读取与低噪声诊断日志。
- 修改：`src/swe/agents/memory/conversation_archive.py`：分页单次扫描判断 `has_more`。
- 修改：`src/swe/app/routers/console.py`、`src/swe/app/runner/runner.py`、`src/swe/app/runner/query_runtime.py`：请求内 Chat/启动快照复用。
- 测试：对应 `tests/unit/app/`、`tests/unit/agents/` 的既有测试模块；新增测试仅放在拥有被测行为的现有模块附近。

### Task 1: Chat Repository 快照读取与读写锁拆分

**Files:**
- Modify: `src/swe/app/runner/repo/json_repo.py`
- Modify: `src/swe/app/runner/repo/base.py`
- Modify: `src/swe/app/runner/manager.py`
- Test: `tests/unit/app/test_json_chat_repository.py`（若现有测试分散，复用其实际模块）
- Test: `tests/unit/app/test_chat_manager.py`

- [ ] **Step 1: 写入失败测试，定义有效快照不得读取文件内容**

测试通过 monkeypatch `_read_file_state` 或文件读取函数，建立已安装快照后验证 `get_chat`、`filter_chats` 和 `get_chat_id_by_session` 仅做元数据签名检查，不调用 JSON 解码；同时验证写入后下一次读取得到更新的 Chat。

- [ ] **Step 2: 运行新测试并确认因缺少快照读路径失败**

Run: `venv/bin/python -m pytest tests/unit/app/test_json_chat_repository.py tests/unit/app/test_chat_manager.py -q`

Expected: FAIL，失败原因是测试期待的 snapshot-only 读取或无锁读路径尚未存在。

- [ ] **Step 3: 实现最小快照访问 API**

在 `JsonChatRepository` 中分离轻量签名检查与完整读取；有效签名仅使用 `exists`、`mtime_ns`、`ctime_ns`、`size`、`inode`，不读取内容和计算 digest。增加或覆写仅从快照返回深拷贝的 `get_chat`、`filter_chats`、按逻辑会话查询及分页支持；失效、缺失或不稳定读取时回退 `load()`。保留 JSON 全量重写、文件锁和原子 compare-and-set。

- [ ] **Step 4: 让 `ChatManager` 仅在写路径持有全局锁**

使 `list_chats`、`list_chats_page`、`list_chats_cursor`、`get_chat` 和 `get_chat_by_session` 直接调用 repository 的一致快照读接口。`get_or_create_chat`、场景 Chat、更新、删除仍持有写锁；不得以“最新 Chat”替代 legacy session 查找。

- [ ] **Step 5: 运行针对性测试并确认通过**

Run: `venv/bin/python -m pytest tests/unit/app/test_json_chat_repository.py tests/unit/app/test_chat_manager.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/swe/app/runner/repo/json_repo.py src/swe/app/runner/repo/base.py src/swe/app/runner/manager.py tests/unit/app/
git commit -m "perf(chat): reuse repository snapshots for reads"
```

### Task 2: Chat 列表批量运行状态

**Files:**
- Modify: `src/swe/app/answer_turn/coordinator.py`
- Modify: `src/swe/app/runner/api.py`
- Test: `tests/unit/app/test_answer_turn_coordinator.py`
- Test: `tests/unit/app/test_chat_api.py`

- [ ] **Step 1: 写入失败测试，定义批量状态与列表接口行为**

测试 `statuses([chat_a, chat_b, missing])` 返回活跃 Chat 的当前 `TurnStatus` 与 missing 的 `None`。测试 `/chats` 对一个分页结果只调用一次批量状态 API，且仍返回 `idle`、`running`、`stopping` 的既有字符串。

- [ ] **Step 2: 运行测试并确认失败**

Run: `venv/bin/python -m pytest tests/unit/app/test_answer_turn_coordinator.py tests/unit/app/test_chat_api.py -q`

Expected: FAIL，缺少 `statuses` 或列表仍逐项调用 `status`。

- [ ] **Step 3: 实现批量状态快照**

在 coordinator 中一次取得 `_global_lock` 后读取目标 Chat 的 `_TurnState`，返回不暴露可变 `_TurnState` 的状态映射。列表 API 先获取页面/列表 items，再一次请求状态映射；Coordinator 缺失时维持所有项 `idle`。

- [ ] **Step 4: 运行针对性测试并确认通过**

Run: `venv/bin/python -m pytest tests/unit/app/test_answer_turn_coordinator.py tests/unit/app/test_chat_api.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/swe/app/answer_turn/coordinator.py src/swe/app/runner/api.py tests/unit/app/
git commit -m "perf(chat): batch list turn status lookups"
```

### Task 3: Answer Turn 请求内 state/history 复用

**Files:**
- Modify: `src/swe/app/runner/api.py`
- Test: `tests/unit/app/test_chat_api.py`
- Test: `tests/unit/app/test_session_title_generation.py`（若其覆盖 Answer Turn 兼容性）

- [ ] **Step 1: 写入失败测试，覆盖候选和旧数据回退**

测试带相同 `sessionid` 的多个 Chat：`turn_states[msgid].chat_id` 明确不匹配的候选不构建 History；命中候选只读取一次 state、构建一次 History；无 `turn_states` 的旧记录仍按现有完整 History 语义找到 Answer Turn。

- [ ] **Step 2: 运行测试并确认失败**

Run: `venv/bin/python -m pytest tests/unit/app/test_chat_api.py -q`

Expected: FAIL，当前实现对候选和最终结果会重复读取/构建。

- [ ] **Step 3: 实现一次请求内读取上下文**

为 `_build_chat_history` 增加可选已读取 state 参数，或引入仅在路由内部使用的读取结果容器。先从所有所有权已过滤的 session 候选中检查既有 `turn_states`；对 state 没有明确答案的旧记录才构建 History。最终响应复用命中 candidate 的 state 与 history，保留 `chat_id` 直查和所有 404 语义。

- [ ] **Step 4: 运行针对性测试并确认通过**

Run: `venv/bin/python -m pytest tests/unit/app/test_chat_api.py tests/unit/app/test_session_title_generation.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/swe/app/runner/api.py tests/unit/app/
git commit -m "perf(chat): avoid duplicate answer turn history reads"
```

### Task 4: History 批量审批状态和归档单次扫描

**Files:**
- Modify: `src/swe/app/approvals/service.py`
- Modify: `src/swe/app/runner/api.py`
- Modify: `src/swe/agents/memory/conversation_archive.py`
- Test: `tests/unit/app/test_approvals_service.py`
- Test: `tests/unit/app/test_chat_api.py`
- Test: `tests/unit/agents/test_conversation_archive.py`

- [ ] **Step 1: 写入失败测试，定义审批批量查询**

测试 `ApprovalService.get_requests(request_ids)` 在一次锁范围返回同 scope 的已知记录，忽略重复 ID、未知 ID 和跨 scope ID。测试 History 组装对多个审批卡只调用一次批量方法，且每条已有卡获得原有 `status`。

- [ ] **Step 2: 写入失败测试，定义归档分页只扫描一次**

建立多 boundary 归档，包装 `_read_batch` 计数，验证第一页 `has_more` 计算不会为了探测更旧消息重读已经读取的 batch；继续验证既有 cursor、timeline 顺序和损坏 batch 跳过行为。

- [ ] **Step 3: 运行测试并确认失败**

Run: `venv/bin/python -m pytest tests/unit/app/test_approvals_service.py tests/unit/app/test_chat_api.py tests/unit/agents/test_conversation_archive.py -q`

Expected: FAIL，缺少批量审批 API，或归档计数显示重复扫描。

- [ ] **Step 4: 实现最小批量读取和单次归档扫描**

审批服务以一次锁读取请求集合，不在常规成功路径生成完整 inventory 日志；保留诊断接口和 scope 可见性。History 先收集 request ID，再批量赋值。归档选择函数返回已选择项及“是否存在更旧有效消息”的事实，`read_page` 直接使用该事实计算 `has_more`，不再二次调用 `_select_page`。不得更改 cursor 编码、归档锁或边界一致性校验。

- [ ] **Step 5: 运行针对性测试并确认通过**

Run: `venv/bin/python -m pytest tests/unit/app/test_approvals_service.py tests/unit/app/test_chat_api.py tests/unit/agents/test_conversation_archive.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/swe/app/approvals/service.py src/swe/app/runner/api.py src/swe/agents/memory/conversation_archive.py tests/unit/
git commit -m "perf(chat): batch history status reads"
```

### Task 5: Console 启动请求复用和 Workspace Skill Snapshot 复用

**Files:**
- Modify: `src/swe/app/routers/console.py`
- Modify: `src/swe/app/runner/runner.py`
- Modify: `src/swe/app/runner/query_runtime.py`
- Test: `tests/unit/routers/test_console_chat_stream.py`
- Test: `tests/unit/app/test_runner_hook_runtime.py`
- Test: `tests/unit/app/test_runner_query_boundaries.py`

- [ ] **Step 1: 写入失败测试，定义请求内 Chat 复用**

测试 Console 普通新请求创建/解析一个 Chat 后，Runner runtime 使用请求中可信的 Chat identity，不再次调用 `get_or_create_chat`。测试 Chat 不存在、身份不匹配、场景 Chat 和 W+ 入口均回退既有解析/原子创建流程。

- [ ] **Step 2: 写入失败测试，定义 Query 内快照复用与失效**

测试一个 Query 的预检、runtime input 和最终 agent 构建复用相同已验证 Workspace Skill Snapshot；测试 manifest stat 变化时下一 Query 重新加载，且当前 Query 仍保持其启动快照。

- [ ] **Step 3: 运行测试并确认失败**

Run: `venv/bin/python -m pytest tests/unit/routers/test_console_chat_stream.py tests/unit/app/test_runner_hook_runtime.py tests/unit/app/test_runner_query_boundaries.py -q`

Expected: FAIL，当前路径重复解析 Chat 或重复验证快照。

- [ ] **Step 4: 实现请求级复用**

使用已有 `AgentRequest.channel_meta` 或专用内部 request-scoped runtime 字段承载已授权的 Chat ID，避免把可伪造客户端字段作为可信对象。运行时仅在 Chat ID 可由当前 workspace 重新验证且身份匹配时复用；否则回退 `get_or_create_chat`。W+ 查找仅在相关请求字段存在时执行。场景创建继续使用已有原子方法和失败清理。

- [ ] **Step 5: 实现 Query Skill Snapshot 复用**

将本 Query 已验证快照放入 `_QueryRuntimeInputs` 并传递到后续阶段；只在最终创建 Agent 前做轻量 freshness 确认。跨 Query 缓存必须以 manifest stat 为 key；任何已检测到的变化都使下一 Query 重新加载。不得改变 Query Skill Snapshot 的 fail-closed 规则。

- [ ] **Step 6: 运行针对性测试并确认通过**

Run: `venv/bin/python -m pytest tests/unit/routers/test_console_chat_stream.py tests/unit/app/test_runner_hook_runtime.py tests/unit/app/test_runner_query_boundaries.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/swe/app/routers/console.py src/swe/app/runner/runner.py src/swe/app/runner/query_runtime.py tests/unit/
git commit -m "perf(chat): reuse console startup state"
```

### Task 6: 全量回归和文档验收

**Files:**
- Modify: `docs/plans/2026-09-04-chat-performance-optimization-report.md`
- Test: `tests/unit/app/`
- Test: `tests/unit/routers/`
- Test: `tests/unit/agents/`

- [ ] **Step 1: 运行所有受影响测试**

Run: `venv/bin/python -m pytest tests/unit/app/ tests/unit/routers/ tests/unit/agents/ -q`

Expected: PASS。

- [ ] **Step 2: 运行静态和格式检查**

Run: `pre-commit run --files src/swe/app/runner/repo/json_repo.py src/swe/app/runner/repo/base.py src/swe/app/runner/manager.py src/swe/app/answer_turn/coordinator.py src/swe/app/runner/api.py src/swe/app/approvals/service.py src/swe/agents/memory/conversation_archive.py src/swe/app/routers/console.py src/swe/app/runner/runner.py src/swe/app/runner/query_runtime.py`

Expected: PASS。

- [ ] **Step 3: 更新报告的实施状态并提交**

将报告中的方案保留为设计记录，并新增简短的实现/测试结果段落，准确写入执行命令和通过情况。不得将未执行的性能数字表述为测量结果。

- [ ] **Step 4: 提交**

```bash
git add docs/plans/2026-09-04-chat-performance-optimization-report.md
git commit -m "docs: record chat performance optimization validation"
```

## 自检

- 五个报告优化点分别由 Task 1–5 覆盖。
- 未引入 MySQL、SQLite、额外索引文件或详情 API 分页改造。
- 每个生产变更都要求先有失败测试和针对性通过测试。
- 每个功能任务都有独立提交；完成后需要 GitNexus 变更分析和独立代码检视。
