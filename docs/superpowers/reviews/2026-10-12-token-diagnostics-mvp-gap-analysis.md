# Token diagnostics：75e1ade3 与 MVP v1.0 差距分析

日期：2026-10-12。状态：核心范围已收敛；用户暂缓专门 Usage 核对工作，相关验收门槛移出本轮；本文不是已批准的实现方案。

## 分析边界与结论

目标提交：`75e1ade30ccb47d09f89475e3c7500811b55ffd4`，父提交：`99a2d861446a55415a9e54fb560f7895c1172a95`。当前工作区 HEAD：`458e80cd3c8926b6024b7575036070da44598052`，包含该提交。

方案依据：[Agent Token 效率诊断平台 MVP v1.0](../../../Agent_Token_效率诊断平台_MVP_方案设计_v1.0.md)。既有约束见根目录 AGENTS.md、CONTEXT.md 与 [ADR-0037](../../adr/0037-token-diagnostics-own-records-and-share-tracing-enablement.md)。

提交新增 65 个文件范围的修改，6244 行新增、25 行删除，实现 SWE 本地采集 → 独立 Token 表 → Monitor 异步规则分析 → Console 单页诊断。它提供了可继续开发的记录底座，但尚不满足新版 MVP 的三页面、八规则、前缀机会与人工整改闭环。

已核验目标提交到当前 HEAD 在 `src/swe/token_diagnostics/`、Monitor Token services、Console TokenDiagnostics 页面和 Token DDL 上无差异。其他集成入口存在于合并后的仓库；本文不声称完成所有 65 个文件的逐行安全审查。

## 第一轮已确认范围

用户于 2026-10-12 确认：

- **Q1：首批 SWE/Monitor 增量开发。** 明确采集事件版本与扩展边界。外部 Agent/Gateway 通用接入不属于本轮交付。
- **Q2：首个里程碑 A 观测底座。** 初始范围为合规存储、任务归因、事件与真实用量核对；用户随后暂缓专门核对工作，当前本轮范围为合规存储、任务归因、事件与完整性状态。B 的三页面/八规则/前缀分析、C 的反馈/比较/策略作为后续目标。

A 可以建设后续分析所需采集字段，但不因此扩大为 B/C 的产品交付。

### 第二轮事实补充与已确认决策

请求执行已有 `TurnIdentity(chat_id,msgid,turn_id)`；附着到活跃执行复用该身份，新执行创建新身份。Console、Channel 与 Runner 会传播该身份。Provider/运行时基础请求 ID 可空，不能假定每个入口都有统一请求 ID。定时执行有自己的执行身份，不能仅用 session_id 或 B3 ID 归因。

Token 表在仓库中通过独立 `CREATE TABLE IF NOT EXISTS` SQL 和部署说明人工建立；未找到 Token 自动迁移/版本登记入口。用户已确认这五张表尚未建立，无需考虑迁移。

用户确认 Q3–Q7：

- **Q3 Task 主粒度**：一次根请求目标执行（含子 Agent/辅助调用）；附着/恢复同一执行不重复建 Task，后续请求另建 Task。跨轮业务目标聚合不属于 A。
- **Q4 Trace 依赖**：继续独立记账，保持 ADR-0037 的可选关联；Trace 缺失或写入失败不阻止 Token 记录。新版通用方案的必填 Trace/Span 字段在 SWE 首期按关联可得性适用。
- **Q5 原核对方向，现暂缓**：原决定 Provider 返回 Usage 与账本逐请求核对、单列未知/缺失/覆盖；用户最新要求暂不考虑专门核对脚本、真实模型对照和报告，因此 ≤1% 真实偏差不作为本轮交付门槛。用量采集与基本数字/状态回归继续保留。
- **Q6 故障交付承诺**：沿用业务优先、非阻塞采集；明确缺口与完整性状态，并验证数据库故障、缓冲超限和进程退出等行为。不承诺崩溃后恢复交付。
- **Q7 历史兼容要求**：五张 Token 表尚未建立，不需要考虑迁移；直接修订初始 schema 及配套读写。不增加旧 JSON schema 的兼容读写、回填或双写路径。

Q3 对应 Task/Run 定义及 Q5 对应核对概念已同步至 CONTEXT.md。Q4/Q6 延续 ADR-0037；Q7 采用初始 schema 修订。其余字段与验收细节仍须完成设计，不将核心口径确认视为全部方案确认。

