# Chat 热点路径性能改造报告

## 1. 报告范围

本报告针对 `src/swe` 的高频 Chat 读写路径，重点覆盖：

- 历史会话列表：`GET /chats`
- 会话详情：`GET /chats/{chat_id}`
- Answer Turn 查询：`GET /chats/answer-turn`
- Console 发起会话：`POST /console/chat`
- 会话归档和运行态读取

分析基于代码和 GitNexus 调用链，未替代线上 profiling。当前 GitNexus 索引已刷新。

## 2. 已确认约束

- 暂不迁移 MySQL，继续使用现有 JSON、session JSON 和 Chat Record 归档文件。
- 不增加额外索引文件或其他需要独立维护的持久化文件。
- 允许 1–5 秒级进程内缓存；写入后主动失效，跨进程变化通过文件 `stat` 检测。
- 暂不修改会话详情 API 的完整 `ChatHistory` 默认契约，也暂不推进详情分页改造。
- 可复用现有 session JSON 中的 `turn_states`，不新增 Answer Turn 索引结构。
- 可在一次请求内复用已解析的 `Chat Record`。
- 可增加批量运行状态和批量审批状态读取接口。
- `ChatManager` 读路径可以无锁，写路径继续串行；查后创建仍必须保持原子 compare-and-set 和文件锁。

## 3. 主要瓶颈

### 3.1 Chat Record 全量加载、过滤和排序

`BaseChatRepository.filter_chats()` 每次调用 `load()`，完整读取和解析 `chats.json`，之后再在 Python 内存中过滤。分页接口继续对完整结果排序后切片，Cursor 分页也会全量排序。

相关位置：

- `src/swe/app/runner/repo/base.py:146-169`
- `src/swe/app/runner/repo/base.py:183-203`
- `src/swe/app/runner/repo/base.py:282-304`

`JsonChatRepository.get_chat()` 在快照可复用性判断前还会读取文件内容并计算 SHA-256，导致单个 Chat 查询也承担了与文件大小相关的成本。

相关位置：

- `src/swe/app/runner/repo/json_repo.py:78-124`
- `src/swe/app/runner/repo/json_repo.py:363-385`

### 3.2 列表接口逐 Chat 查询运行状态

`GET /chats` 对返回列表中的每个 Chat 顺序调用 `coordinator.status()`。50 条记录会产生 50 次异步调用和 50 次 per-Chat 锁获取。

相关位置：

- `src/swe/app/runner/api.py:937-979`
- `src/swe/app/answer_turn/coordinator.py:182-203`

### 3.3 Answer Turn 查询重复读取和重建历史

当只提供 `sessionid` 时，接口先扫描全部 Console Chat，再对每个候选读取 session state、构建完整历史；找到候选后又重新构建一次历史并再次读取 state。

相关位置：

- `src/swe/app/runner/api.py:1078-1107`
- `src/swe/app/runner/api.py:1114-1130`

这使成本近似为 `O(候选 Chat 数 × 单个历史大小)`。

### 3.4 Chat History 组装存在 N+1 和重复文件扫描

`_build_chat_history()` 会解析完整 session JSON、重建消息、排序、去重、脱敏，并为归档元数据读取归档页。带审批元数据的消息还会逐条调用 `ApprovalService.get_request()`。

相关位置：

- `src/swe/app/runner/api.py:710-792`
- `src/swe/app/runner/api.py:102-131`
- `src/swe/agents/memory/conversation_archive.py:974-1040`

归档分页取完一页后，`_has_previous_message()` 会再次调用 `_select_page()`，可能重复读取同一批 JSONL。

### 3.5 发起会话重复解析 Chat 和技能快照

Console 请求可能在 W+ 拦截、`_start_new_chat()` 和 Agent runtime 装配阶段重复查找或创建 Chat。Query 启动还会多次加载/校验 Workspace Skill Snapshot 和配置。

相关位置：

- `src/swe/app/routers/console.py:1054-1069`
- `src/swe/app/routers/console.py:1184-1192`
- `src/swe/app/runner/runner.py:4514-4521`
- `src/swe/app/runner/query_runtime.py:340-430`

## 4. 优先改造的五项方案

### 4.1 Repository 快照、低成本失效检测和读写锁拆分

**目标**：让 Chat 列表、按 ID 查询和按 session 查询走现有内存快照，避免每次 JSON 解析。

**方案**：

