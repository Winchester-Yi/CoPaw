# 05: 查看真实模型调用、用量与首输出延迟

Status: resolved
Type: task
Blocked by: 04
Triage: ready-for-agent
Priority: P0

**What to build:** 在执行链路中查看真正发生的模型调用，理解请求与实际模型、耗时、首输出延迟及已知用量；没有调用模型的短路和缺Usage不会制造调用或零值。

**Blocked by:** 04：展示Attempt与Agent执行树、同行瀑布

**Status:** resolved

依据已确认的独立可观测设计、Trace视图对照、领域词汇及ADR 0047/0048。与旧SDK、旧埋点、旧追踪ID和旧可观测存储查询没有运行依赖；只输出元数据，正文仅经现有业务授权从NAS读取。采集由用户处理，本票不增加ES、TTL/清理或新身份体系；授权仍沿用项目已有身份判定。

模型请求埋点验收范围为 OpenAI 兼容协议，覆盖通用适配器及基于同一适配器的 Kimi 分支。Ollama、Anthropic 和 Gemini 专用适配器从产品移除，涉及注册、配置解析、模型工厂及调用方。生产环境未使用这三个适配器。本次仅移除适配器及其产品入口，无存量配置迁移。

- [ ] 观察点位于实际模型调用边界，分别定义逻辑调用与本项目可见请求尝试；保留后续重试扩展身份，不能把一次reasoning包装等同一次物理请求。
- [ ] 从真实流式与非流式请求到独立日志、Span投影、API、LLM Inspector和Run摘要完成闭环；审批/计划等无请求短路不计llm_calls。
- [ ] 记录受控provider、请求模型、响应模型、streaming和finish reason；响应模型补齐可替换请求回退索引值并保留来源，二者不同不标身份冲突。
- [ ] 首有效chunk排除空启动与usage-only帧，首可见文本另计；无输出为null，非流式不伪造TTFT，未证明为首token时页面不称精确TTFT。
- [ ] Usage取真实最终/最新累计快照，不逐帧相加，不重复统计父逻辑容器和请求操作，不默认把推理Token额外加进输出；未知input/output分别保持null并展示覆盖。
- [ ] 两次模型请求中仅一次返回 20 个输入 Token 时，Run 摘要显示已观测的 20。单次请求详情对已知值直接显示，对缺失项显示「未获取」，输入和输出分别判断。全部缺失时对应 Run 指标返回 `null`。
- [ ] 验证上线前的历史 Run 在缺少请求级用量证据时，API 返回 `null`，Console 对相应 Token 字段留空。存在可信请求级证据时展示已知值，输入和输出分别判断。
- [x] 只记录真实存在的Reasoning Step ordinal，不创建假Step/Loop区间；Span与后续详情使用相同的 `step_index`。
- [x] 独立定义并验证实际Agent Loop汇总口径；retry上限、物理请求次数和缺失Step不能冒充完整Loop数。
- [ ] 测试流式取消、异常、无输出、Usage缺失、实际模型变化与并发上下文；日志/Raw不包含prompt、messages或响应文本，成本保持P1。
- [ ] 验证LLM类型详情在Chromium108、当前浏览器及适用嵌入场景正常展示。

首批 Console 验收覆盖 Run 摘要与单次模型请求详情。用包含重试、取消和缺失用量的 OpenAI 兼容模型执行链路，核对 JSONL、MySQL 投影、授权 API 与页面展示。跨 Run 模型性能指标在后续批次验收。

首批实施范围、事项依赖及完成标准见[模型请求可观测首批 Spec](../../model-request-observability/spec.md)。

验收环境不可用时必须明确记录未验证项，不以构建成功或新浏览器模拟替代真实Chromium108验收。

## 生产 OpenAI 兼容流式响应样例

以下三帧来自用户提供的实际模型响应末尾，作为适配器开发和回归测试的输入。