后续 A 的采集与验收设计汇总至[观测底座 A 设计草案](../specs/2026-10-12-token-diagnostics-observation-foundation-design.md)。Q8 消息级近似边界保持有效；Q9 的专门核对覆盖门槛随核对工作暂缓，基础回归仍验证预期记录不漏不重。无需本轮选择 Q10 首批真实 Provider/模型。

## 已有能力与边界

| 能力 | 已实现事实 | 证据 |
| --- | --- | --- |
| 用量记录 | 可观察 adapter attempt 单独记录；流式、中断、未知用量保留状态 | `src/swe/token_diagnostics/model_observer.py:89–171`、`models.py:7–30` |
| 请求归属 | Main/SubAgent/辅助调用共享请求运行归属，分支独立；Trace 为可选关联 | `context.py:74–109,158–173`、ADR-0037 |
| 输入明细 | message/tool schema 条目、来源、HMAC、UTF-8 密度估算；媒体和预算不足显式降级 | `input_capture.py:60–98` |
| 工具及压缩 | 工具开始/结束与指纹、压缩周期事件 | `context.py:205–242` |
| 持久化与分析 | 独立连接/事务、记录 revision 幂等、分析租约与版本校验 | `src/swe/token_diagnostics/store.py:61–130`、Monitor `analysis.py:17–48` |
| 规则 | 五类：工具结果重复携带、重复注入、工具重复、历史增长、输出预算 | Monitor `rules.py:9–16` |
| 展示 | 当前 source/tenant 的运行列表、分支/调用/输入与规则证据，Trace 查找入口 | Console `TokenDiagnostics/index.tsx:555–738`、Monitor router |
| 完整性 | 对已知分支与调用计数对账，不把未知 Usage 当完整零值 | Monitor `query_service.py:8–49` |

重要区别：记录重放幂等不是物理请求跨层去重；输入条目采齐不是后端渲染上下文完整；采集完整性不是业务成功，也不是全平台采集覆盖率。

## 当前规范冲突：数据库

新增 SQL 与当前 AGENTS.md 数据库规范冲突，继续开发时应首先纳入整改范围：

- 五张表含 JSON 列：`scripts/sql/token_diagnostics_tables.sql:20,23,50,65,81,98`；DDL 未定义表/字段 COMMENT。
- Monitor 使用 `SELECT *`：`query_service.py:71,113,119,171,187,194`、`store.py:109,130,141`。
- 旧设计稿关于兼容 MySQL JSON 的表述（旧 spec 第 128 行）不能作为当前开发例外；需要同步修正文档。

不建议仅将 JSON 改成其他大文本列来绕过规则。应按已确认实体设计合规字段与明细表；用户已确认尚未建表，因此直接修订初始 DDL，不设计历史迁移。本文未执行数据库 DDL。

### Q7：表结构整改范围

五张现有 Token 表都需要整改 JSON 存储，并补表/字段 COMMENT。下表是拆分方向，尚非确定 DDL；复合数组/证据可用明细表表达，具体新增表数量待数据模型设计。

| 现有表 | 当前不合规列 | 结构化拆分方向 |
| --- | --- | --- |
| `swe_token_runs` | `payload`、`analysis_summary` | Agent/Chat/Session、分支角色/用途、执行关联及版本；预期子分支关系与规则评估状态/缺失字段明细 |
| `swe_token_model_calls` | `payload` | Usage 来源/状态、缓存/推理子项、结束原因、预算、输入估算与采集元数据 |
| `swe_token_input_items` | `payload` | 来源引用、producer 操作 ID、指纹密钥版本、字节量、归因质量 |
| `swe_token_execution_events` | `payload` | 工具操作 ID/名称、参数/结果 HMAC、终态、压缩周期及事件版本；按事件类型保留字段 |
| `swe_token_findings` | `payload` | 标题、建议、证据等级、观测数值与调用/操作证据引用明细 |

其中 calls/input_items/events 是观测数据，runs 同时承载分支及分析状态，findings 是已有五规则的结果。findings 在 A 中只做合规存储和现有功能兼容，不扩大为 B 的八规则/人工工作流。继续沿用独立 Token 表，不需要因这项整改修改既有 Trace/Span 表。

两侧 TokenStore、Monitor 查询和分析结果写入依赖 payload 解包，需要配套调整显式列读取/写入；只改 SQL 文件会破坏当前读写。本轮直接将未部署的初始 schema 改为合规结构，并同步部署文档。

## MVP 能力差距

