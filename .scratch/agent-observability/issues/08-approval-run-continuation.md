# 08: 贯通审批等待与同Run恢复执行

Status: implemented-pending-site-verification
Type: task
Blocked by: 06
Triage: ready-for-agent
Priority: P0

**What to build:** 待审批问题在可观测页面保持同一个未终结Run，批准后进入新的Attempt；运营人员可以查看等待区间、连续审批及恢复关系，而不会把审批提示当作问题已成功。

**Blocked by:** 06：查看真实工具执行、拒绝与技术错误

**Status:** implemented-pending-site-verification

依据已确认的独立可观测设计、Trace视图对照、领域词汇及ADR 0047/0048。与旧SDK、旧埋点、旧追踪ID和旧可观测存储查询没有运行依赖；只输出元数据，正文仅经现有业务授权从NAS读取。采集由用户处理，本票不增加ES、TTL/清理或新身份体系；授权仍沿用项目已有身份判定。

- [ ] 在真实审批请求/决议和恢复入口产生独立事实；原业务Turn completed不结束待审批Run，当前Attempt以waiting_approval结束。
- [ ] 审批载体携带根Run、根问题、采集快照、原始Run时间、前驱Attempt和request身份，只含元数据；恢复不依赖人工日志已经入库，不按session最新消息猜。
- [ ] 批准后同Run新Attempt，连续审批保留最初根问题；既有业务幂等下的重复批准复用同一次恢复Attempt，不额外执行工具或抢占序号。
- [ ] 真实拒绝、过期、取消按业务裁决结束Run；记录丢失、重启无法恢复或无终态只显示unresolved/unknown，不自行恢复业务或推断失败。
- [ ] 投影和API提供独立等待状态、等待起止、决议与Attempt导航；UI区分Run总耗时、活动时间和审批等待，不把等待拼成连续Agent Span。
- [ ] 恢复/前驱关联使用Link语义，不制造跨Attempt普通父子；即使通用Links票尚未合入，本票须通过审批专用关系和导航独立演示。
- [ ] 覆盖审批跨业务Turn、连续审批、重复决议、先决议后请求、上下文缺失和快照继承；不修改原审批授权、执行或业务状态机。
- [ ] 完成等待→人工入库→批准恢复→同Run新Attempt的API/UI闭环，验证Source隔离与Chromium108/当前浏览器。

验收环境不可用时必须明确记录未验证项，不以构建成功或新浏览器模拟替代真实Chromium108验收。

## Comments

2026-11-15：用户已确认15张票的拆分及阻塞关系，现发布为本地实施事项。尚未领取或开工；本次发布不构成功能开发授权。
2026-11-17：匹配到 pending 审批但观测 carrier 缺失或校验失败时，续跑决议标记为 `unresolved`。业务审批命令仍沿用原处理路径，但不为当前请求新建观测 Run/Attempt，也不建立 deferred settlement；已补单元回归测试。此项只定义未知关联，不影响审批授权或业务状态机。
2026-11-17：验证 `tests/unit/observability/test_runner_lifecycle.py`（10 passed）、本地 MySQL `test_mysql_pipeline.py`（3 passed）及目标文件 pre-commit hooks。Chromium 108 浏览器实测及本票其余连续审批、重复决议、超时/取消和等待区间 UI 验收仍未完成。
2026-11-17：审批请求事件现在使用 ApprovalService 实际创建时间，解决事件保持同一等待起点；授权 Events API 返回受 scope 保护的 `attempt_id`，Inspector 显示受控决议文案、等待时长和关联 Attempt 导航。Stop 将 pending approval 标为 superseded 时，ApprovalService 从经 scope 校验的 carrier 发出 `approval.resolved`，Run 终态仍由原业务持久化 settlement 负责；只清除该已结束等待 Attempt 的 deferred 引用，避免同一 Attempt 重复写终态，普通 Stop 保留活动 Attempt。新增完整 `AnswerTurnCoordinator.claim_stop`→Workspace approval port→真实 `ApprovalService`→session persistence→deferred cancelled settlement 测试，断言审批决议先于 Run 终态且等待 Attempt 只结束一次。定向 Console 测试 25 passed，TypeScript/ESLint/Prettier 通过，Approval/Runner/AnswerTurn 单测 20 passed，MySQL approval continuation 专项 1 passed，全套 MySQL observability pipeline/lifecycle 14 passed。独立复审未发现 Stop 跨 Run 绑定错误；连续审批/重复外部决议覆盖、活动时间汇总和真实 Chromium 108/360 验收仍未完成，故保持 in-progress。
浏览器验收环境检查：本机 CUA browser host 不可用，未发现可执行的 Chromium/360 浏览器或 Playwright browser cache；本轮未执行真实浏览器验收。
2026-11-17：增加连续两轮审批恢复回归，确认审批 carrier 恢复同一 Run 与原问题身份，三段 Attempt 按 predecessor 连续编号；等待期间不发 Run 终态，每轮产生 requested/resolved 事实，最终只产生一次 succeeded Run 终态。重复同一已批准 request 再次进入 admission 时不恢复上下文，避免重复分配观测 Attempt。`tests/unit/observability/test_runner_lifecycle.py` 与 Stop 测试 14 passed，目标 pre-commit 通过。真实 ApprovalService 持久化幂等的端到端链路和 MySQL 连续审批尚未覆盖。

