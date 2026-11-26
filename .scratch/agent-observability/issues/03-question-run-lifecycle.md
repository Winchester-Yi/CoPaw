# 03: 展示真实问题的Run生命周期与采集状态

Status: claimed
Type: task
Blocked by: 02
Triage: ready-for-agent
Priority: P0

**What to build:** 一次真实主对话或专家主执行问题能够通过独立日志关联为一个Run，人工入库后展示受理、运行、业务终态及采集质量；观测开关或故障不会改变原业务行为。

**Blocked by:** 02：打通人工入库到授权Run列表的最小闭环

**Status:** claimed

依据已确认的独立可观测设计、Trace视图对照、领域词汇及ADR 0047/0048。与旧SDK、旧埋点、旧追踪ID和旧可观测存储查询没有运行依赖；只输出元数据，正文仅经现有业务授权从NAS读取。采集由用户处理，本票不增加ES、TTL/清理或新身份体系；授权仍沿用项目已有身份判定。

- [ ] 在真实受理/结算边界调用已发布的独立内核，主对话与专家主执行均可从实际问题到日志、投影、API及页面闭环验收，不以旧SDK状态为事实来源。
- [ ] Run只代表一次根问题；新问题和终态后的重执行创建新Run并可关联前驱，重连不创建，终态不可被晚到子事实重开。
- [ ] 准入时固定全局采集快照，后续执行继承；每个Run上下文携带原始run_started_at、capture_enabled、完整scope与根问题锚点，accepted丢失仍可构建partial根记录。
- [ ] 仅按已发生的业务裁决映射成功、失败、取消、超时或拒绝；等待审批不能因业务Turn completed误判Run成功，为审批专票预留独立等待语义。
- [ ] 缺日志、长时间未更新、进程中断和不明终态显示unknown/partial/unresolved；不由elapsed或查不到状态推断失败，不阻塞后续独立问题。
- [ ] 页面展示安全采集能力、数据质量和截至信息；全局observer健康仅admin可读，不泄露连接秘密或将全局健康按Source伪分配。
- [ ] 覆盖新旧开关组合、关闭低成本、取消/异常传播、观测DB不可用及输出降级的业务回归；采集传输由用户负责，不纳入验收。

验收环境不可用时必须明确记录未验证项，不以构建成功或新浏览器模拟替代真实Chromium108验收。

## Comments

2026-11-15：用户已确认15张票的拆分及阻塞关系，现发布为本地实施事项。尚未领取或开工；本次发布不构成功能开发授权。

2026-11-16：Ticket 02 已完成并解除依赖。本轮开始将独立 Run/Attempt 上下文接入普通 Runner admission，覆盖 SQL 与 legacy 会话路径；尚需补齐终态事件与审批等待回归、重连/重复受理证明及 Chromium 108 页面验收。

2026-11-16：本轮已完成普通 admission 的 accepted/running/terminal 事件接线、SQL/legacy 重连去重、审批等待状态、默认租户 scope、取消早于 stream finally 的结算顺序，以及 metadata-only 前驱 Run carrier；受影响后端回归 189 项通过。仍保持 claimed：无 answer-turn coordinator 的 legacy Runner 以及完整真实主对话/专家主执行、Chromium 108 页面验收尚未在本票完成；审批续跑归并继续由 Ticket 08 处理。

2026-11-16：新增 manager/admin 能力状态与 admin-only 全局 Health API；Run 状态缺失的 Console 行显式显示 `unknown`，质量仍独立显示。后端 observability 单测 32 passed，Console 类型检查通过，相关 Vitest 7 passed、ESLint 通过。完整真实主对话/专家主执行验收及精确 Chromium 108 补丁版仍待完成。

2026-11-16：修正 Run 质量投影：accepted/running/waiting 状态保持 `partial`，仅已提交业务终态 `run.ended` 标记 `observed`；乱序下后到终态提升质量，晚到 accepted 不降级。隔离 MySQL 8.0.46 集成测试已覆盖 accepted→ended。

2026-11-16：Run 非终态 status 使用严格较新的事件 `occurred_at` 更新，较旧或同刻的 accepted/running 不回退当前状态；MySQL 实测覆盖 running→旧 accepted、waiting_approval→旧及同刻 running，终态仍优先。受影响回归 191 passed，MySQL 集成复跑 1 passed。