| MVP 要求 | 当前缺口 | 开发前必须明确的前提 |
| --- | --- | --- |
| Task → Step → Call | 根 `task_run_id` 每请求生成 UUID，没有稳定业务 Task、完整 Step 树、task_type/cohort/outcome | 业务目标是否跨请求；Task 与 Run 的关系 |
| 跨 Gateway/SDK 去重 | attempt 的 `call_id` 为本地 UUID；没有共同 `physical_request_id`；汇总直接求调用记录之和 | 首批是否接入 Gateway；ID 传播及冲突来源优先级 |
| E01–E06 事件 | 自有 run/call/event 记录不等于统一事件协议；缺 envelope/schema/source 元数据 | SWE 内部扩展还是通用事件接入 |
| Context/Segment | 缺渲染阶段、tokenizer/template 标识、Token 偏移与跨轮片段身份 | 可取得的数据层及降级状态 |
| 前缀机会/首差 | 整片 HMAC 不能恢复最长公共 Token 前缀；缺兼容组、有序前缀指纹、时序窗口 | 后端兼容 Token 序列还是文本近似；不得互相冒充 |
| 工具安全属性 | 缺 read_only/side_effect/data_version/cacheable | 工具属性来自声明还是其他可靠来源；未知应保守降级 |
| 上下文操作 | 压缩仅记录 epoch/event，缺 before/after Context、删除片段和变换量 | 能否在装配/变换边界捕获 |
| 全局看板 | 缺任务分布、Agent 排行、潜在复用、全局覆盖率和结果已知率 | 可观测 Task 分母、样本范围与 cohort |
| 问题清单 | Finding 以 branch/rule/version/key 唯一；无跨任务根因聚合和人工反馈 | 问题聚合与单次命中身份；人工状态独立于自动分析 |
| Compare/策略库 | 无导入/配对、质量字段、策略实体与规则关联 | 可比群体、结果来源、首批八项策略 |

物理请求证据：`model_observer.py:118–121`；运行身份：`context.py:97–100`、`runtime.py:38–52`；求和：Monitor `query_service.py:12–13,97`；工具属性：`context.py:209–235`；前缀条件见 MVP 第 285–306 行。

## 规则映射：不能把旧五类直接改名为新八类

| 新规则 | 可复用部分 | 必须补足/改变 |
| --- | --- | --- |
| R01 上下文持续增长 | history_growth 的分组/增长证据 | 旧规则 ≥4 个逻辑调用只比较首尾；新要求 ≥3 次单调增长或窗口压力；并行分支仍需因果隔离 |
| R02 历史重复携带 | HMAC 与跨调用关联 | 旧只处理同 producer 的 tool_result、累计 ≥8K；新要求片段连续出现、末次 ≥5K 及占比 ≥25% |
| R03 工具结果过量 | 工具结果来源与估算 | 实际注入占比、步骤完成状态、持续携带轮数；原始工具返回量与注入量分离 |
| R04 同工具重复查询 | 工具/参数/结果指纹 | 只读、副作用、数据版本及额外调用计数；当前 repeat_tools 不能证明可缓存 |
| R05 可疑 Loop | repeat_tools 的连续候选序列 | Step/action/state/progress 信号及例外；不能将重复直接判定无价值 |
| R06 前缀机会不足 | 无现成前缀指标 | 兼容组、序列/文本级别、历史窗口、首差与覆盖状态 |
| R07 固定指令/Schema 大 | system/tools 来源和估算 | 固定内容范围、已归因分母、阈值与例外 |
| R08 同类模型异常 | 已知模型用量 | task cohort、复杂度、质量口径与 ≥30 样本；无事实来源时证据不足 |

旧 duplicate_injection/output_budget 没有一对一的新规则，是否保留为附加规则待决定。新旧规则需独立版本/标识和历史语义，不能复用 ID 却改变判断口径。新阈值是待校准启动值，不是普遍业务真理。

## 领域冲突与设计树

首批范围和 Q3–Q7 核心口径已经确认，相关领域定义已同步词汇。后续仍需明确入口身份映射、事件采集边界、覆盖分母与核对集合；B/C 的选择在相应阶段展开：

