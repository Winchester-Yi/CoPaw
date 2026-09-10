# Cron 定时任务结果持久化与状态一致性优化方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 确保定时任务只有在最终执行成功、存在可展示的 assistant 内容且内容已写入正确 session 后才标记为普通 `success`，彻底消除“任务成功但聊天窗口只有输入”的错误状态。

**Architecture:** 将 Runtime 终态、assistant 输出和 session 持久化拆成三个独立条件；Runner 返回可确认的持久化结果，Cron Executor 根据统一状态机做最终判定。任务绑定层强制校验 chat/session/user/tenant 路由一致性，session 追加层保留原子锁和幂等能力并显式报告冲突。

**Tech Stack:** Python 3.12、FastAPI、Agentscope Runtime、`SafeJSONSession`、pytest、现有 Cron Manager/Executor/Runner 链路。

---

## 1. 当前基线与已确认问题

当前工作区 HEAD 为 `610e4db7a`，包含 `043f00e5`、`40c4a4033` 和 `40c2f65d0`。GitNexus 索引落后 HEAD 14 个提交，本方案以当前源码和 Git 历史为准。

已确认的现状：

1. `043f00e5` 已使 `response/failed` 与 `message/failed` 都进入失败判定，修复了“先收到 message/completed、最终 response/failed 却记 success”。
2. `40c4a4033` 将 `response/completed` 纳入 `completed_seen`，但输出提取仍只读取 `message/completed`，因此允许“无 assistant 输出的 success”。
3. Runner 在异步生成器 `finally` 中保存 session，Cron Executor 可能在持久化确认前处理完成后取消，形成 success 与 session 落盘之间的竞态。
4. 任务正常创建时 `task_session_id`、request 和 chat 通常一致，但复用旧 `task_chat_id` 时不会校正旧 chat 的 `session_id`/`user_id`。
5. `execution_key` 重复时当前追加逻辑静默跳过；通知路径的部分代码还混用了 `task_chat_id` 和 `task_session_id`。

### 1.1 部署前提：当前后端不使用 Redis

本方案明确不以 Redis 为前提，也不新增 Redis 依赖、连接、锁、租约、去重或 outbox：

- 当前生产启动链路由 `Workspace` 注入 `SchedulerAdapter`，外部调度通过 HTTP 完成；没有装配 `CronCoordination`。
- `src/swe/app/crons/coordination.py`、`CronCoordinationConfig`、`redis>=5.0.0` 依赖和相关单测属于遗留/可选代码，不是当前 Cron 执行链路。
- 任务执行幂等、结果确认和诊断只使用现有 Runner session、任务状态/消息持久化和文件原子写入能力。
- 外部发送成功后进程在本地回执写入前崩溃时，无法仅靠当前后端保证 exactly-once；本方案明确采用“本地回执 + 重放可识别”的 at-least-once 语义，并要求下游 channel 依据 `cron_delivery_key` 实现幂等。若下游不支持幂等，必须将该次结果标记为 `delivery_uncertain`，不得伪装为普通成功。

## 2. 目标行为契约

最终执行状态必须满足以下优先级：

| 优先级 | 条件 | 结果 |
|---|---|---|
| 1 | `response/failed`、`rejected`、`incomplete` | `error` |
| 2 | `response/canceled` | `cancelled` |
| 3 | 超时 | `timeout` |
| 4 | `response/completed`，有 assistant 内容，session 已提交 | `success` |
| 5 | `response/completed`，无 assistant 内容 | `error`，错误码 `empty_model_output` |

普通 `success` 的不变量：

```text
final_response_status == completed
assistant_message_count > 0
session_state_committed == true
task_session_id == request.session_id == chat.session_id
creator_user_id == request.user_id == chat.user_id
```

## 3. 实施任务

### Task 1: 建立统一的 Cron 终态模型

**Files:**
- Modify: `src/swe/app/crons/executor.py`
- Modify: `src/swe/app/crons/manager.py`
- Test: `tests/unit/app/test_cron_manager_completed_cancellation.py`

