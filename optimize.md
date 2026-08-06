# `src/swe/app` 性能优化方案：SSE、首 Token 路径与会话队列

## 1. 范围、结论与约束

本文只覆盖此前审查中编号 `1`、`3`、`5` 的问题：

1. `TaskTracker` 的 SSE 事件缓存、广播和重连订阅。
2. Agent 请求在产生第一条业务事件前的串行前置工作。
3. `UnifiedQueueManager` 的按会话队列与 consumer 任务模型。

目标不是用更大的超时或更多线程掩盖问题，而是让内存、任务数、排队量和外部依赖等待都有明确上限，并保留以下现有语义：

- 同一会话的消息顺序不能改变。
- `/console/chat/stop` 和重连必须继续工作。
- Hook 是安全边界；`USER_PROMPT_SUBMIT` 和 `SESSION_START` 的阻断决定不能移到后台。
- MCP 连接失败仍应按现有策略降级，而不是使整个请求失败。

目前没有压测和生产时序数据，本文中的阈值均应做成配置，先以观测结果定标后再收紧。当前代码已经有部分耗时日志，例如 MCP 连接耗时和 provider-models 中间件耗时；但没有覆盖端到端的 TTFT、SSE 积压和 queue-key 数量，不能用现有日志判断容量上限。

## 2. 现状调用链

```text
Console / Channel 请求
  -> ChannelManager.enqueue 或 TaskTracker.attach_or_start
  -> TaskTracker._producer
       -> AgentRunner query stream
       -> buffer.append(event)
       -> 每个 subscriber queue.put_nowait(event)

AgentRunner 的首条业务事件之前
  -> 审批、配置、USER_PROMPT_SUBMIT Hook
  -> 创建或读取 chat
  -> 解析上下文引用
  -> 逐个创建并连接 MCP client
  -> 首次会话同步调用标题服务
  -> SESSION_START Hook
  -> 创建 Agent、注册 MCP client
  -> Agent 开始生成事件

Channel 回调
  -> 每个 (channel, session, priority) 创建 asyncio.Queue + consumer task
  -> consumer 一直存活，空闲 10 分钟后清理
```

Console 在创建后台 run 后会先发送 keep-alive，因此网络层的 first byte 可能很好看；真正影响体验的是从接收请求到首条 Agent/SSE 业务事件的时间，即 semantic TTFT。后续指标必须同时记录两者，避免 keep-alive 掩盖模型前置等待。

---

## 3. 问题一：`TaskTracker` 的无界 SSE 状态与全局锁广播

### 3.1 代码事实