2026-11-17：扩展真实 MySQL approval continuation 合同为两轮连续审批，在同一隔离库投影后查询授权 Run Detail/Events；断言三段 Attempt predecessor 链、两个 approval requested/resolved 分别指向等待与恢复 Attempt、根问题保持不变、最终 Run 只结束一次。MySQL 专项 1 passed（14.73s），`test_mysql_pipeline.py` pre-commit 通过。仍缺实际 ApprovalService 重复外部决议持久化幂等、拒绝/超时/取消完整 MySQL 流程与浏览器验收。
Chromium 108.0.5359.29 实机页面检查被 dev server 的认证、有效配置和模板 API 500 阻断，审批 Inspector 未加载；该检查不构成兼容性验收。108.0.5359.95、360 内核与 `hideMenu=true` 仍待服务环境可用。

2026-11-18：Projector 为 `approval.resolved.decision` 增加受控枚举校验，仅接受 `approved/denied/timed_out/superseded`，未知值不作为自由文本持久化。定向 query/lifecycle/Stop 单测 47 passed，目标 pre-commit 通过。真实 ApprovalService 持久化的重复外部决议与拒绝/超时 MySQL 流程、浏览器验收仍未完成。

独立只读复核确认四个值覆盖当前所有生产发射路径，无具体兼容性问题。

同日使用隔离 `_test` schema 重验两轮 approval continuation 的 MySQL 投影/API合同，并与 pipeline、Runner admission 一起共 6 passed。真实 ApprovalService 重复外部决议持久化幂等、拒绝/timeout业务闭环和 Chromium108/360 验收仍未覆盖。

补充：新增真实 `ApprovalService` + `ApprovalAuditStore` 调用路径合同，覆盖 approved、denied、timed_out 首次决议及冲突重放；断言只持久化一条 resolved 事件，重放不改首次状态或 Future 结果。审批审计服务单测 6 passed。当前 shell 未配置 DB 连接环境变量，本轮未对该服务合同执行真实 MySQL；此前隔离 `_test` schema 的审批恢复投影/API验证仍为 6 passed。浏览器验收和完整拒绝/超时/取消 MySQL 流程仍未完成。

2026-11-18：新增 `tests/integrated/conversation_storage/test_approval_audit_mysql.py`，在 fixture 生成的隔离 conversation schema 上真实初始化 `ApprovalAuditStore` 并调用 `ApprovalService.create_pending/resolve_request`；参数覆盖 approved/denied/timed_out，随后提交相反决议重放。MySQL 实测 3 passed，验证 requests 表保留首次状态、events 表只有 created/resolved 各一条，resolved.status 与首次决议一致，Future 结果也未变化。随机控制 schema 和 fixture 子 schema 均清理，另查同名隔离 schema 余数为 0。审批 audit/service 单测 26 passed，目标 pre-commit 全绿。独立复核进行中；外部 HTTP/消息通道、拒绝/超时/取消 Runner 全链路及浏览器验收仍未完成。