```text
data: {"id":"chatcmpl-338dcace-b8de-4335-9a0e-18b2a5851675","object":"chat.completion.chunk","created":1795597418,"model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"。"},"logprobs":null,"finish_reason":"stop","stop_reason":null,"token_ids":null}]}

data: {"id": "chatcmpl-338dcace-b8de-4335-9a0e-18b2a5851675", "object": "chat.completion.chunk", "created": 1795597418, "model": "deepseek-v4-flash", "choices": [], "usage": {"prompt_tokens": 333, "total_tokens": 344, "completion_tokens": 11, "prompt_tokens_details": {"cached_tokens": 0}}, "system_fingerprint": "vllm-0.25.1-dp16-ep-9c255410"}

data: [DONE]
```

- 同一请求的文本帧携带 `finish_reason=stop`，后续 `choices: []` 帧携带最终 Usage。流按现有语义继续消费至 `[DONE]`，请求结算同时保留实际模型 `deepseek-v4-flash`、结束原因 `stop`、输入 Token 333、输出 Token 11、合计 344。
- 在回归样例前加一帧有效文本时，首有效 chunk 和首可见文本应取该较早的帧。`choices: []` 用量帧和 `[DONE]` 均排除在首输出计时之外。
- 增加在收到用量帧前取消的变体。若此前未收到 Usage，请求详情显示「未获取」。响应正文只用于测试适配器的业务输出，观测 JSONL 仅验证受控元数据。

## Comments

2026-11-15：用户已确认15张票的拆分及阻塞关系，现发布为本地实施事项。尚未领取或开工；本次发布不构成功能开发授权。

2026-11-16：预备实现独立 `ObservabilityModelWrapper`，位于 RetryChatModel 外层，记录逻辑 LLM Span、受控 provider/request model、非流式最终 Usage、流式首个有效 chunk 和取消/异常状态；metrics 合并保留实测 duration，正文和 prompt 不进入事件。确定性单测 26 passed，真实普通 Runner MySQL 链路已验证主执行，但该票仍保持 `open`，等待 Ticket 04 完成树/Inspector，并需补齐模型 Span 的真实投影/API/UI验收及 Chromium 108 基线。

2026-11-16：使用独立 `swe_agentobs_codex_20261116_test` MySQL 库完成真实 Runner 验证：生命周期/结算与普通、专家主执行共 6 passed；Inbox 投影、授权 Run 查询和事件分页 2 passed；observability 单测 36 passed；Console typecheck、页面 Vitest 6 passed、相关 ESLint 通过。MySQL readiness 返回 `ready=true`，无缺表、列或索引。真实 Runner 断言覆盖 LLM Span 的 request/response model、来源、streaming、finish reason、Usage、API 查询及非流式首输出保持 null。未连接或写入业务库。Chromium 108.0.5359.95、当前浏览器及 `hideMenu=true` 下新增 LLM Inspector 展示仍未验收；流式取消、无输出、Usage 未知及并发上下文的端到端边界也未全部覆盖，因此本票继续保持 `open`。

2026-11-16：补充模型包装器边界回归：流式 Usage 使用最终累计快照而非逐帧相加；空输出/无 Usage 保持 token 与首输出延迟未知；Task cancel 与 async generator `aclose()` 均以 cancelled 结束 Span 并关闭源流；并发 Run 共用包装器时身份隔离，正文不入日志。`tests/unit/observability` 与 Runner 生命周期集成入口合计 41 passed、6 skipped（该次未提供真实 MySQL 环境变量）；Ticket 04/05 真实 MySQL 集成此前另行实测通过。目标 pre-commit 通过。Ticket 仍保持 open，浏览器及完整 UI 验收尚未完成。

2026-11-17：补齐真实模型调用异常与流迭代异常回归；验证原异常继续传播、request-attempt 与逻辑 chat Span 均记录受控失败类型、源流关闭且异常消息/模型文本不进入 JSONL。Run 的 `llm_calls` 及 input/output Usage 现在从唯一结束的逻辑 `LLM/chat` Span 集合幂等重算，物理 request-attempt 不重复计数，任一逻辑调用缺该项 Usage 时 Run 汇总保持 null；MySQL Runner/API 集成断言汇总值。LLM Inspector 新增 stream mode、finish reason 与逐 Span Usage 完整性。新增确定性流式 Provider 的真实 Runner→JSONL→MySQL→API 集成场景。最新验证：隔离 MySQL observability 集成 10 passed，Observability/Provider 单测 72 passed，Console API/Inspector Vitest 16 passed，生产构建、TypeScript、目标 ESLint 和 Python pre-commit 通过。仍保持 open：模型调用/流迭代异常尚未走 MySQL 端到端，Ticket 04 未完成，Chromium 108.0.5359.95/360 与 hideMenu=true 基线未验收；Step/Loop 的真实计数证据也未闭合。