1. 复用 `JsonChatRepository` 已有的 `_snapshot`、`_chat_index` 和 `_session_index`。
2. 快照有效性只检查 `mtime_ns`、`size`、`inode` 等元数据，正常命中路径不计算 SHA-256。
3. `filter_chats()` 在快照有效时直接基于快照过滤，避免再次 `load()`。
4. `paginate_chats()` 和 `paginate_chats_cursor()` 基于快照排序；短 TTL 只用于避免重复 stat/解析，不改变失效检查逻辑。
5. `ChatManager` 的 `list/get/get_by_session` 不再持有全局写锁；写入时通过原子引用切换快照。
6. `create/update/delete` 继续串行；查后创建继续使用 repository 的原子 compare-and-set 和文件锁。

**一致性边界**：本进程写入立即清除缓存；其他进程写入由 `stat` 变化触发失效。缓存未命中或损坏时回退现有全量加载逻辑。

**预期收益**：降低所有 Chat 热点路径的 `O(文件大小)` 读取成本，并消除读请求之间的队头阻塞。

### 4.2 历史列表批量获取运行状态

**目标**：消除 `/chats` 对每个 Chat 的串行 `status()` 调用。

**方案**：

- 为 `AnswerTurnCoordinator` 增加 `statuses(chat_ids)` 或不可变状态快照接口。
- 一次锁操作读取指定 Chat 的状态，列表接口统一组装响应。
- 不对运行状态使用 TTL，继续提供实时 `idle/running/stopping` 语义。
- 对不存在的 Chat 返回 `idle`，保持现有兼容行为。

**预期收益**：将 N 次锁获取和 await 降为一次批量读取，降低列表接口 P95 延迟。

### 4.3 基于现有 `turn_states` 的 Answer Turn 定位

**目标**：消除 `GET /answer-turn` 的重复 state 读取和重复历史构建。

**方案**：

1. 使用 Chat 快照筛选所有匹配 `session_id` 的候选 Chat。
2. 每个候选 session JSON 在请求内只读取一次。
3. 优先检查 `turn_states[msgid].chat_id`；明确不匹配的候选直接跳过。
4. 仅对命中的候选构建一次 `_build_chat_history()`。
5. 请求上下文缓存 `ChatSpec`、state 和 history，后续逻辑直接复用。
6. 无 `chat_id` 或旧数据缺少 `turn_states` 时，保留现有全量候选搜索作为兼容回退。

**语义约束**：不能只取最新 Chat。`CONTEXT.md` 已规定旧式 `sessionid + msgid` 查询必须搜索当前拥有权范围内的全部候选 Chat。

**预期收益**：从“每个候选构建完整历史 + 命中后再构建一次”降为“候选只读 state，命中 Chat 只构建一次历史”。

### 4.4 Chat History 内部流水线去 N+1

**目标**：保持完整 `ChatHistory` 默认响应不变，降低详情查询的 CPU、磁盘和审批锁开销。

**方案**：

- 同一请求内缓存 session state，避免 `_build_chat_history()` 和调用方重复读取。
- 收集消息中的全部审批 `requestId`，使用批量审批查询一次返回状态。
- `ApprovalService` 查询锁内不再构造完整 pending/completed ID inventory。
- 将详细 inventory 日志从 `INFO` 降为 `DEBUG`，或仅通过诊断接口输出。
- `ConversationArchiveStore.read_page()` 在一次批次扫描中同时判断 `has_more`，避免 `_has_previous_message()` 二次调用 `_select_page()`。
- 对同一文件版本复用解析结果，但不改变归档锁和一致性校验。

**预期收益**：消息越多、审批卡越多、归档批次越多，收益越明显；不改变历史消息形状和审批结果。

### 4.5 Console 发起会话去重和启动快照复用

**目标**：降低首个 SSE 事件前的 Chat 查找、配置读取和技能快照校验开销。

**方案**：

- 首次解析出的 Chat Record 写入 request-scoped context，后续 W+、启动和 runtime 阶段优先复用。
- 普通请求无 W+ 相关字段时，跳过不必要的 W+ Chat 查询。
- 场景 Chat 仍通过原子创建，并保留失败时的资源清理。
- 同一 Query 内复用已加载并验证的 Agent 配置、租户 Hook 和 Workspace Skill Snapshot。
- 跨请求按 Workspace Skill manifest 的 `stat` 做缓存失效；检测到变化时，下一个 Query 重新读取。
- 保持 Query Skill Snapshot 作为当前 Query 的一致性边界，不能把已失效技能继续带入运行中的 Query。