独立复核指出缺少 scope 持久化断言；现已增加 `scope_id` 与 PendingApproval 身份一致，以及 tenant/source 精确断言。修复后隔离 MySQL 参数化合同再次 3 passed，单测 26 passed，目标 pre-commit 通过；复核确认 finding 已关闭，无其他可操作发现。完整外部通道/Runner 闭环和 Chromium108/360 验收仍未完成。

2026-11-18：新增独立 `test_approval_terminal_mysql.py` 参数化合同，覆盖 denied→rejected 与 timed_out→timed_out 的 MySQL 投影和授权 Run Detail/Events API；验证等待 Attempt 保留、approval ID 与 Attempt 关联、请求早于决议、Run 仅终结一次。使用随机 `_test` schema 按设计 DDL 初始化后实测 2 passed，测试后删除并核验 schema 余数为 0；相关生命周期/query/audit 单测 54 passed，目标 pre-commit 与 diff check 通过。独立复核指出该合同从 lifecycle hooks 发射事件，验证投影/API，不经过 Runner 的真实审批决议分支；该缺口保留为后续真实 Runner 拒绝/超时集成验证，本合同不代表完整业务闭环。浏览器验收仍未完成。

同日按复核意见升级上述合同：通过真实 `ApprovalService.create_pending` 创建待审批，并调用 `AgentRunner._resolve_pending_approval` 执行拒绝和超时分支；之后才将真实 Runner 发射的终态事件写入隔离 MySQL 并查询 API。针对内部状态 `timeout` 与观测决议 `timed_out` 分别断言。MySQL 2 passed（20.55s），随机 `_test` schema 清理回查为 0；目标 pre-commit 通过。此前“未经过 Runner 决议分支”的复核缺口已由此关闭；外部 HTTP/消息通道端到端和浏览器验收仍待完成。

同日继续复核发现 SQL 会话存储入口尚未贯通审批观测：`stream_indexed_query` 的普通输入先由 `IndexedQuery._admit` 新建观测 Run/Attempt，再执行真实审批 preflight；拒绝或非批准文本可能将原审批终态关联到新 Run。`/approve` 被识别为 daemon 命令，SQL 的 `_stream_command` 在审批被 preflight 消费后仍走命令分派，而非普通 Runner 的批准后恢复路径。既有 MySQL 合同直接绑定原 Attempt 调用 resolver，不能证明这两条入口正确。

一版按 pending 状态预查恢复 carrier 的尝试经独立复核发现存在预查与实际决议之间的并发和超时窗口，且未覆盖任意文本触发拒绝；该尝试已撤回，未作为修复交付。后续需以实际审批消费结果为准，在不改变既有业务审批授权及持久化语义的前提下，覆盖批准、拒绝、普通文本、超时边界、重复/并发决议和 carrier 缺失，从真实 SQL 入口到 MySQL 投影/API 验证。此发现也使本票原有端到端验收继续保持未完成。

2026-11-18：`ApprovalService.resolve_request_once()` 在原锁内返回本次是否首次改变状态，原 `resolve_request()` 保持返回类型兼容。Runner 仅首次决议发观测事实；重复/并发败者返回已处理提示，不再重复恢复工具。增加两个 Runner 先选中同一 pending 后并发批准的 barrier 测试，确认只恢复一次且只发一次观测；不同决议竞争也验证败者不产生终态。审批服务/审计、外部审批、专家主执行和生命周期定向单测 83 passed；审批终态与 ApprovalAudit MySQL 集成 5 passed，随机 schema 已清理并确认不存在；目标 pre-commit 全绿。

该局部修复不等于完整可靠的一次性审计：ApprovalService 在首次内存状态迁移后才通知 Future、唤醒 Goal、持久化及写审计；中间失败或取消会使后续重试看到已解决状态，审批审计/观测事实可能缺失。需在不改变现有业务授权/状态机前提下明确此缺失语义或形成可靠恢复设计。SQL IndexedQuery 的实际决议入口、批准恢复及外部通道到 MySQL 闭环仍未修复/验证，Ticket 保持 in-progress。