1. **已确认交付对象与首个里程碑**：SWE/Monitor 增量开发、A 观测底座；参见第一轮已确认范围。
2. **已确认成本主粒度**：每次根请求目标执行为一个 Task，具体入口关联与重连身份映射待设计。
3. **已确认观测身份原则**：Trace/Span 为可选关联，独立记账；事件版本与映射范围待设计。
4. **事实来源与验收**：保留 Usage 来源/状态与可验证范围，专门 Provider 逐请求真实核对暂缓；渲染 Token、缓存兼容信息、Task 分类/业务结果按后续能力边界展开，不能由 completed 推断业务 success。
5. **规则与覆盖**：旧规则保留方式、八类新规则降级条件、覆盖率分母、阈值配置及校准数据。
6. **人工闭环**：反馈属于聚合问题还是命中实例；Compare 的输入、配对与质量口径。自动重算不得覆盖人工确认。

设计依赖：交付边界 → Task/接入协议 → 数据可得性与降级 → 规则/指标/页面 → 反馈与比较验收。数据库规范是已有约束，不需要重新询问是否遵循。

## 交付顺序

- 先修正数据库规范冲突并确认 Task/Run、记录身份与事件版本；通过受控响应和隔离 MySQL 验证 SWE 写入、Monitor 查询/分析、完整性和既有功能兼容。
- 再补结构化 Context/Segment 与工具属性，建设规则和三个 P0 页面；前缀计算按可取得的真实数据分级，缺数据明确证据不足。
- 最后补问题反馈、八项策略与手工可比运行组；质量未知只报告资源变化。

第一项属于用户已确认的本轮 A 范围，具体实施分解见草案；第二、三项属于后续 B/C 范围。≤1% 真实用量偏差与专门 Provider/网关核对按用户最新要求暂缓，不将受控数字回归宣称为真实对账通过。

## 本轮验证与限制

- `venv/bin/python -m pytest tests/unit/token_diagnostics/ -q`：**54 passed**。
- Console `npm run test:run -- src/pages/Analytics/TokenDiagnostics/presentation.test.ts src/pages/Analytics/TokenDiagnostics/useResource.test.tsx`：**4 passed**，两个文件。
- GitNexus 已刷新，CLI status 指向 HEAD `458e80c` 且 covered files up-to-date。AttemptObserver context 证实 model_factory 接入，存在 ModelProxy 动态调用边界；create_branch 深度 2 upstream impact 为 LOW，三个直接调用者是 accounting_run、runtime.safe_call、LogicalObserver.__call__。未编辑这些符号。
- MCP 仍返回旧 freshness 缓存及异常字段，本轮最终结构证据使用 CLI。图的 process 覆盖受分析预算限制，没有找到完整 flow 不代表不存在调用。
- 未做真实 MySQL、Provider/网关账单对照或线上多实例联调；原提交的[校验记录](2026-10-11-token-diagnostics.md)也明确这些未验证项。
- 未改应用代码，未提交或推送。用户已有本地修改与方案文件保留。


## 本轮实施结果与验证（2026-10-12）

用户确认继续后完成观测底座 A 的代码改造：初始 DDL 为五张主表及六张类型化关系明细；两侧独立 Store 重建既有 DTO 与五规则证据。根执行按 scoped AnswerTurn / 实际 cron 执行归因，缺标识时独立生成；事件固定为 schema 1.0，Call 关联步骤和 messages_only Context，Usage 保留来源及未知状态。专门真实 Usage 核对继续暂缓。

独立复审发现并已修正三项：call/event 先于 Run 时占位分支保持 running 与未知结束；压缩事件不继承根请求开始时间；SQL 保留明确 null 的压缩开始时间。最终复审无剩余具体发现。

验证：Token 专项 70 项通过；扩展后端 115 项通过（SWE_TRACING_ENABLED=false，Token 工厂集成用例单独启用采集）；Console 诊断 4 项通过；变更 Python 的 Black、仓库 pre-commit 参数下 flake8 及 git diff --check 通过。没有执行整个仓库测试或全量 pre-commit。

GitNexus 完整重建后，含本轮新增文件的 detect_changes 返回 145 个符号、24 个变更文件，partial=false、truncated=false、风险 low、未解析出受影响流程。图索引自身仍有入口/分支预算和动态分派局限，结果不等于证明无下游影响；源码回归与独立审查构成补充证据。

真实 MySQL 往返尚未执行：本地 Docker daemon 无法连接，没有使用业务凭据。已提供 TEST_TOKEN_MYSQL_DSN 隔离 schema 的测试路径；默认关系往返使用 SQLite，不能代替真实 MySQL DDL 验证。本轮没有真实 Provider 调用、提交、推送或部署。