`TaskTracker` 的注释已声明 subscriber 使用无界队列，[`task_tracker.py:38`](src/swe/app/runner/task_tracker.py#L38)。每个 run 还保存没有大小或字节数限制的 `buffer`。重连时，`attach` 会将整个 buffer 拷贝到新建的无界 queue；见 [`task_tracker.py:105`](src/swe/app/runner/task_tracker.py#L105)。

生产者广播时持有 tracker 的单一 `_lock`，将事件追加到 buffer 后，对同一 run 的所有订阅者执行 `put_nowait`；见 [`task_tracker.py:209`](src/swe/app/runner/task_tracker.py#L209)。

这会带来三个独立问题：

1. **内存没有上界。** 长任务产生的所有 token、工具输出和进度事件都会保存到 buffer；每个慢订阅者还会获得一份尚未消费的完整事件序列。
2. **广播成本按订阅数线性增长。** 一个 run 有 `S` 个重连订阅者、产生 `E` 条事件时，内存和循环工作量都近似为 `O(S * E)`。
3. **跨 run 的锁竞争。** 即使事件属于不同 chat，所有 `_broadcast_sse` 仍争抢同一把 `_lock`。订阅者复制或大事件序列会延迟不相关会话的广播、取消、detach 与进度读取。

一个典型失效场景是：移动网络上的连接变慢但 TCP 没有及时断开，Agent 继续输出工具日志。producer 从不等待 queue，事件持续累计；run 结束前没有任何背压，也没有主动剔除该订阅者。

### 3.2 推荐设计：有界回放窗口 + 有界订阅队列 + 每 run 广播串行化

推荐分两步实施。第一步不改变客户端协议，优先消除失控内存；第二步再增加精确的断点续传。

#### 阶段 A：最小可用的资源边界

将 `_RunState` 改为以下逻辑状态：

```text
RunState
  replay: deque[EventEnvelope]          # 按事件数和字节数双重上限
  replay_bytes: int
  subscribers: dict[id, Subscriber]
  broadcast_lock: asyncio.Lock          # 只序列化同一 run 的事件顺序

Subscriber
  queue: asyncio.Queue[EventEnvelope]   # 有界
  dropped_events: int
  detached: bool
```

建议新增下列配置，所有值都应从环境/系统配置读取：

| 配置 | 初始建议 | 含义 |
|---|---:|---|
| `SWE_SSE_REPLAY_MAX_EVENTS` | 512 | 单 run 最多保留的回放事件数 |
| `SWE_SSE_REPLAY_MAX_BYTES` | 1 MiB | 单 run 回放窗口的字节上限 |
| `SWE_SSE_SUBSCRIBER_QUEUE_MAX_EVENTS` | 128 | 单连接未消费事件上限 |
| `SWE_SSE_SUBSCRIBER_QUEUE_MAX_BYTES` | 512 KiB | 单连接未消费字节上限 |
| `SWE_SSE_MAX_SUBSCRIBERS_PER_RUN` | 4 | 单 chat 最大同时订阅数 |

事件入队时必须以序列号、序列化后的 byte length 和类型封装。超过 replay 上限时从队头驱逐旧事件；超过 subscriber 上限时，不可继续无声积压。策略建议为：

- 尝试入队失败后标记 subscriber 为慢消费者，并删除该 subscriber。
- 最多发送一条本地控制事件 `slow_consumer`；若队列已满，直接关闭该流。
- producer 继续服务 run 和其他 subscriber，慢连接不能反向阻塞 Agent。
- 断开的客户端通过现有 reconnect 机制重新订阅，只得到当前回放窗口；如果缺少旧事件，服务端明确发送 `replay_truncated` 控制事件。

该阶段可能使极慢客户端丢失部分中间 token，但不会丢失任务本身，用户仍可重连或查看最终 chat 记录。这是比进程 OOM 更安全的退化方式。

#### 阶段 B：可验证的断点续传

为 SSE 事件添加单调递增的 `event_id`，服务端接受 `Last-Event-ID` 或已有请求体中的 `after_event_id`。reconnect 时只回放 `event_id > after_event_id` 的窗口数据。若请求 ID 早于窗口起点，则返回一个明确的 `replay_truncated` 事件，并由 Console 改为读取完整的会话历史/最终状态，而不是假装收到了完整流。

此阶段将“重连可拿到全部内存中的事件”的隐式行为，转换为有界、可观察、可恢复的协议。不要让重连逻辑继续复制整个列表。

#### 锁与广播实现要求

- `_lock` 只保护 `_runs` 注册表、订阅者增删和状态交换，不能包住所有 run 的 fan-out。
- 同一 run 使用 `broadcast_lock` 保证 event-id 和 subscriber 可见顺序。
- 广播时先在注册表锁内取得当前 subscriber 快照，再在该 run 的 `broadcast_lock` 中逐个非阻塞投递；失败项收集后再短暂获取注册表锁删除。
- 不允许为“保证不丢 token”在 producer 中 `await queue.put()`。这会把慢消费者变成整个 Agent 输出的背压源。
- `detach_subscriber` 必须幂等；producer finalizer 仍负责通知 sentinel、清理 run 与 task-progress。

### 3.3 观测、测试和验收

新增指标：

- `swe_sse_active_runs`、`swe_sse_active_subscribers`。
- `swe_sse_replay_events`、`swe_sse_replay_bytes`、`swe_sse_subscriber_queue_bytes` 的 gauge/histogram。
- `swe_sse_slow_consumer_disconnect_total`、`swe_sse_replay_truncated_total`。
- 单次广播耗时和 subscriber fan-out 数量。

必须补充测试：

1. 持续产生事件时，replay 的事件数和字节数不超过配置上限。
2. 一个不消费的 subscriber 被剔除后，正常 subscriber 仍按顺序收到完整后续事件。
3. 多个并发 run 广播时，一个 run 的慢 subscriber 不阻塞另一个 run 的 detach、stop 或事件发送。
4. reconnect 从合法 event-id 续传；过期 event-id 获得 `replay_truncated`。
5. 任务取消、正常结束和 HTTP 客户端断开均不会留下 subscriber 或 run 状态。

验收标准是长时间流式压测后 RSS 有稳定上界，慢客户端数量增长不再线性拉高进程内存，且正常客户端的 p95 广播延迟不随无关 run 增长显著恶化。

---

## 4. 问题三：首 Token 前存在可累加的串行外部等待

### 4.1 当前前置链路

`AgentRunner._prepare_query_preflight` 会执行审批解析、读取 agent 配置、加载 tenant hook 和 `USER_PROMPT_SUBMIT` Hook；见 [`runner.py:2205`](src/swe/app/runner/runner.py#L2205)。这些操作中，安全决策需要维持同步。

通过 preflight 后，`_start_query_runtime_resources` 依次执行：

1. 创建/查询 chat 和上下文引用。
2. 构建并连接 MCP clients，[`runner.py:3147`](src/swe/app/runner/runner.py#L3147)。
3. 在 Agent 主回答前生成标题，[`runner.py:3156`](src/swe/app/runner/runner.py#L3156)。
4. 执行 `SESSION_START` Hook。
5. 创建 Agent 并注册 MCP。

MCP 连接函数逐项 `await client.connect()`，[`runner.py:990`](src/swe/app/runner/runner.py#L990)。每个 client 的连接超时沿用 240 秒；参见 [`runner.py:136`](src/swe/app/runner/runner.py#L136)。因此有 `N` 个不可用 MCP 时，最坏等待接近 `N * 240s`，尽管每个失败最终会被降级跳过。

标题生成也在该关键路径上。标题服务单次超时为 30 秒，[`title_generator.py:19`](src/swe/app/title_generator.py#L19)，调用为等待式 HTTP 请求，[`title_generator.py:39`](src/swe/app/title_generator.py#L39)。它只影响首次/需要更新标题的会话，但恰好发生在用户最敏感的新会话首答。

### 4.2 优化原则

将前置工作分为三类：

| 类别 | 处理原则 | 当前项 |
|---|---|---|
| 必须阻断 | 必须在模型调用前完成，失败按安全策略处理 | 审批、`USER_PROMPT_SUBMIT`、阻断型 `SESSION_START` Hook |
| 首答必需 | 只等待当前请求会真实使用的资源；设置小而明确的预算 | chat 注册、必要的 MCP、Agent 创建 |
| 可延后 | 不影响安全或第一轮推理，放到首条业务事件后 | 标题生成、非关键目录扫描、非关键索引更新 |

不能为降低 TTFT 将 Hook 改为异步旁路，也不能将未就绪 MCP 假装已注册给 Agent。优化应来自资源选择、并行化和预算，而不是放宽正确性。

### 4.3 方案

#### A. 将标题生成移出首答关键路径

推荐在 chat 注册完成后创建受管理的 title task，但不在 `_start_query_runtime_resources` 等待它。title task 仍执行现有 `generate_title` 和 `_persist_session_title`，只改变调度位置。

要求：

- task 必须由 workspace/TaskTracker 的受管集合持有，完成后移除并消费异常；不能裸 `create_task`。
- title 只在确定 chat id 后启动，使用现有幂等判断，避免同一会话重复生成。
- 标题完成后通过一个独立 SSE 事件或 Console 轮询可见；不能依赖本轮 response header。
- 给 title 服务单独设置较短的连接/总超时和失败指标。标题失败应继续保持 fallback chat name，不重试阻塞用户消息。

这是低风险、高收益的改动：新会话的 semantic TTFT 不再被最多 30 秒的外部服务支配。

#### B. 并行且有预算地连接 MCP

将 `_build_and_connect_mcp_clients` 拆为“选择”“并行连接”“稳定排序”三步：

1. 从 agent 配置和请求显式选择的 skill/context 推导首轮必需 MCP 集合。默认不需要的 client 不应为每轮请求建立连接。
2. 对选中的 client 用 `asyncio.gather(..., return_exceptions=True)` 并行连接；每个连接使用独立、较短且可配置的 connect budget。
3. 按原配置顺序筛掉失败 client，再将成功实例注册给 Agent，保持工具列表的确定性。

建议新增：

| 配置 | 初始建议 | 说明 |
|---|---:|---|
| `SWE_MCP_CONNECT_TIMEOUT_SECONDS` | 5-10 秒 | 单 client 建连预算，不能复用 240 秒调用超时 |
| `SWE_MCP_CONNECT_MAX_CONCURRENT` | 4 | 控制 fan-out，避免一次请求压垮外部 MCP |
| `SWE_MCP_REQUIRED_CONNECT_TIMEOUT_SECONDS` | 单独配置 | 仅对明确 required 的 MCP 使用 |
| `SWE_MCP_CONNECT_FAILURE_COOLDOWN_SECONDS` | 30-60 秒 | 对连续失败的 optional MCP 熔断，避免每轮重复等待 |

对于 HTTP MCP，后续可评估按 `(tenant, agent, client configuration fingerprint)` 复用连接池或 client factory。不得直接跨请求复用带有 `session_id`、`chat_id`、`trace_id` 或用户透传 header 的 stateful client；这些信息位于当前 client 创建路径 [`runner.py:1025`](src/swe/app/runner/runner.py#L1025)，错误复用会造成租户与会话边界问题。第一阶段只并行化短生命周期连接，待压测证明建连是主要成本后，再设计带显式 claims 绑定的安全池化。

#### C. 为关键路径建立预算与分段追踪

在以下边界记录 monotonic duration，统一附加 `trace_id`、tenant/source、agent 和结果标签：

```text
request_received
  -> workspace_ready
  -> preflight_done
  -> trace_started
  -> chat_ready
  -> context_refs_ready
  -> mcp_ready
  -> session_start_hook_done
  -> agent_ready
  -> first_agent_event
  -> first_sse_business_event
```

需要新增的核心指标：

- `swe_query_semantic_ttft_seconds`：从路由接收至第一条业务 SSE。
- `swe_query_transport_ttfb_seconds`：从路由接收至 keep-alive，单独记录，不作为 Agent 性能指标。
- 每个阶段的 histogram 和错误/超时计数。
- `swe_mcp_connect_duration_seconds{client,outcome}`、`swe_mcp_connected_clients`、`swe_mcp_circuit_open_total`。
- `swe_title_generation_duration_seconds{outcome}`，以及 title task backlog。

### 4.4 验收与回滚

验收时应建立四组基线：无 MCP、一个健康 MCP、多个健康 MCP、多个不可用 MCP。预期结果是：健康 MCP 的首答准备时间由串行总和变为接近最慢单项；不可用 optional MCP 仅消耗其并行预算，不随数量线性放大。

完整发布方案应为标题异步化、MCP 并行连接、MCP 熔断分别提供 feature flag。第一阶段先只落地可独立调小的连接预算和并发上限；若 MCP 工具注册顺序、鉴权或外部兼容性出现问题，可通过回滚代码版本恢复串行路径。

### 4.5 本次落地（第一阶段）

已在当前分支完成首答路径的最小改造：

- MCP 仍按原配置创建请求级 client、仍保留失败降级和工具注册顺序；连接改为 `asyncio.gather` 并发执行，并通过 `SWE_MCP_CONNECT_MAX_CONCURRENT`（默认 `4`）限制 fan-out。
- MCP 建连预算从通用 HTTP 调用预算中拆出，由 `SWE_MCP_CONNECT_TIMEOUT_SECONDS` 控制，默认 `10` 秒；超时或失败的临时 client 会主动关闭，不加入 Agent。
- `SESSION_START` Hook 和显式技能 Hook 完成后，Runner 将标题生成作为受管后台 task 启动，不再在 Agent 创建前等待外部标题服务。
- Console 首条业务 SSE 之前只检查已完成的标题任务；不会等待未完成 task。流结束时仍会等待该 task 并发送 `session_title_updated`，保证短响应也能刷新标题。

本阶段没有实现跨请求 client 池化、连接失败 cooldown 或指标接入。这些项需要先确定 tenant/configuration fingerprint、指标后端和容量基线；在缺乏这些约束时直接加入全局缓存或熔断状态，会放大会话 claims 与租户隔离风险。

---

## 5. 问题五：按 QueueKey 创建常驻 consumer，缺少全局准入与总容量

### 5.1 代码事实

`UnifiedQueueManager` 为每个 `(channel_id, session_id, priority_level)` 创建一个 `asyncio.Queue` 和一个 consumer task；见 [`unified_queue_manager.py:165`](src/swe/app/channels/unified_queue_manager.py#L165)。默认每个队列可缓存 1000 条消息，[`channels/manager.py:41`](src/swe/app/channels/manager.py#L41)。队列只有在为空且空闲超过 10 分钟时才清理，[`unified_queue_manager.py:405`](src/swe/app/channels/unified_queue_manager.py#L405)。

因此当前资源上限是“每个 QueueKey 1000 条”，而不是进程级上限。攻击流量、批量 webhook 或大量真实短会话都可以创建大量 key：

```text
总 consumer task 数 ~= 活跃或 10 分钟内活跃的 QueueKey 数
理论总积压     ~= QueueKey 数 * 1000
```

此外，`ChannelManager._enqueue_one` 每次回调都创建一个 `_enqueue_with_timeout` task，[`channels/manager.py:374`](src/swe/app/channels/manager.py#L374)；满队列超时在 [`channels/manager.py:426`](src/swe/app/channels/manager.py#L426) 被静默吞掉。上游无法区分“已接收”与“因过载丢弃”。

### 5.2 推荐设计：从“每 key 一个常驻 task”改为“有界 keyed scheduler”

最终目标是固定数量 worker 处理可变数量的 session queue，而不是让 session 数直接决定 task 数。

```text
enqueue
  -> 全局准入控制（总消息、活跃 key、每 tenant 配额）
  -> SessionQueueState.messages（有界）
  -> ReadyKeyQueue（key 只入队一次）
  -> 固定数量 dispatcher worker
  -> 处理一个 batch
  -> 若同 key 仍有消息，重新进入 ReadyKeyQueue
```

每个 `SessionQueueState` 维护 `enqueued_to_ready` 和 `in_flight` 标识。同一个 key 在 worker 执行时不能被另一 worker 并行处理，因此仍严格保证同会话顺序；不同会话可以由不同 worker 并行处理。完成一个 batch 后，如果该 key 仍有消息，重新入 ready queue，使高流量会话不能无限独占 worker。

priority 继续参与调度，但必须定义会话级顺序：建议优先级只影响“下一个待执行 batch 的选择”，而不能让同会话的普通消息与命令消息并行进入 Agent。可以将 queue state key 收敛为 `(channel, session)`，在 state 内使用小型优先队列；这是比当前 `(channel, session, priority)` 更符合顺序约束的模型。

### 5.3 分阶段实施

#### 阶段 A：在现有架构上增加硬上限和显式拒绝

先不重写 dispatcher，新增全局 admission controller：

| 配置 | 初始建议 | 行为 |
|---|---:|---|
| `SWE_CHANNEL_MAX_ACTIVE_QUEUE_KEYS` | 1,000 | 超过时不再创建新 QueueKey |
| `SWE_CHANNEL_MAX_TOTAL_PENDING_MESSAGES` | 10,000 | 所有 key 的消息总数上限 |
| `SWE_CHANNEL_MAX_PENDING_PER_TENANT` | 200-1,000 | 防止单租户占满进程 |
| `SWE_CHANNEL_MAX_ENQUEUE_TASKS` | 有界 semaphore | 限制 `_enqueue_with_timeout` 并发 task |
| `SWE_CHANNEL_QUEUE_MAXSIZE` | 降至按容量定标 | 继续约束单 key |

达到上限时必须：

- 记录 `rejected_total{reason,channel,source}`。
- 对同步 HTTP/webhook 入口返回明确的 overload 响应；对不能同步响应的渠道，调用渠道定义的失败回执或告警策略。
- 不得静默 `pass`。消息丢失必须可追踪、可告警。
- 保留已在处理的队列，优先拒绝新 key，避免取消用户已启动的任务。

阶段 A 能快速把内存和 task 数限制在可计算范围，但仍会保留空闲 consumer 的 10 分钟成本。

#### 阶段 B：实现 keyed scheduler

新增独立的 `KeyedChannelScheduler`，由 `ChannelManager.start_all/stop_all` 管理。建议职责如下：

| 组件 | 职责 |
|---|---|
| `AdmissionController` | 原子维护总量、tenant 配额和拒绝统计 |
| `SessionQueueState` | 存储单会话消息、优先级、in-flight 状态、最后活跃时间 |
| `ReadyKeyQueue` | 只放可运行的 key，不放每条 payload |
| `DispatcherWorker` | 取 key、取一个合并 batch、调用现有 `_process_batch`、重新调度 |
| `QueueMetrics` | 暴露 active keys、ready depth、pending 消息、等待时长、拒绝数 |

worker 数通过 `SWE_CHANNEL_DISPATCHER_WORKERS` 配置，并与模型供应商限流和 workspace cache 容量联合定标。不要把 worker 数简单设为 CPU 核数：每个 batch 可能等待 Agent、MCP 和外部 API，实际约束是允许的并发 Agent 任务和供应商 quota。

队列清理改为删除“无消息、未调度、非 in-flight 且超过 TTL”的 state。不会再取消一个长期阻塞在 `queue.get()` 的 task，因为不存在每 key 常驻 task。

### 5.4 与 `TaskTracker` 和重连的关系

Channel scheduler 只负责“何时开始一条会话消息”；一旦 `_process_batch` 启动，`TaskTracker` 仍负责该 run 的事件流、取消和 Console 重连。两者的边界应清晰：

- scheduler 的 pending payload 有字节/条数上限；TaskTracker 的 SSE event 有独立上限。
- scheduler 被拒绝表示任务从未开始；应返回可识别的 overload 状态。
- TaskTracker 的慢 subscriber 被剔除不应取消 scheduler 已经启动的任务。
- 同一 chat 已运行时，scheduler 不应依赖 `TaskTracker.attach_or_start` 的“message ignored”作为正常流控。这一状态应在 scheduler 中显式处理：要么排在当前 run 后，要么按渠道规则合并，不能无声忽略。

### 5.5 测试、压测和验收

必须覆盖：

1. 10,000 个不同 session 的突发消息不能创建 10,000 个 consumer task；task 数接近固定 worker 数加有限状态维护任务。
2. 同一 session 的 100 条消息严格按顺序处理；不同 session 可并行。
3. 高流量 session 不饿死低流量 session，且 priority 的调度规则可预测。
4. 达到全局、租户和单 key 上限时返回/记录明确 overload，而不是静默丢消息。
5. stop/reload 时，in-flight batch 按当前取消语义终止，未开始消息按渠道语义保留、重试或明确丢弃。

压测应输出：活跃 QueueKey 数、dispatcher worker 利用率、pending 深度、消息等待时间 p50/p95/p99、每 tenant 拒绝数和 event-loop lag。验收标准是 session 基数增长时 task 数保持受控，总 pending 不超过配置，并且同会话顺序测试始终通过。

---

## 6. 推荐落地顺序

1. **先加指标与压测脚本。** 没有 semantic TTFT、SSE 字节数和 queue-key 基线，就无法正确设容量。
2. **TaskTracker 阶段 A。** 这是 OOM 和全局锁竞争风险最高、且不依赖外部协议大改的一步。
3. **标题异步化与 MCP 并行预算。** 直接改善新会话首答；逐项 feature flag 发布。
4. **Channel 队列阶段 A。** 先消除无全局上限和静默丢弃，再观察真实 key 基数。
5. **Keyed scheduler 与 SSE 断点续传。** 这是结构性改造，应在前述指标和回归测试就位后实施。

每一步都应独立可回滚，不与租户隔离、Hook 安全策略或 Agent 业务逻辑重构混在同一变更中。