同日补充并发决议的局部保护：Runner 选中 pending 后，若其他请求先完成了相反决议，只有实际状态与本请求决议一致时才发出对应观测事件和终态。真实 ApprovalService 的批准、拒绝和超时冲突测试与生命周期单测共 35 passed；目标 pre-commit 全绿。独立复核指出同一决议并发仍可能重复发事件，且现有业务批准路径可能使用已被拒绝的 completed 记录继续恢复；此修改不改变业务行为，也不构成并发幂等或 SQL 入口闭环。后续需要服务锁内的首次转换标记，并以实际决议处理业务及观测状态。

同日继续：SQL IndexedQuery 将 Run/Attempt 创建延迟到 approval preflight 之后。Runner 在真实首次决议时恢复并校验 carrier，通过 task-local callback 将 Run 上下文、决议和 changed 状态交给当前 SQL Turn；拒绝/超时不创建替代 Run，批准在原 Run 下创建新 Attempt。`/approve` daemon 命令进入 SQL admission；preflight 消费批准后走既有 `_stream_query_after_preflight` approved-tool 恢复，由外层 IndexedQuery 单次结算；普通 daemon 命令仍沿旧命令路径。普通请求 preflight 异常/取消会补建 Run/Attempt 后保留原异常。

验证：SQL Runner、审批服务、Expert Main、observability lifecycle 定向单测 111 passed；目标 pre-commit 全绿、`git diff --check` 通过。conversation-storage Runner 的 22 项真实 MySQL 集成套件通过，随机 control schema 及 fixture 子 schema 均清理；该套件验证通用 admission/terminal/recovery 合同，不直接驱动本次新增的审批命令 SQL 分支。新审批命令的 Attempt 转接目前由单测覆盖，尚无完整 SQL MySQL→投影→授权 API 专项合同。外部 channel 的完整生命周期、审批审计中断恢复和浏览器验收仍未完成，Ticket 保持 in-progress。

同日补充 SQL 审批 MySQL 合同：新增 `tests/integrated/conversation_storage/test_approval_observability_sql_mysql.py`，在随机 conversation schema 中安装观测 DDL，使用真实 `ApprovalService`、`stream_indexed_query`、SQL admission/terminal、JSONL、Inbox/Projector 与 manager Run Detail API。拒绝和 `/daemon approve` 两种场景均只关联原 Run；批准创建第二个 Attempt 并指向等待 Attempt。隔离 MySQL 2 passed，控制库和 fixture 子库清理后确认不存在；定向单测 111 passed，目标 pre-commit 和 diff check 全绿。

此合同构造待审批起点并替换模型恢复流，因而不代表完整真实工具执行或外部 channel 已端到端验收；还需覆盖 timeout、carrier 缺失/错误 scope、连续审批及 SQL→授权事件 API，并处理审批审计中断恢复。页面及浏览器验收继续按用户要求暂停。

同日扩展：SQL 审批 MySQL 合同现覆盖四种场景：deny→rejected、`/daemon approve`→原 Run 新 Attempt 并 succeeded、超时→timed_out、carrier agent scope 不匹配时业务决议仍按原授权执行但不伪造/替代观测 Run。四项均经真实 SQL admission 与 Inbox→Projector；前三项查询 manager Run Detail 与 Events API，校验决议值及 Attempt 关联，错 scope 用例确认不生成替代 Run。隔离 MySQL 4 passed，随机控制库/fixture 子库已删除并核验；相关单测 111 passed，目标 pre-commit 与 diff check 全绿。尚未覆盖连续审批的 SQL+MySQL 合同、真实外部 channel 投递、真实工具副作用及审批审计持久化失败恢复；Ticket 保持 in-progress。

同日继续：SQL MySQL 合同新增两次连续批准，验证原 Run 不变、三个 Attempt 的 predecessor 链、两条 resolved 事件分别关联等待 Attempt，以及 Run 只结束一次；完整测试文件 5 passed，随机控制库和 fixture 子库已清理。一次完整运行期间远端 MySQL 断线，产生 UnknownCommit 并阻断 fixture 清理；已重连并仅删除该次明确标识的两个随机测试库，再以新库完整重跑通过。测试仍以替身生成第二次待审批和恢复结果，不代表真实工具或外部 channel 全链路；审计持久化中断恢复及页面验收仍待完成。