- [x] **Step 1: 为 `AgentStreamState` 增加独立字段**

保留 `response_completed_seen`、`response_failed_seen`、`response_cancelled_seen`，新增：

```python
assistant_message_seen: bool = False
assistant_message_count: int = 0
output_source: str = ""
session_state_commit_attempted: bool = False
session_state_committed: bool = False
```

- [x] **Step 2: 将失败状态优先级固定为高于完成状态**

执行顺序必须为：失败/取消 → 缺少终态 → 缺少 assistant 输出 → 缺少 session commit → 成功。

- [x] **Step 3: 为状态决策写矩阵测试**

覆盖 `message/completed`、`response/completed`、`response/failed`、`response/rejected`、`response/incomplete`、`response/canceled`、空流及中间事件无终态等组合。

- [x] **Step 4: 运行测试**

```bash
venv/bin/python -m pytest tests/unit/app/test_cron_manager_completed_cancellation.py -q
```

Expected: 新增状态矩阵测试全部通过。

### Task 2: 修复 `response/completed` 空成功

**Files:**
- Modify: `src/swe/app/crons/executor.py:960-980, 1764-1796`
- Test: `tests/unit/app/test_cron_manager_completed_cancellation.py`

- [x] **Step 1: 拆分事件文本解析函数**

新增 `_extract_text_from_message_event()` 和 `_extract_text_from_response_event()`。提取顺序为：

```text
message/completed.content
→ response/completed.output
→ 明确标识为 assistant 的 response output
```

按 `message_id`、`event_id` 或 `sequence_number` 去重，避免同时收到 message 和 response output 时重复追加。

- [x] **Step 2: 默认拒绝无 assistant 内容的完成结果**

当 `response_completed_seen` 为真但 `assistant_message_seen` 为假时，抛出：

```text
RuntimeError("Agent execution failed: empty_model_output")
```

不保留任何允许空输出成功的例外分支。

- [x] **Step 3: 修改输出预览逻辑**

`_build_agent_execution_result()` 必须使用统一解析后的 assistant 文本；无文本时不要生成普通成功预览。

- [x] **Step 4: 先写失败测试，再实现代码**

新增以下断言：

```python
response/completed without message/completed -> error
response/completed with response.output assistant text -> success
```

- [x] **Step 5: 运行测试**

```bash
venv/bin/python -m pytest tests/unit/app/test_cron_manager_completed_cancellation.py -q
```

### Task 3: 让 Runner 明确报告 session 持久化结果

**Files:**
- Modify: `src/swe/app/runner/query_attempt.py`
- Modify: `src/swe/app/runner/query_cleanup.py`
- Modify: `src/swe/app/runner/query_contracts.py`
- Modify: `src/swe/app/runner/runner.py`
- Modify: `src/swe/app/crons/executor.py`
- Test: `tests/unit/app/test_query_session_execution.py`
- Test: `tests/unit/app/test_runner_session.py`

- [x] **Step 1: 定义持久化结果对象**

新增内部结果结构：

```python
@dataclass
class QueryPersistenceResult:
    session_id: str
    user_id: str
    assistant_message_count: int
    commit_attempted: bool
    committed: bool
    commit_error: str | None = None
```

- [x] **Step 2: 在 `_save_and_close_session_execution()` 中记录提交结果**

只有 `commit_state()` 正常返回后才设置 `committed=True`；异常、超时、取消必须保留错误原因。

- [x] **Step 3: 将结果提供给 Cron Executor**

`stream_query` 结束后，Executor 必须取得本次 query 的 `QueryPersistenceResult`，不能只根据最后一个 Runtime event 猜测成功。

- [x] **Step 4: 增加真实 session 集成测试**

测试必须使用 `SafeJSONSession` 和真实 cleanup 路径，验证：

```text
assistant 存在 + commit 成功 -> success
assistant 不存在 -> error
commit 超时/失败 -> error，不得 success
```

- [x] **Step 5: 运行测试**

