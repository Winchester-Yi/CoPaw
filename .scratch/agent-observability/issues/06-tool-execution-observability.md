# 06: 查看真实工具执行、拒绝与技术错误

Status: in-progress
Type: task
Blocked by: 04
Triage: ready-for-agent
Priority: P0

**What to build:** 从执行链路识别工具真正执行了几次、用了多久、处于什么业务和技术状态，并能区分审批/策略拒绝与实际执行失败。

**Blocked by:** 04：展示Attempt与Agent执行树、同行瀑布

**Status:** in-progress

依据已确认的独立可观测设计、Trace视图对照、领域词汇及ADR 0047/0048。与旧SDK、旧埋点、旧追踪ID和旧可观测存储查询没有运行依赖；只输出元数据，正文仅经现有业务授权从NAS读取。采集由用户处理，本票不增加ES、TTL/清理或新身份体系；授权仍沿用项目已有身份判定。

- [x] 真实工具执行使用独立tool_execution_id，关联业务tool_call_id和受控tool_key；同call重放可有不同execution_id，不能把包装层重复观察计为多次执行。
- [x] 记录实际可知的executor_kind、timeout budget、retry index和exit code，从日志→投影→API→Tool Inspector及Run实际调用计数完成闭环。
- [x] guard/审批/策略短路单独记录真实事实；未执行不增加工具执行次数，结构化失败不只依赖Python异常。
- [x] 业务status、technical_status、has_error和受控error.type/phase/code独立；失败、取消、拒绝不自动等同技术ERROR，子失败不自动把根或父操作标ERROR。
- [x] 安全name来自注册值而非命令/SQL/用户参数；工具schema、参数、结果和异常原文不进日志或安全Raw；为后续NAS证据保留精确业务关联。
- [x] 重复或乱序结束不叠加计数；缺开始可用自包含结束摘要但标partial，父身份/业务身份冲突不静默覆盖。
- [x] 测试实际成功/结构化失败/异常/取消/超时明确裁决、guard拒绝及并行工具；验证原工具调用次数、返回与取消语义不变。
- [ ] 完成真实工具的端到端演示及Chromium108、当前浏览器受影响Inspector验收。

验收环境不可用时必须明确记录未验证项，不以构建成功或新浏览器模拟替代真实Chromium108验收。

## Comments

2026-11-15：用户已确认15张票的拆分及阻塞关系，现发布为本地实施事项。尚未领取或开工；本次发布不构成功能开发授权。

2026-11-16：预备接入独立 Tool Span：在 `ToolGuardMixin` 的真实 hard-timeout 工具执行边界包裹 `registered.tool`，携带受控 tool_key、tool_call_id、executor_kind，按实际返回、结构化空结果、异常和取消分别结束，不记录参数/结果正文。既有工具相关回归 135 passed；本票仍保持 `open`，等待 Ticket 04 详情闭环后补投影/API/Inspector 及 Chromium 108 验收。

2026-11-16：已补充 Tool Span 的 timeout budget 与 execution id，按结构化 `isError` 区分业务失败和技术错误；取消不标成技术 ERROR。工具标识与白名单元数据已贯通 JSONL、MySQL span 投影、manager 授权 Run Detail API 和 Console Inspector。最终验证：相关 Python 单测 107 passed；真实 MySQL `_test` schema observability 集成 9 passed；Inspector Vitest 10 passed；TypeScript、目标 ESLint、生产构建和 pre-commit 均通过。没有连接或写入业务库。工单保持 `open`：Ticket 04 尚未关闭，且真实 Chromium 108.0.5359.95/360 验收环境不可用。
2026-11-17：新增 `tests/unit/agents/test_tool_observability.py`，直接运行真实 `_run_tool_call_with_hard_timeout_impl` 边界并替换外层 toolkit 执行器，验证成功/结构化 permission_denied/普通异常/取消的 Span 终态、技术状态、正文不落日志和业务返回/异常传播。7 项边界与模型调用路径测试通过。Ticket 06 其余未完成项：Guard/审批短路的独立事实、超时/并行工具执行覆盖、真实 Chromium 108 Inspector 验收。
2026-11-17：补充 Guard 判定事件：auto-denied 以 `Guard/rejected`、待审批以 `Guard/waiting_approval` 记录，身份只含 tool_key/tool_call_id/executor_kind，不生成 Tool execution id、不增加实际工具调用数。Projector 明确允许 Guard 携带安全 Tool identity，但拒绝 Guard 伪称拥有 tool_execution_id。直接 `_acting_impl` 回归验证策略短路未进入 tool executor，两个 Guard 状态通过正式 `_validate_payload`。
2026-11-17：连接用户提供的 MySQL，在隔离库 `swe_agentobs_codex_20261117_test` 重跑观测 pipeline 与 Runner lifecycle 集成，14 passed。验证覆盖 Guard 身份从 inbox→MySQL→manager 授权 Run Detail 的闭环；未写业务库。真实 Chromium 108/360 验收仍未完成。