同日复核修复 carrier 缺失/作用域不匹配时的串写风险：Runner 不再回退到任意当前 task 的观测上下文，只有经过完整身份校验的审批 carrier 可用于 resolved 和 Run 终态；业务决议行为不变。新增真实 ApprovalService 回归，覆盖 carrier 缺失及不匹配且当前绑定其他 Run；相关单测 81 passed。原拒绝/超时 MySQL 合同改为携带规范 scope 的 carrier 和请求身份，隔离库实测 2 passed，随机 schema 已删除并核验；目标 pre-commit 全绿。

同日继续：同步审批观测回调抛异常时不再中断已发生的业务审批决议；SQL preflight 已消费审批但观测交接缺失时不创建替代 Run。真实 ApprovalService 拒绝和 SQL preflight 降级单测纳入回归，相关单测 83 passed，目标 pre-commit 全绿。该保护不处理审批服务自身持久化失败或取消后的恢复，相关缺口仍未关闭。

2026-11-18 同日补充：`resolve_request_once()` 现在接受仅同步的 `on_transition` observer，并在首次内存状态转换后、Future/Goal/审计持久化等 await 之前调用；observer 异常在 ApprovalService 内被隔离，后续业务审计异常/取消仍按原行为传播。Runner 将经校验 carrier 对应的决议事实放入该回调，因此 SQL preflight 在审计 I/O 失败前已收到“审批已消费”，不会错误创建普通问题 Run。新增“审计持久化抛错但 transitioned callback 已发生”和 SQL preflight 异常期间保持原审批 Run 的单测；相关单测 85 passed，目标 pre-commit 与 diff check 全绿。

随后重新实测 SQL 审批拒绝/批准/超时/scope mismatch/连续审批及 observability Approval Detail/Events 合同，共 7 passed，使用分离的 conversation 测试库和安装了观测 DDL 的 observability 测试库；结束后两库及 fixture 子库均删除并核验。首次复跑曾因测试未初始化 observability schema 而 readiness=false，修正测试环境后全组通过。仍不提供 ApprovalAuditStore durable/outbox recovery，真实工具副作用和外部消息/HTTP channel 全路径及 UI/浏览器验收未完成，Ticket 保持 in-progress。

同日后续完成审计补写：`resolve_request_once()` 对已完成的 approved/denied/timeout 决议重放时，保持原状态、Future 和 `changed=False`，同时重试幂等 request upsert 与 resolved event。`ApprovalAuditStore` 的 resolved event 在插入前按 request/event type 查重；服务内独立 audit lock 串行化首次写入与重放修复，避免同一实例同决议并发重复写入。新增真实 MySQL 测试注入一次 resolved event 瞬时写失败，再以相反决议重放，确认请求状态未改且最终恰有一条 resolved event；另测并发相反决议也只留一条终态事件。Approval audit MySQL 5 passed，审批服务/审计/外部渠道/专家执行/SQL Runner 单测 118 passed，目标 pre-commit 全绿，随机库已清理并核验。恢复依赖同一进程后续收到该审批 ID 的重放；没有后台 retry/outbox，进程重启后的内存决议无法由此机制恢复，也不提供跨服务实例的数据库级 unique-key exactly-once。真实工具、外部 channel 全执行链、UI/浏览器验收仍未完成。

同日再补取消路径：对 `_persist_request` 注入 `CancelledError`，验证业务审批状态已迁移、carrier transition observer 先收到 resolved 决议，然后取消原样传播；请求重放仍可触发审计补写逻辑。相关审批审计/服务/Runner 单测 119 passed，目标 pre-commit 全绿。该轮 MySQL fixture 测试在未设置专用连接环境变量时 skipped；前述真实 MySQL 的瞬时 resolved-event 写失败与并发合同保持 5 passed 的证据。仍无重启后的 durable outbox/recovery、真实工具副作用或外部 channel 全路径；Ticket 继续 in-progress。