2026-11-17：另补非流式请求 await 阶段取消测试，断言 `CancelledError` 原样传播、逻辑 chat Span 以 `cancelled` 结束且请求正文不进入 JSONL；定向单测 1 passed。零模型调用 Run 汇总语义、异常路径 MySQL 投影和实际 Step/Loop 计数仍未完成，本票保持 open。

2026-11-17：SWEAgent 的真实 MRO 命中 `ToolGuardMixin._reasoning`；Step 计数现位于该可达路径，只有实际调用下层推理时才递增，计划/审批短路不造 Step，retry 仍使用同一轮次索引。task-local ContextVar 将 1-based `step_index` 投影到真实 LLM Span，Tool Span读取当前 Agent 轮次。模型 retry 事实移除了把配置 `max_attempts` 误称实际 Loop 的 `loop_count`，仅保留真实 retry ordinal。MRO Step 计数、Span 投影及 Tool/LLM 边界定向测试 43 passed；指定文件 pre-commit (mypy/black/flake8/pylint) 通过。独立代码检视确认计数位置可达，未发现 correctness 问题。泛化 Agent Loop 汇总及真实 Chromium 108 UI 验收仍未完成。
2026-11-17：Console Run 瀑布行与 Inspector 现在显示有证据的 `Reasoning Step`，不对缺失索引补值。页面/API测试 19 passed，TypeScript、ESLint、Prettier 通过。真实 Chromium 108/360 与嵌入场景验收仍未完成。
2026-11-17：真实 AgentRunner→JSONL→隔离 MySQL→授权详情 API 集成现在断言逻辑 LLM Span 与物理 request-attempt 都保留实际 Reasoning Step。普通、专家主执行、Agent retry 和 streaming 四种场景 4 passed；结合 MRO/Span/Tool 定向测试共 43 passed，定向 pre-commit 通过。该值只表示真实 `_reasoning` ordinal，不代表 Loop 聚合完整性。

2026-11-17：Run 的 `agent_loops` 以已结束逻辑 `LLM/chat` Span 为证据，按 `(COALESCE(parent_span_id, attempt_id), step_index)` 去重；没有模型 Span或任一 Step index 缺失时保持 `NULL`。字段纳入 MySQL readiness、Run 列表/详情 API 与 Console Run 表。隔离 MySQL pipeline/Runner lifecycle 加相关单测 28 passed；Console 页面 19 passed、TypeScript 和目标 pre-commit 通过。Chromium 108.0.5359.95/360 与 `hideMenu=true` 实机验收仍未完成。

2026-11-17：补充独立 MySQL 聚合合同：同一执行组重复 Step 去重、不同 parent 的相同 Step 分别计数、已知与缺失 Step 混合时整体保持 `NULL`，新增用例 1 passed。独立复审确认 root 按 Attempt ID、后台 SubAgent 按传递的 SubAgent Span ID 分组正确；未来若 parent span 改为每次调用变化，应改用显式执行组身份。Console Run 类型现要求 API 返回 `agent_loops`，页面测试以可访问名称验证列值。目标 Console 测试 19 passed、TypeScript、ESLint、Prettier 通过；Python pre-commit 的现有 `test_mysql_pipeline.py` 两个集成用例超过 pylint 50 statements 限制（52/50、53/50），其余该次 hook 通过。Chromium 108/360 实机与 `hideMenu=true` 仍未验收。

Chromium 108.0.5359.29 实机检查已启动，但 Console dev server 的 `/api/auth/status`、`/api/source-system-config/effective` 与模板 API 返回 500，页面未加载 Run 表；因此该次不能作为 UI 兼容性验收。目标 108.0.5359.95、360 内核及 `hideMenu=true` 仍待可用服务环境。