```bash
venv/bin/python -m pytest \
  tests/unit/app/test_query_session_execution.py \
  tests/unit/app/test_runner_session.py -q
```

### Task 4: 修复完成后取消竞态

**Files:**
- Modify: `src/swe/app/crons/executor.py:1332-1402`
- Modify: `src/swe/app/crons/manager.py:3339-3369`
- Test: `tests/unit/app/test_cron_manager_completed_cancellation.py`

- [x] **Step 1: 收紧完成后取消成功条件**

只有以下条件全部满足时，取消后才可保留 success：

```python
stream_returned
and assistant_message_seen
and session_state_committed
```

- [x] **Step 2: 增加有界 cleanup 等待**

取消发生在 stream 关闭前时，最多等待配置的 cleanup timeout；等待后仍未提交，状态为 `cancelled` 或 `error`，不得伪装成 success。

- [x] **Step 3: 使用同步屏障构造确定性竞态测试**

测试顺序：

```text
收到 completed
暂停在下一次 async iterator 请求前
注入取消
断言 session 未提交时不能 success
释放 cleanup
断言提交成功后才允许 success
```

- [x] **Step 4: 运行测试**

```bash
venv/bin/python -m pytest tests/unit/app/test_cron_manager_completed_cancellation.py -q
```

### Task 5: 修复 task chat/session 路由一致性

**Files:**
- Modify: `src/swe/app/crons/manager.py:2146-2308`
- Modify: `src/swe/app/crons/executor.py:753-783, 1717-1745`
- Test: `tests/unit/app/test_tenant_cron_api.py`

- [x] **Step 1: 校验已有 chat 的绑定**

复用 `task_chat_id` 时比较：

```text
chat.session_id == task_session_id
chat.user_id == creator_user_id
```

- [x] **Step 2: 修复不一致数据**

如果旧 chat 不一致，优先创建新的正确 chat 并更新任务 metadata；保留旧 chat，避免覆盖既有历史。

- [x] **Step 3: 统一 ID 语义**

明确：

```text
task_chat_id     -> Chat 记录/跳转
task_session_id  -> Runner/session 文件
creator_user_id  -> session 文件 user 维度
```

所有 push store、超时通知和 channel 调用按接口契约使用正确 ID，不得将 chat ID 当 session ID。

- [x] **Step 4: 增加路由不一致测试**

覆盖旧 chat session 不一致、user 不一致、tenant/source/scope 不一致，并断言执行不会写入一个聊天窗口读取另一个 session 的结果。

### Task 6: 加强 execution key 幂等与冲突检测

**Files:**
- Modify: `src/swe/app/runner/runner.py:2774-2842`
- Modify: `src/swe/app/crons/executor.py:41-76`
- Test: `tests/unit/app/test_cron_session_append.py`

- [x] **Step 1: 为 task run 保存输入指纹**

记录：

```text
job_id
session_id
user_id
execution_key
input_hash
```

- [x] **Step 2: 区分幂等重放与 key 冲突**

相同 key 且输入完全一致时返回 `idempotent_replay=true`；相同 key 但输入不同必须返回 `execution_key_conflict`，不能静默跳过。

- [x] **Step 3: 增加并发测试**

验证两个并发执行在同一 session 上不会丢消息、不会重复追加，也不会错误覆盖旧 state。

### Task 7: 完善观测与诊断

**Files:**
- Modify: `src/swe/app/crons/executor.py`
- Modify: `src/swe/app/crons/manager.py`
- Modify: `analysis/playbook/common-errors.md`
- Test: `tests/unit/app/test_cron_manager_completed_cancellation.py`

- [x] **Step 1: 记录一次最终决策日志**

日志必须包含：

```text
job_id, trace_id, task_chat_id, task_session_id,
request_session_id, chat_session_id, creator_user_id,
tenant_id, source_id, scope_id, execution_key,
response_terminal_status, completed_message_seen,
assistant_message_count, output_len, output_source,
session_state_commit_attempted, session_state_committed,
final_status, final_error_code
```

- [x] **Step 2: 增加指标**

至少增加：