2026-11-16：新增真实 Runner admission 与 `AgentRunner.persist_outcome`→JSONL→白名单校验→隔离 MySQL 投影→manager API 集成测试，使用受控成功执行流而非真实 Provider；首次实测发现 Attempt 结束时间污染 Run ended_at、阻止 run.ended 投影，现仅 run.ended 能结束 Run。Ticket 仍保持 claimed：真实模型/专家主执行、无 coordinator legacy 路径和 Chromium 108 精确现场补丁版尚未验收。

2026-11-16：已在隔离 MySQL 集成测试覆盖有 coordinator 的 `persist_outcome` 结算，以及无 coordinator 时从 durable turn terminal status 结算；两条路径均产生 run.ended 并通过 manager API 查询。真实模型/专家主执行与 Chromium 108 精确现场补丁版仍未验收。

2026-11-16：补入真实 `ExpertMainRegistry`/`ExpertMainLease` 的 Runner admission 集成覆盖，成功执行投影为 succeeded；Lease 中存在待审批工具请求时，即使 Turn completed 也保持 waiting_approval、Attempt 已结束且 Run 不终结。Run 结束事件现携带本进程单调时钟 duration_us；跨进程无法比较时长时不伪造值，MySQL 验证了 Run 与 Attempt 结束时间分离。受影响回归 28 passed。仍未验收真实 Provider/完整应用请求与 Chromium 108.0.5359.95。

2026-11-16：Chrome 157 实测 Console manager/admin 页面及 `hideMenu=true`：manager 无全局 Health，admin 可见累计丢弃状态；360x800 与 390x844 页面无横向溢出，窄屏表格可横向滚动至末列。该 UI 检查通过本地 iframe store 和 mock API 驱动；本地未启动后端，其他全局接口返回 500。Chromium 108.0.5359.95/360 极速模式仍未验收。

2026-11-16：新增真实普通主对话集成用例：本地确定性 Provider 驱动真实 `AgentRunner.query_handler`、SWEAgent/RetryChatModel 和 admission，随后验证独立 JSONL、白名单脱敏、隔离 MySQL 投影及 manager API；该用例与 lease 分类用例合计通过 observability 集成回归。真实 Provider 网络调用、专家主 Agent 完整执行和指定 Chromium 108.0.5359.95 仍未验收。

2026-11-16：将真实 Runner 用例扩展为普通与专家主执行两种模式。专家模式由服务端 ExpertMain Registry 注册受限快照并校验请求身份，实际 SWEAgent 在确定性 Provider 上完成问题；输出携带 `execution_identity.kind=expert_main`，Run/Attempt 生命周期经 JSONL、隔离 MySQL 投影及 manager API 验证。两种模式均通过。Ticket 继续保持 claimed：指定 Chromium 108.0.5359.95/360 极速模式与真实外网 Provider 尚未验收。

2026-11-16：进一步让生命周期与模型 Span 共用生产同款的模块级 default emitter，在真实标准/专家 Runner MySQL 用例验证同一 JSONL 同时包含 Run/Attempt 和 LLM Span；投影后数据库与 Run 详情 API 的 input/output Token 均与 Span 结束事件一致。此断言发现并修复 span UPSERT 未合并结束事件 Token 的缺陷。两种模式 2 passed，observability 单元/模型关键路径 39 passed，相关 pre-commit 全部通过。仍待指定 Chromium 108.0.5359.95/360 验收和真实外网 Provider 验收。

2026-11-18 后续：增加准入采集快照回归，分别验证准入时 enabled 后全局开关关闭仍沿用该 Run 的 capture snapshot，以及准入时 disabled 后全局开关打开不为既有请求补造 Run/Attempt。两个方向通过，生命周期单测文件 2 项回归通过；目标文件 pre-commit 全绿。真实 Chromium108/360 页面验收按用户要求暂停。

2026-11-26：可重试的 Agent 失败在退避前结束当前 Attempt，退避完成后创建继任 Attempt。退避中取消的定向回归验证失败 Attempt 已结算且无继任 Attempt。