2026-11-17：补充模型请求异常与流迭代异常的 Inbox→MySQL→授权 Run Detail API 集成测试。该路径发现模型 Span 失败结束事件使用小写 `technical_status="error"`，与事件契约仅接受 `UNSET/OK/ERROR` 不一致，白名单将其静默移除；现统一为 `ERROR`。两种异常场景 MySQL 集成 2 passed，确认 chat 与 request-attempt 记录失败、API 保留受控技术状态/错误类型，敏感 prompt、部分输出和异常消息不进入 JSONL/详情。实际 Step/Loop 计数、零调用汇总明确语义及 Chromium 108/360 UI 验收仍未完成。

2026-11-18：复核现有 request-await 与 stream-iteration 异常 producer→Inbox→Projector→manager Run Detail 合同时发现根 Run/Usage 摘要断言不足；现新增 MySQL Store 与 API 断言，确认 Run 终态为 failed、逻辑 `llm_calls=1`，input/output Usage 均保持 NULL，而 LLM Span 与 request-attempt 各自保留失败。两种异常合同真实 MySQL 2 passed，隔离 schema 删除并验证不存在；目标 lifecycle 测试文件 pre-commit 全绿。模型成功路径、零调用来源仍有未知语义，UI/Chromium108 验收按用户要求暂停，Ticket 保持 open。

独立复核提出物理 request-attempt 的 `error_type` 未被断言；现增加 MySQL 行及授权 Run Detail API 对 `RuntimeError` 的断言。请求异常和流迭代异常参数化合同复跑 2 passed；与晚到 SubAgent 合同同库执行共 3 passed，目标两文件 pre-commit 全绿、随机 schema 清理为0。独立复核 finding 已按代码更新待确认关闭。

同日继续：Performance API 将 `LLM/chat` 逻辑调用与 `LLM/request_attempt` 物理请求分开统计，避免重复计算逻辑调用/Usage 覆盖率；单测及真实 ModelWrapper→RetryChatModel→MySQL 投影→授权 Performance API 契约通过（MySQL 1 passed）。用例确认 `call_count=1`、`request_attempt_count=1`、Usage coverage=1，敏感 prompt 和回复正文均不进入事件；随机 schema 清理为0。Chromium108 UI验收按用户要求暂停，Ticket 保持 open。

同日复验：独立复核要求覆盖逻辑调用内部发生多次物理重试；真实 MySQL 契约改为 deterministic RetryChatModel 第一次 429、第二次成功，授权性能 API 验证 `call_count=1`、`request_attempt_count=2`，Usage 仍只按唯一逻辑 chat Span 计数（coverage=1）。该合同通过，随机 schema 清理核验为0；finding 更新待确认关闭。

2026-11-17：增加已结算且无 LLM Span 的 Run→JSONL→MySQL→API 合同测试。Run 汇总的 `llm_calls`、input/output Token 保持 NULL：仅凭没有投影到 Span 的记录无法区分真实零调用短路与被丢弃/缺失的模型 Span，不能伪造 0；若将来需要精确零值，须由业务结算提供独立可信的执行完整性/零调用事实。测试 `test_run_without_model_span_keeps_usage_summary_unknown` 在隔离 MySQL/API 通过。

2026-11-18：后端复核发现 RetryChatModel 的非流式空输出会触发一次真实请求重试，但不发 `model.retry_scheduled`，导致首个 request-attempt 只被后续 Span 创建逻辑标为泛化 `model_retry`。现复用受保护的重试回调发出受控 `EmptyModelOutputError` 事实，保持原立即重试策略不变；新增 model factory→JSONL 合同断言两条物理尝试、失败原因、`retry_of` Link 及 prompt/回复不落日志。模型调用、观测 core 与 Provider 定向套件 62 passed，目标 pre-commit 全绿。零调用来源仍保持未知；页面和浏览器验收按用户要求暂停。

2026-11-26：OpenAI 兼容请求的首批实现和独立回归已验收，历史缺测语义已验证。见[首批测试报告](../../model-request-observability/test-report.md)。 [本轮交付](../followup-completion-report.md)