最新复核：在实际切换到 `on_transition` 后，重新运行 SQL 审批、审计和终态合同时，10 个 admission/审计场景通过；组合运行的两个独立终态用例曾因远端连接/提交超时失败，随后在新建且明确跟踪的 observability schema 上单独重跑拒绝与超时合同 2 passed，并删除核验该 schema。组合运行的清理连接也断开，当前发现两个匹配随机命名模式的库，但未能唯一证明属于该运行，故未删除。该资源性超时保留为测试环境限制。

2026-11-18 后续复核发现 Runner 原先只选择 pending 记录，服务层重放补写无法从重复审批命令到达。现对带明确 request ID、同 scope、同 session 和同用户的已完成决议调用审计补写，并返回已处理响应，不再次恢复工具或执行拒绝记忆清理；非 SQL admission 将同用户的重复决议标为 unresolved，避免新建 Run。Runner 入口的真实 MySQL 瞬时审计写失败后反向决议重放验证包含在审计测试中，共 5 passed，随机控制库已删除并核验；相关单测 123 passed。补写仍只覆盖同进程保留的已完成记录，不保证跨实例原子去重或重启恢复；无 ID 重放无法定位历史决议，SQL 同一消息 ID 的幂等重试在预检前返回历史结果，也不会触发补写。

同日继续：SQL `IndexedQuery` 对已存在问题直接返回授权历史，重放不会进入预检。现基于 `answer_turn` 返回的持久化原始用户消息 ID 和审批 request ID，校验 ApprovalService 当前 scope、session、user 后调用幂等决议审计补写；重试正文不作为识别依据，不重新执行模型/工具，也不改变原历史响应。单测验证匹配身份补写与会话不匹配跳过，conversation-storage/approval/lifecycle 定向单测 90 passed；隔离 MySQL `test_same_message_replay_repairs_approval_audit` 1 passed，确认相同 question ID 重放补齐失败的 resolved event，随机控制库已清理并核验。代码检视未发现授权绕过；目标文件 pre-commit 与 diff check 通过。跨服务实例原子唯一性及重启恢复仍不在此机制覆盖范围。

2026-11-18 复验：使用全新空 conversation 控制 schema 重跑 `test_approval_observability_sql_mysql.py`，6 passed（190.38s），覆盖相同消息审计补写、拒绝/批准/超时、连续审批和 carrier scope 不匹配；随机控制 schema 删除并核验不存在，逐用例子 schema 由集成夹具清理。恢复流仍由替身提供，本合同不证明真实工具副作用或外部消息通道全链路；审批持久审计/进程重启恢复与浏览器验收仍未完成。

2026-11-18 后续复验：SQL 审批 MySQL 合同扩为 7 个用例并整文件重跑，7 passed（128.03s）；新增用例真实调用 `submit_external_approval_decision`，将外部决议提交审计写入 ApprovalAuditStore，再由测试 Console bridge 把生成命令送入真实 SQL admission/Runner 决议，最后经 Inbox/Projector 查询授权 Run Detail 与 Events API。确认外部 `decision_submitted` 与 resolved 审计、原 Run 的等待/恢复 Attempt 关系及单一 Run 终态；临时控制 schema 删除并核验不存在，逐用例随机 schema 由 fixture 清理。该合同未连接真实 zhaohu/Console 网络通道，批准后的模型/工具恢复仍使用替身；真实工具副作用、外部通道投递、进程重启后的 durable audit recovery 与页面验收仍未完成，Ticket 保持 in-progress。

独立代码检视确认上述新用例自身无断言错误，但其 Coordinator 是仅保存 producer 的假对象，测试随后手动调用 producer，因此不覆盖生产 AnswerTurnCoordinator/TaskTracker 的锁、stream 注册与状态生命周期；也直接调用 external helper，未经过 Router 的 workspace/请求身份授权。初始 Run/pending 仍由测试 helper 手工建立，恢复模型/工具执行仍被替换。故该结果只作为外部 helper 到 SQL admission/观测投影的部分合同，不作为外部通道完整闭环证据；后续仍需真实 Coordinator、Router 授权与 ToolGuard 起点合同。