**预期收益**：减少启动阶段重复文件访问和重复校验，降低首包延迟；不改变技能生效时机和场景绑定语义。

## 5. 实施顺序

1. Repository 快照复用、低成本失效检测、读写锁拆分。
2. Coordinator 批量运行状态接口。
3. Answer Turn 查询请求内缓存和 `turn_states` 快速定位。
4. Chat History 批量审批、session state 复用和归档单次扫描。
5. Console Chat Record 复用及配置/技能快照复用。

## 6. 验证方案

### 功能回归

- 多用户、多租户和多 Agent Chat 隔离。
- `sessionid + msgid` 对重复 Chat、旧数据和缺少 `turn_states` 的兼容行为。
- Chat 列表的排序、分页、Cursor 和实时状态。
- Chat History 的完整消息、隐藏上下文脱敏、审批状态和归档边界。
- 场景 Chat 原子创建、失败清理、重连和并发首条消息。

### 性能指标

- `/chats` P50/P95 延迟、JSON 读取次数、排序耗时和状态查询次数。
- `/chats/{chat_id}` session JSON 解析次数、审批查询次数和归档批次读取次数。
- `/answer-turn` 候选 Chat 数、state 读取次数、历史构建次数。
- `/console/chat` 请求进入到首个 SSE 事件的延迟。
- ChatManager 锁等待时间和并发读吞吐。
- 跨进程写入后缓存失效延迟。

## 7. 风险与边界

- 进程内缓存无法消除多 worker 之间的瞬时陈旧，只能通过 `stat` 尽快发现变化。
- 不能删除所有锁；写入、查后创建、归档提交和 session 事务仍需要原子保护。
- 不能用“最新 Chat”替代旧式 Answer Turn 的全候选搜索。
- 不能把实时运行状态放入 TTL 缓存。
- 本报告不建议直接把完整 Chat History 改成分页响应，避免破坏现有 API 契约。

## 8. 实施与验证结果（2026-09-04）

已按上述顺序完成代码改造，并拆分为以下提交：

- `8c5bc250c`：Chat Repository 进程内快照、stat 失效检测和 ChatManager 读写锁拆分。
- `3132cb4d2`：AnswerTurnCoordinator 批量状态读取及 `/chats` 列表批量组装。
- `70e729926`：Answer Turn state/history 请求内复用、批量审批读取和归档单次扫描。
- `92be1c3b2`：Console 启动 Chat 身份复用及 Query Workspace Skill Snapshot 复用。

实现边界与本报告一致：仍以 `chats.json` 为事实来源，没有新增持久化索引文件、没有迁移 MySQL，也没有改变完整 Chat History 默认响应契约。快照命中仅执行文件 stat；写入继续使用现有文件锁和原子替换；运行状态不使用 TTL；请求携带的 Chat ID 必须重新按 session/user/channel 校验后才复用。

已验证通过：

- `tests/unit/app/test_chat_json_repo.py`：24 passed。
- `tests/unit/app/test_chat_pagination.py tests/unit/app/test_chat_manager_agent_metadata.py`：26 passed。
- `tests/unit/app/test_answer_turn_coordinator.py tests/unit/app/test_chat_answer_turn_api.py`：17 passed。
- `tests/unit/app/test_approval_service.py::test_get_requests_batches_scope_filtered_records` 及 Chat API 回归：8 passed。
- `tests/unit/agents/test_conversation_archive.py`：12 passed。
- 使用临时 `SWE_WORKING_DIR`/`SWE_SECRET_DIR` 运行 Console/Runner 受影响测试：133 passed。
- 进一步运行 `tests/unit/app/ tests/unit/routers/ tests/unit/agents/`（排除缺失 `wplus-sop` 脚本）得到 1080 passed、9 skipped；随后在既有 `tests/unit/app/test_context_references.py` 的模块导入错误（`swe.app.agents` 不存在）处失败并中断，未将该目录套件标记为全绿。
- `pre-commit` 对每个改造提交均通过（包含 AST、mypy、black、flake8、pylint）。

全量受影响目录测试在收集阶段仍有一个仓库已有的缺失文件：`tests/unit/app/wplus_sop/test_stage_scripts.py` 引用不存在的 `skills/wplus-sop-miner/scripts/validate_stage_sop.py`；排除该文件后又遇到上述既有模块导入错误。未执行基准压测，因此本文不提供未经测量的延迟或吞吐数字。