```text
cron_empty_model_output_total
cron_success_without_assistant_total
cron_session_commit_failure_total
cron_cancel_after_completed_total
cron_session_route_mismatch_total
cron_execution_key_conflict_total
```

- [x] **Step 3: 更新排查手册**

在 `analysis/playbook/common-errors.md` 中增加按日志字段判定四类问题的步骤，并提供 session JSON 检查命令。

### Task 8: 历史数据处理与直接上线

**Files:**
- Modify: `analysis/playbook/` 相关文档
- Test: Cron 集成测试

- [ ] **Step 1: 上线前创建 session 与任务状态备份**

在切换前备份任务定义、执行记录和相关 session JSON，确保可以恢复历史数据。新逻辑直接生效，不保留旧成功判定分支。

- [ ] **Step 2: 扫描并标记历史空成功任务**

扫描 `status=success` 但 session 中没有 assistant 的执行记录，标记为 `historical_incomplete`，不要伪造模型结果。

- [ ] **Step 3: 直接部署并执行端到端验证**

部署后立即执行成功、空输出、最终失败、取消、session 路由不一致和重复 execution key 场景，确认任务状态、Monitor 记录和聊天历史一致。

- [ ] **Step 4: 发布后持续核验不变量**

持续检查普通 `success` 是否同时满足最终 completed、assistant 存在、session 已提交、chat/session 路由一致四项条件。

## 4. 验收标准

上线前必须满足：

1. `response/completed` 且无 assistant 内容不再产生普通 `success`。
2. `message/completed + response/completed` 能稳定写入正确 session 并显示在聊天窗口。
3. `message/completed + response/failed` 最终仍为 `error`，保留失败原因。
4. 完成后取消只有在 session commit 成功后才能保留 `success`。
5. chat/session/user/tenant/source/scope 不一致时不会静默读写不同 session。
6. 相同 execution key 的合法重放幂等，冲突 key 可观测且不丢消息。
7. 所有相关单元测试、session 集成测试和至少一条真实 Cron 端到端测试通过。
8. `cron_success_without_assistant_total` 长期为 0。

## 5. 风险与取舍

- 空输出从 `success` 统一改为 `error`，会改变已有工具型任务的状态；该规则对所有新执行一致生效。
- 将成功通知推迟到 session commit 后，会增加极少量完成延迟，但能避免用户收到“已完成”后打开空聊天。
- 修复旧 chat 绑定时优先新建正确 chat，可能产生少量历史 chat；直接覆盖旧 chat 风险更高，不推荐。
- 直接切换会立即改变空输出任务的状态语义，必须在上线前完成 Runtime/adapter 事件矩阵和真实 session 集成测试。

## 6. 方案完成定义

本方案实施完成的唯一判断不是“任务状态显示 success”，而是：任何显示为普通成功的定时任务，都能在正确租户、正确用户、正确 chat 对应的 session 中找到本次 assistant 消息，并且该事实有日志和测试可证明。

## 7. 兼容性结论

本方案是执行语义上的破坏性变更，但不是删除历史数据或破坏旧 session 文件格式的迁移：

1. `response/completed` 但无 assistant 内容的任务，不再记为普通 `success`，统一改为 `error(empty_model_output)`。
2. 成功通知会延迟到 session 提交确认之后，依赖“收到完成事件就立即通知”的调用方需要调整预期。
3. 完成后取消但未完成 session 提交的任务，不再强制保留 success，历史统计中的 success 数量可能下降。
4. 旧 chat/session 绑定不一致时，可能创建新的正确 chat；用户看到的任务 chat ID 可能变化，但旧 chat 和旧 session 保留，不做破坏性删除。
5. 新增诊断字段和 task run 指纹只追加新数据；现有 session JSON 不改写、不删除旧字段。
6. 不新增状态枚举；外部 API 与前端继续使用既有 `error` 状态，并通过 `empty_model_output` 错误码区分。

部署前必须完成全量测试、历史数据备份和客户端状态枚举检查；部署后所有新执行直接采用新语义。