2026-11-17：`tests/unit/agents/test_tool_observability.py` 增加真实 hard-timeout 路径与两项并行工具重叠验证，断言 timeout 取消底层执行、以受控 `tool_timeout` 标记技术错误；并行工具各自保留 tool_call/execution ID，原结果及一次调用语义不变。成功、结构化失败、异常、取消、guard 拒绝、timeout、并行场景共 8 passed。Chromium 108.0.5359.95/360 Inspector 实机验收仍未完成。

Chromium 108.0.5359.29 页面检查同样被 dev server 的认证/配置/模板 API 500 阻断，Inspector 未渲染；不能作为浏览器验收。108.0.5359.95、360 内核和 hideMenu 嵌入模式仍待服务可用后检查。

2026-11-18：工具输出现在在执行 Span task-local 绑定下，于 `ArchiveBinding.append` 确认 MySQL/NAS 发布成功后写 `evidence.linked`，只记录 Chat 与持久化 Msg ID；重复 ID 由 `HistoryStore.message_occurrence` 返回 unresolved。相关定向测试 61 passed，目标 pre-commit hooks 通过。新增真实 MySQL/NAS 发布→精确正文读取集成用例因当前未配置 `SWE_TEST_DB_*` 被 skip；真实 Inspector 及 Chromium 108/360 验收仍未完成。

2026-11-18：新增隔离 MySQL Span 终态乱序/幂等合同：先投影 failed `span.ended`，重放同 event ID，再投递同生命周期槽的冲突 succeeded 终态，随后投影成功 Run 终态和迟到 `span.started`；预期冲突隔离、唯一 Span 保留原错误、根 Run 成功及 ended_at 不因后到子事实回退。用例 `test_mysql_span_end_replay_and_late_start_preserve_one_terminal_fact` 已通过目标 pre-commit，但因未配置 `SWE_AGENTOBS_TEST_DB_*` 被 skip，故“重复或乱序结束”验收项仍未勾选，待实际隔离 MySQL 执行。

同日复验：观测 core/query 单测 66 passed；上述 MySQL 合同 1 skipped。独立复核修正 `has_error` 数据库整数断言后确认无其他阻塞问题；目标 pre-commit 全部通过。复核建议 Run 终态先于迟到子 Span，本用例已纳入该顺序并断言 Run 的 status/ended_at 不变。重复/乱序终态的真实 MySQL 证据仍缺失。

2026-11-18：用新建随机 `_test` schema 重跑 `test_mysql_span_end_replay_and_late_start_preserve_one_terminal_fact`，与资产 33 项分页、approval continuation、active-duration 合同同组执行，共 4 passed。重复 ended/conflicting terminal/late start 的隔离 MySQL 证据已补齐；schema 在测试后删除并确认本轮 agent-observability 隔离 schema 余数为 0。Inspector 与 Chromium108/360 验收仍未完成。

2026-11-18：新增独立真实 ToolGuard producer→Inbox→Projector→manager Run Detail MySQL 合同，覆盖结构化 `permission_denied` 与 `RuntimeError`。前者投影为 Tool `failed/OK/has_error=false`，根 Run 可保持 `succeeded`；后者投影为 Tool `failed/ERROR/has_error=true/error_type=RuntimeError` 并由合同结算根 Run `failed`。验证原工具各调用一次、跨 Source 404、参数和异常正文不进入 JSONL/API。随机 `_test` schema 两参数均通过（23.90s），删除后核验余数为 0；ToolGuard 单测 10 passed，目标 pre-commit 全绿。Tool Inspector 和 Chromium108/360 验收仍未完成。

同日继续：终态 MySQL 合同扩展覆盖同一 Span 上 conflicting parent 身份被隔离、重复 ended 不增加事件、late start 不覆盖失败终态，以及仅有 `quality.start_observed=false` 的自包含 ended Span 投影为 partial。该合同在随机隔离 `_test` schema 通过；期间发现 projector 原先仅凭 started_at 字段将明确标记未观察到 start 的 Span 错记为 observed，现改为尊重显式质量标记，调整后 MySQL 合同再次通过并确认隔离库已删除。相关 Observability core/ingest 单测 71 passed，目标 pre-commit 全绿。端到端 UI/浏览器验收仍未完成。

2026-11-26：PreToolUse STOP 上报 Guard 拒绝与受控原因 `hook_stop`。本地工具硬超时上报 `timed_out` 和 `tool_timeout`，保留技术错误标记。定向单测与隔离 MySQL 投影验证通过。
