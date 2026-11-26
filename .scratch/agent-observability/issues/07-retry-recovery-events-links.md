# 07: 追踪重试、恢复及分页Events/Links

Status: implemented-pending-site-verification
Type: task
Blocked by: 05, 06
Triage: ready-for-agent
Priority: P0

**What to build:** 运营人员能够从模型或工具错误看到后续实际重试与恢复过程，通过分页事件和类型化关联定位相关操作，而不会把恢复后的根结果与历史技术错误混淆。

**Blocked by:** 05：查看真实模型调用、用量与首输出延迟；06：查看真实工具执行、拒绝与技术错误

**Status:** implemented-pending-site-verification

依据已确认的独立可观测设计、Trace视图对照、领域词汇及ADR 0047/0048。与旧SDK、旧埋点、旧追踪ID和旧可观测存储查询没有运行依赖；只输出元数据，正文仅经现有业务授权从NAS读取。采集由用户处理，本票不增加ES、TTL/清理或新身份体系；授权仍沿用项目已有身份判定。

- [ ] 接入模型层实际可见请求尝试及工具真实重试/恢复事实；模型重试保持原Attempt，逻辑调用数、请求尝试数及Usage分别计量，内部不可见重试不伪造。
- [ ] 增量span.event采用受控event_type、唯一拥有者序号及固定生命周期槽；单事件不是新的执行区间，不凭它伪造Span开始，专用审批/SubAgent事件不再重复计同一事实。
- [ ] 实现事件与Links的投影、授权分页API和Inspector，按事实ID/固定关系键去重；单数组最多32项不意味着整个Span只能显示32个事实。
- [ ] 事件先于Span到达时先保留安全事实，生命周期后到需重新关联；第33个以后事实仍可读取，metadata不无限增长，重复/乱序入库最终摘要一致。
- [ ] retry_of、retry_next、cause和recovered_by等关联有明确类型；Link不画成parent，目标按同样scope重新鉴权，跨域/不存在统一不可用。
- [ ] 已恢复Span仍保留已知错误和恢复证据，后到成功不擦除错误；根业务成功仍可成立，首错误不强称根因，不从时间先后推断因果。
- [ ] 默认事件100条/最多500、响应字节预算、稳定时间/事件ID游标和必要继续键可验证；安全Raw只返回规范元数据。
- [ ] 以真实重试与恢复完成埋点→入库→关系/事件查询→Inspector验收，覆盖超过32个事实、缺阶段、冲突及Usage不重复；验证目标浏览器。

验收环境不可用时必须明确记录未验证项，不以构建成功或新浏览器模拟替代真实Chromium108验收。

## Comments

2026-11-15：用户已确认15张票的拆分及阻塞关系，现发布为本地实施事项。尚未领取或开工；本次发布不构成功能开发授权。

2026-11-16：接入 RetryChatModel 可见重试事件，并新增 Run/Span 授权事件分页 API 与 Console Inspector；事件游标绑定角色、Source、Run 和可选 Span，响应受 500 条/1 MiB 预算限制。隔离 MySQL 用例从 JSONL、inbox、投影到三页读取 44 条事件，验证无遗漏/重复及 Source 隔离；观测单测 39 项、API/UI Vitest 9 项通过。修复 Span 事件身份字段、默认 kind 与可空 error_type 校验。Ticket 仍保持 open：工具重试/恢复、类型化 Links、事件迟到与跨页完整验收、目标 Chromium 108 验收尚未完成；05/06 依赖仍未解除。

2026-11-17：使用独立 `swe_agentobs_codex_20261116_test` schema 验证，MySQL observability 集成 9 passed；观测及 Provider 定向单测 70 passed；Console API/Inspector Vitest 16 passed；TypeScript、目标 ESLint、生产构建和涉及 Python 文件的 pre-commit 均通过。补充模型 request-attempt 的 MySQL 生命周期断言，并拆分事件解析/Link 目标校验 helper以满足 pylint。`events.example.jsonl` 的重试/恢复记录改为受控名称、事件类型和单 Span Link 目标。工单仍保持 open：工具侧真实重试/恢复生产埋点和真实 Chromium 108.0.5359.95/360 验收未完成。

2026-11-17 追加：Ticket 05 汇总接入及真实 Runner 流式 Provider 覆盖后回归，隔离 MySQL observability 集成 10 passed；Observability/Provider 定向单测 72 passed；Console API/Inspector 16 passed；生产构建、TypeScript、API/UI、mypy、black、flake8、pylint 的目标检查通过。当前只证明模型 request retry 生产观察路径；工具 retry/recovery 发射方仍未实现，工具样例不代表生产已观测事实。Chromium 108.0.5359.95/360 仍未在本机可用环境验证。

2026-11-18：重新核对生产工具执行器后确认目前没有 Tool 自身自动 retry/recovery 状态机，Runner 级 Agent 重建会新建 Attempt，不能映射为 tool.retry_scheduled 或 recovered_by。本轮未伪造工具 retry 事实；该生产观察点仍是明确缺口。模型 request-attempt 路径继续作为唯一已接入的 retry 观察事实。工具重试/恢复 owner 尚未存在，因此 Ticket 07 保持 open。

同日复验：完整 `tests/integrated/observability/test_mysql_pipeline.py` 在全新随机 `_test` schema 通过 10 项（约 177 秒），包含超过32条模型 retry/event 分页、`link.added` 类型关系、目标不存在脱敏、跨 Source 授权、重复/迟到 Span、Assets/Evidence、祖先和性能合同。schema finally 删除并确认不存在。生产工具 retry/recovery owner 仍不存在，故未添加虚构事实；Ticket 保持 open。

2026-11-18 后续：真实 `RetryChatModel` 合同增补 `on_retry` 生产同款 callback，而非仅合成请求 attempt；一次 429 后成功的隔离 MySQL→Projector→Performance/Run Detail/Links API 测试现在断言首次 request-attempt 仍为 `failed/TransientProviderError`，第二次为 `succeeded` 并含可用 `retry_of` Link，Run 最终为 `succeeded`，逻辑调用=1、物理 attempt=2、Usage 只计一次。合同在新随机 `_test` schema 通过，schema 删除并核验不存在；目标 pre-commit 全绿。Tool 自动重试状态机仍不存在，页面/Chromium108验收仍未完成。

2026-11-18 后续：补齐非流式空模型输出触发的生产重试事实。`RetryChatModel` 在空输出重试前通过 `on_retry` 发出 `EmptyModelOutputError` 类型，JSONL 合同确认首 request-attempt 失败、第二次成功并以 `retry_of` 关联，空输出重试与瞬时 Provider 错误可区分；原重试次数和立即重试行为不变。模型调用、观测 core 与 Provider 定向套件 62 passed，目标 pre-commit 全绿。当前生产没有 Tool 自动重试状态机，未添加 Tool retry/recovery 事实；Ticket 保持 open。

2026-11-26：已核对现有实际边界，补齐模型请求和 Agent Attempt 的重试与恢复关系，工具不存在独立自动重试执行边界。已在目标 MySQL 8.0.46 的独立测试库验证重试、恢复和授权 API，现场浏览器待验收。 [测试报告](../followup-test-report.md)