复核另发现提交时序竞态：生产 TaskTracker 可在 `start_or_attach()` 返回后立即执行 producer，而 `record_external_submission()` 原先在该 await 后，producer 可能先进入 admission。现以 task-local Event gate 保证 producer 等待外部提交审计更新完成；协调器或审计阶段异常/取消会释放 gate 并让 producer 退出。MySQL 合同改为在 `start_or_attach()` 内抢先调度 producer，并在 Console bridge 确认 DB 审计已记录后才调用 SQL admission；断言请求表最终 approved、scope/tenant/source 与提交者字段，以及 resolved 事件状态。该 race 合同隔离 MySQL 1 passed（24.37s），conversation fixture schema 前后差异为 0，临时控制 schema 已删除核验；外部审批单测 7 passed，目标 pre-commit 全绿。全文件回归待运行。该修复不覆盖生产 AnswerTurnCoordinator/TaskTracker、Router 请求授权或 ToolGuard 建立起点，Ticket 仍为部分完成并保持 in-progress。

随后新增 API Events `metadata.decision == approved` 断言；race 合同在最终断言下再跑 1 passed（22.65s）。gate 改动后的 SQL 审批文件全量重跑 7 passed（133.73s），控制 schema 删除核验为 0，conversation fixture schema 差异为 0；外部审批单测 7 passed，目标 pre-commit 全绿。独立复核确认新 producer 先被调度至 gate、审计写完才继续，未发现提前执行或永久等待；当协调器已接收新流但后续审计被取消时，producer 会空流结束，TaskTracker 清理未在本合同单独覆盖。生产 Coordinator、Router 授权、真实 ToolGuard 起点/执行与这一取消清理场景仍未覆盖，Ticket 保持 in-progress。

2026-11-19：审批 SQL MySQL 全量回归首次暴露外部审批测试 double 未实现生产新增的 `mark_producer_started`/`cancel_unstarted` 生命周期接口；补齐后失败用例单独通过（1 passed，22.57s），整文件重跑 7 passed（134.88s）。控制 schema 与 fixture 子 schema 删除并确认不存在，目标静态检查通过。该修复只同步测试 double 的已发布 Coordinator 契约，不扩大真实外部通道、Router 授权、工具副作用或进程重启恢复的验收范围，Ticket 保持 in-progress。

同日继续：新增真实 SQL/MySQL 取消结算合同。等待审批 Run 先结束 Attempt 为 `waiting_approval`，取消通过 deferred settlement 清除 Attempt 所有权后结束根 Run 为 `cancelled`；投影/API 保留 waiting Attempt，不重复写 cancelled Attempt 终态，也不伪造 `approval.resolved`。隔离 MySQL 1 passed（9.97s），控制 schema 与 fixture 子 schema 清理并确认不存在；目标文件 pre-commit 通过。该合同覆盖观测结算顺序，不替代真实 Stop/Coordinator 进程级取消、外部通道和审计中断恢复验收，Ticket 保持 in-progress。

后续补充真实 `TaskTracker` 与 `AnswerTurnCoordinator` 的取消生命周期合同：外部提交审计在新 producer 启动前取消时，空流原会让 Coordinator 遗留 `RUNNING`；Stop 若先将 gate 中的 Turn 转为 `STOPPING`，也会令 producer 退出而不结算。现为 gate turn 显式记录 producer-started 状态，成功审计后才标记业务 producer 开始；取消或 Stop 已先发生时，在同一 per-chat 锁下原子认领并持久化 `CANCELLED`，关闭 ReplayStream 并清理 Coordinator/TaskTracker。测试覆盖取消传播、持久终态、stop-before-gate-release、不进入 ConsoleChannel，以及并发 mark/cancel 的线性化；独立复核确认无遗留竞态。外部审批、Coordinator/TaskTracker 与 Stop 定向回归 52 passed，目标 pre-commit 全绿，`git diff --check` 通过。真实 MySQL取消审计时序和浏览器验收仍未执行，Ticket 保持 in-progress。

2026-11-26：新增本地外部审批 HTTP 到真实 ToolGuard 和工具恢复链路，目标外部通道和现场数据库待验收。 [本轮交付](../followup-completion-report.md)
