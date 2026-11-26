# 独立 Agent 可观测设计：元数据日志、MySQL 与 NAS 正文

日期：2026-11-15
状态：设计待最终确认；Q1–Q25 已确认，尚未开始实现或执行建表。
决策：[ADR 0047](../../adr/0047-independent-agent-observability-uses-metadata-logs.md)、[ADR 0048](../../adr/0048-independent-observability-metadata-uses-mysql.md)。
背景：[能力蓝图](../../../Agent_可观测增强设计_Span与五大能力.md)、[逐轮决策](../../../.scratch/agent-observability/map.md)。本文为收敛后的实施设计入口；过程草案只用于核查证据。

视图对照修订（2026-11-15）：已核查 [Trace 视图方案](../../Agent_Observability_Trace_视图设计方案.md)。交互按该方案适配，领域/采集边界以本文与 ADR 为准；[差异清单](../../../.scratch/agent-observability/trace-view-review.md)记录存储、埋点与查询调整。原 worktree 已合入当前分支，交付物位于当前仓库同名路径。本轮只做分析与设计，不实施功能。

## 1. 已确认范围

本方案与 swe.tracing、AgentTraceSDK 及其埋点、配置、身份、输出、表、查询均无运行依赖；不升级、替换、迁移或兼容读取旧可观测。新增业务边界观测可位于同一业务代码，但不改旧埋点逻辑、异常/重试/流式行为。

| 范围 | 决定 |
|---|---|
| 应用 | SWE 内独立模块/API，Console 独立页面；不另起必须部署的服务 |
| 产出 | 专用 logger/有界队列，单行 JSON 写 stdout，仅元数据 |
| 入库 | 用户负责人工采集入库；本项目提供 schema、DDL、INSERT 示例、投影与查询，不设计/实现/验收采集器 |
| 存储 | 独立 MySQL 表/索引、独立连接配置；不增加 ES/OpenSearch |
| 清理 | 本期不做合法元数据清理、TTL、归档/分区淘汰任务 |
| 正文 | 只从现有 NAS 正文经业务定位授权读取，不新增正文副本 |
| 身份 | 沿用现有身份/manager/admin 判定；manager 当前 Source，admin 显式跨 Source；同范围可读正文 |
| 开关 | 单一全局开关，默认关闭；新 Run 准入快照，后续 Attempt/SubAgent 继承 |
| P0 执行 | 主对话、专家主执行、后台 SubAgent 基础关联与终态；Cron/独立 Workflow 放 P1 |
| 模型适配器 | 模型请求埋点覆盖 OpenAI 兼容协议，包含基于该协议的 Kimi 分支。移除产品内置的 Ollama、Anthropic 和 Gemini 专用适配器 |
| 质量 | 优先业务响应、允许有标记降级，不承诺 Span 零丢失 |
| 刷新 | 手动刷新/有界轮询，无新实时推送通道 |

全局开关工程默认采用 SWE_AGENTOBS_ENABLED=false，所有 Pod 统一后台环境配置，按现有发布/重启方式生效；不提供网页热切换和按 Source 覆盖。全局指此应用部署统一策略，不承诺跨 Pod 瞬时原子切换。数据库配置 SWE_AGENTOBS_DB_* 独立解析，不回退到业务/旧追踪连接；只读能力接口不暴露密码。日志输出不依赖数据库连接成功。

## 2. 链路与模块

```text
真实业务边界 → 独立 obs_context → 白名单元数据 → 有界日志队列 → stdout JSON
                                                        │
                                  用户负责采集并 INSERT inbox
                                                        ↓
独立 MySQL inbox → SWE 内投影任务 → events/runs/attempts/spans → 独立 API → Console
                                                                   │
                                              业务授权/元数据定位 → NAS 正文
```

建议新增 src/swe/observability/{context,models,emitter,config,instrumentation,projector,store,query,content,router}.py，以及 console/src/pages/AgentObservability/、独立 API/types。命名不强迫建立空文件，按实际职责合并小模块。通用 DatabaseConnection、现有身份工具、会话正文读能力可复用；旧可观测代码不可作为依赖。

采集端从业务装配挂新观察点：主请求受理/结算、Agent 连续执行、真正模型调用、模型层实际重试尝试、实际工具执行、guard/审批、SubAgent 启动/worker 结算。新观察失败只记安全计数，不能改业务参数、吞异常、重复调用模型/工具或延迟取消。

## 3. 领域身份与状态

- Observability Run：一次根用户问题从受理至终态。新问题或已终态后的重执行新建 Run，可记录 predecessor；重连不建 Run。
- Attempt：同 Run 的一次连续 Agent 执行。Agent 层重新执行/审批恢复新建 Attempt；模型层 retry 不换 Attempt。
- Span：真实操作区间。业务类型与技术 kind/operation 分开；只在实际发生且值得计时时生成。
- 新 run/attempt/span/event ID 使用 UUIDv4 小写规范格式，自主管理，不读取或映射旧 Trace ID。业务定位保留 service_unit/tenant/source/agent、Chat、根 question_msgid 等，原值按字节精确匹配。
- 后台 SubAgent 沿用根 obs_run_id，以独立 Agent 子操作和业务 subagent_run_id 标识自身生命周期，不另造会重开根 Run 的第二套根结算。

| 对象 | 状态 | 结算边界 |
|---|---|---|
| Run | accepted/running/waiting_approval；succeeded/failed/cancelled/timed_out/rejected | 终态不可重开；没有明确业务原因不推断超时/失败 |
| Attempt | running；succeeded/failed/cancelled/timed_out/waiting_approval | waiting_approval 为该连续片段结束，不是 Run 终态 |
| Span | running；succeeded/failed/cancelled/timed_out/rejected | 子失败可被恢复，不自动传播为根失败 |
| 数据质量 | unknown/partial/unresolved/conflict，加具体原因 | 与业务状态独立，缺日志不等于业务失败 |
| 正文 | available/pending/not_saved/compacted/unavailable | 无权/不存在对外一致，不能泄露存在性 |

**审批特殊规则**：现有审批提示会让业务 Turn 正常 completed，批准后新建业务 Turn；新 Run 必须保持 waiting_approval，不能照抄 completed。pending 及 worker 启动载体带新增 obs_context：schema、根 Run、根问题、capture_enabled、run_started_at、前驱 Attempt 与本次审批 request_id。仅元数据，不带正文/权限凭证。连续审批沿关联链保留根问题，不按 session 最新消息猜。独立上下文不等待人工日志已经入库。attempt_index由单问题执行所有者在obs_context中推进，重复批准沿既有业务幂等复用同Attempt；后台worker不分配根Attempt序号。

审批记录本身丢失/重启不可恢复、或日志关联不全时，标 unresolved/unknown，不擅自恢复业务或补判失败；后续独立问题可新开 Run，不被旧观测等待阻塞。明确拒绝、过期、取消按实际业务结果结束。后台子任务晚结束只更新子操作及后台概况，根 Run 终态和结束时间不变。

## 4. 元数据事件协议 v1

UTF-8 单行 JSON；schema_version=1，命名空间 agentobs。完整单行最大 24 KiB；先白名单投影再按序列化字节预算裁剪可选元数据，保留必需身份和生命周期。必需字段不可保留时丢事件并计数，不输出无效 JSON。数组默认不超过32项，嵌套深度不超过4；截断用明确标志/原因，不截 JSON 字符串中间。

| 字段组 | 内容 |
|---|---|
| 固定头 | schema_version、event_id、event_name、occurred_at、producer_id（进程启动身份） |
| 身份 | obs_run_id、适用时 obs_attempt_id/obs_span_id、parent_obs_span_id |
| 生命周期键 | entity_kind、entity_id、phase_key、local_seq；序号只在拥有者局部有效，不是跨 Pod 全局时钟 |
| scope | service_unit_id、source_id、tenant_id、agent_id；缺业务作用域显式不支持，不填假身份 |
| 业务引用 | chat_id、root_question_msgid、run_started_at、capture_enabled（每个Run生命周期事件都带）、当前问题/turn、approval_request_id、subagent_run_id（适用时） |
| 操作 | span_type、operation、kind、受控 name、step_index、start/end、业务 status、technical_status、has_error、reason_code、attempt ordinal |
| 度量 | duration_us、首有效chunk/首可见文本延迟、已知tokens、loop/tool计数；未知 null |
| 关系/资产 | 安全 Events、Links、cause/recovery、可信模型/Tool/Agent/Skill/Prompt ID与版本/签名 |
| 质量 | start/end证据、metadata_truncated、context_missing、usage_known 等独立信息 |

事件名：run.accepted/run.state/run.ended、attempt.started/attempt.ended、span.started/span.ended/span.event、approval.requested/approval.resolved、subagent.started/subagent.ended、asset.used、observer.health。observer.health为全局诊断元数据，允许无Run/scope，使用进程启动observer实体UUID与health序号；仅admin能读取全局诊断，manager不得查跨Source健康事件。其它生命周期事件必须完整Run/scope，不能借可空列绕过校验。结束事件携带操作起始时间、终态、完整有界汇总，即使 start 丢失也可展示 partial。每个事件固定生命周期槽：(entity_kind,entity_id,phase_key,local_seq)，重传槽与event_id保持；不同event_id重复同槽不重复计数，同槽冲突标记而非最后到达获胜。

元数据禁止正文：问题/回答、messages/prompt、工具参数/结果/说明/schema、检索Query/片段、URL query、文件文本、原始异常消息/堆栈、人工自由文本理由、密钥。错误只记受限 error.type/phase/code 与 cause/recovery ID。名称来自注册/配置且限长，不接受用户输入或任意 repr。当必须正文解释时查 NAS；NAS 没保存就不可用。

**类型兼容**：13类业务类型注册显示；P0真正埋 Agent/LLM/Tool、存在的guard/审批阶段及SubAgent基础节点。Skill只有资产使用则为事件；无真实RAG/Workflow生命周期不造空节点。未知业务类型受限显示Custom；未知属性不任意透传，支持版本内安全字段可保留，非法/未知载荷隔离为安全原因。原蓝图“未知字段不丢”不得突破元数据边界。

## 5. 日志与资源边界

专用logger不挂swe handler，不继承普通日志级别/队列/formatter；propagate=false，异步线程单写stdout。默认队列4096条且总预算8MiB，包含in-flight；满则非阻塞丢事件并累加安全计数，后台有机会时输出observer.health。关闭最多等待1秒排出并报告未确认数量；进程强杀仍可能丢失，不能保证计数摘要本身必达。

这些是可配置工程初值，不是性能SLA。不开逐token日志、不每chunk序列化累计正文；只在内存保存少量计时/Usage元数据，按Provider累计快照取最终Usage不逐帧相加。关闭采集的新Run不深拷贝/投影正文，不尝试连接观测DB。已开启Run的后续Attempt和SubAgent继续继承快照。

## 6. MySQL、入库与投影

设计基线 MySQL 8.0.21+、InnoDB DYNAMIC、16KiB页和strict mode；实际MySQL/TDSQL必须实库验收。DDL由人工预建，应用只检查readiness，不启动时CREATE/ALTER。时间UTC DATETIME(6)，业务身份VARBINARY精确值，新UUID为ASCII CHAR(36)，扩展为应用校验TEXT，不存正文、不设TTL。

[DDL草案](../../../.scratch/agent-observability/schema-draft.sql) 定义五表：

| 表 | 写入者 | 核心用途/索引 |
|---|---|---|
| swe_agentobs_inbox | 用户INSERT，投影器更新状态 | payload_text；(processing_state,ingest_id)领取pending |
| swe_agentobs_events | 投影器 | event_id主键、生命周期槽唯一；按run/实体、scope/时间查安全Events |
| swe_agentobs_runs | 投影器 | scope/时间、根问题定位、独立Run结算、质量 |
| swe_agentobs_attempts | 投影器 | Run+Attempt序号/自身ID，连续执行及恢复关系 |
| swe_agentobs_spans | 投影器 | Run+开始/父节点、类型/模型/工具/错误受限筛选 |

用户最小INSERT只带payload_text，数据库默认生成ingest_id、received_at、pending；[事件样例](../../../.scratch/agent-observability/events.example.jsonl) 给出对应JSON。用户无需维护派生表或投影版本。重复导入inbox允许，规范events层幂等。

投影器是现有SWE生命周期内独立后台任务，独立连接池与事务，不继承业务事务。READ COMMITTED下每批最多32条pending，SELECT ... FOR UPDATE SKIP LOCKED，逐行保存点校验/归并；处理和标processed同事务。失败回滚仍pending，不用MAX(id)游标永久跳过晚提交的小ID。坏事件清空payload仅留安全原因和ID；这是入库校验处置，不是合法元数据按龄清理。规范化成功后inbox.payload_text置NULL，安全事件仅在events保存一份；inbox保留元数据处理记录。槽内容比较排除event_id和入库字段，比较其余规范语义；相同槽换event_id的重传不重计。

事件上下文必须携带根run_started_at和capture_enabled，用于accepted丢失时创建partial根视图，不以子Span开始时间冒充根开始。缺Attempt自身事实时保留Span的attempt引用并标关联未齐，不伪造Attempt序号/起止。全局observer.health仅写规范events而不创建runs。

同一Run归并先创建/锁runs行，固定加锁顺序run→attempt→span；唯一槽与event_id同时校验，scope/根问题/父身份不可变。重传不重复加tokens/调用数；汇总基于唯一操作终态重算/差量替换，不能盲加每条end。并发死锁整批有界重试；冲突终态质量标conflict，保留事实并从明确业务结算校正，不按入库先后选择真相。子操作晚到不回滚根终态。

MySQL锁队列用法依据官方[Locking Reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)；索引按[最左前缀规则](https://dev.mysql.com/doc/refman/8.0/en/multiple-column-indexes.html)设计。SKIP LOCKED只用于待处理队列，不用于用户需要一致结果的列表查询。具体隔离级别/超时/死锁须实库验证，当前未执行DDL。

## 7. 正文与统计的事实边界

NAS保存已提交会话/恢复消息，不保证每次原始模型请求：临时hint不持久化，工具结果可已压缩。读取经service_unit/tenant/source/agent/Chat/消息occurrence定位MySQL publication，再pin/限量读NAS；不用日志绝对路径、不扫文件找最近消息。在线与归档精确定位均需支持，现有answer-turn只查在线不能冒充完整归档解析。每次鉴权/删除检查及展示脱敏，正文不回写日志或新数据库，不新增原始下载。

统计分三类，API和UI始终标population/window/generated_at：

1. 当前可查询的业务受理问题/Turn/SubAgent数量与状态，只读业务元数据，不受日志采样影响，但不等于逻辑Run全量分母。
2. 可确认关联/结算的逻辑Run成功率；审批归并缺失的unresolved单列，不混入成功率分母，不承诺删除后不可变全历史审计。
3. 已观测的操作/性能分布，标span数量、start/end完整率、Usage已知覆盖；无日志业务问题标not_collected，不伪造Run身份。

业务DB与独立观测DB/NAS没有跨库原子事务；各自读取/投影截至信息显式返回。日志未出现无法区分未采集/未入库/丢失时标unknown，不给虚假的精确采集延迟。

## 8. 五大能力与指标口径

| 能力 | P0 | 降级或P1 |
|---|---|---|
| 过程可观测 | Run/Attempt、真实操作树/瀑布、Events/Links、13类型注册兜底、SubAgent基础状态 | Cron/独立Workflow/P1专项实际埋点；缺父节点标orphan，不猜树 |
| 结果可解释 | 已保存问题/回答/工具正文证据与业务定位、采集范围显示 | RAG答案片段→文档/版本/Chunk只有真实映射才显示；不证明内部推理或答案正确 |
| 过程可追溯 | 实际模型/Provider、Agent/Tool/Skill/Prompt的可信ID/版本/签名 | 外部修订不可得为unknown；版本反查增强/P1，不用当前覆盖历史 |
| 异常可定位 | error分类/phase、cause、recovery、已恢复子错误与根结果分离 | 原始敏感异常文本不进入新日志；无因果证据不强称根因 |
| 性能可分析 | 时长P50/P95/P99、TTFT/首可见文本、Token覆盖、AgentLoop、实际Tool次数/失败率与类型热点、丢弃计数 | TPOT/关键路径/CPU分解、价格成本/质量评测/跨执行对比为P1 |

Run elapsed含审批等待，active time为Attempt活动区间并集；操作duration取本地单调计时。模型逻辑调用与底层运行时重试尝试分别统计，Provider内部不可见HTTP重试不冒充已观测请求。首有效chunk从实际逻辑模型调用到非空输出chunk，首可见文本另计；空启动/usage-only不算输出。stream无输出时延迟null，非流式不伪造TTFT。Usage取提供方真实终态/最新累计值，未知不填0，推理Token不是默认额外加到输出总量。

父子时长不相加，热点按操作类型分组并说明可能并行，P95用明确样本集nearest-rank，不平均各组P95。不采用隐蔽抽样：初值每次聚合最多读50000个满足过滤的已结束样本；超出时返回range_too_large提示缩小窗口，不默默抽前N条。取消、失败、成功的耗时分布可分开筛选，已恢复失败的工具仍计工具失败但不计Run失败。

## 9. 新 API 与页面

Console 首版已确认 manager 和 admin 都在当前 Source 内完成从列表到证据的排查。交互及当前已实现的 API 映射见 [Swe Agent 可观测页面交互设计](2026-11-23-agent-observability-console-interaction.md)。

前缀/agent-observability；所有数据API先应用现有身份判定和服务端manager当前Source/admin显式Source过滤，普通用户403，越界详情/正文与不存在统一404。admin跨Source须明确all_sources或列表，默认不自动全库。service_unit由后台允许范围约束，不让客户端任意指NAS根。

- GET /capabilities：全局配置快照、只读可用性，不返回连接秘密。
- GET /runs、GET /runs/{id}：分页列表、摘要/关系/Attempt。
- GET /runs/{id}/spans、GET /runs/{id}/spans/{span_id}：共享行模型、事件/Links/安全Raw、资产和性能。
- GET /runs/{id}/content：以受控证据键查询已保存正文，默认32KiB、最多256KiB，每次鉴权；截断/分页明确。
- GET /overview：业务总体/确认Run/观测性能分别给口径和质量。

游标携带版本/过滤指纹/授权scope/稳定最后键，用MAC防改，scope每次重验；不复用业务会话cursor的秘密。列表默认24h、50条，最多200条；单次范围最多31天但可明确选择任意历史窗口，不清理数据。Span分页默认200、最多1000，旧页已加载节点不宣称整树完整，缺父提供安全祖先摘要或标orphan。服务查询默认5秒超时，超限返回可重试/缩小范围，不影响Agent。

Console使用独立“Agent可观测”入口：上方筛选/数据质量，执行列表，详情共享Tree+Waterfall行模型，Inspector含Overview、Evidence、Assets、Errors、Performance、Events/Links/Raw。总览嵌在同页面，不铺大量孤立页面。尝试/审批/后台状态各自可见。手动刷新及运行/等待或已知后台仍运行时5秒轮询，页面隐藏暂停、所有已知执行终态后停止自动轮询，可手动查晚到日志。

外观以console/DESIGN.md为唯一规范，不照搬第三方主题。真实Chromium108与当前浏览器验证独立滚动、长列表、键盘选择、1280/1440宽度和hideMenu=true；窄屏Inspector改抽屉。不得无回退使用subgrid/Popover；滚动条需webkit样式，保持原有壳层语义。

## 10. Trace 视图适配：存储、埋点与读模型

### 10.1 Run、Attempt 与视图中的 Trace

沿用已确认领域：一次问题一个 Run，连续执行一个 Attempt，模型重试不创建新 Attempt。列表每行一个 Run，详情内以 Attempt 导航执行树；视图稿中的 Trace 仅作为所选 Attempt 的执行链路视图称谓，不增加 trace_id、Trace 表或 OTel/旧 SDK 运行依赖。Run/Attempt/Span 的深链接使用已有新 ID。审批等待在 Attempt 之间独立显示；SubAgent 基础节点保留启动 Attempt 关联，可晚于该 Attempt/Run 结束。可同时显示根终态与后台运行态。

页面路径采用 `/agent-observability/runs`、`/agent-observability/runs/{run_id}?attempt={attempt_id}&span={span_id}`，不依赖旧 Monitor 页。项目筛选映射服务端授权 Source，不新增 project 身份。默认 Run 总览，展开所选 Attempt 的真实 Span；Run/Attempt 导航条是 UI 结构，不伪造持久化 Span。父子需同 Run，跨 Attempt 的恢复关系使用 Link；未确认父关系不能按时间、名称或 step 猜。

2026-11-23 已确认从列表打开详情默认展示 Run 最终状态和最近一次已观测的 Attempt。进行中的 Run 展示当前状态，带 Attempt 或 Span 的深链接优先恢复指定位置。

### 10.2 五表增量与投影字段

保留五表，新列/索引均已进入 [DDL 草案](../../../.scratch/agent-observability/schema-draft.sql)，尚未执行：

- runs：增加 `span_count`、`error_span_count`，以及 Source+Session、Source+Agent+时间、Source+duration+Run 索引。精确 Session 查找保留原值字节比较；不做前缀检索。
- spans：增加受控 `name`（128字符）、可空 `step_index`、`technical_status`、`has_error`；增加 Run+Attempt+时间和 Run+Attempt+has_error+时间索引。
- events：增加 Run+Span+时间+事件ID索引，Inspector 不扫描整条 Run 的 metadata。关系/资产/正文证据仍为白名单元数据，不新增关系表、正文表或任意属性索引。

`status`仍是业务结果；`technical_status=UNSET|OK|ERROR|null`是本方案自己的技术枚举，常见名称不意味着 OTel 依赖。`has_error=1`需明确技术错误证据（独立 ERROR、受控 error.type 或异常事件），0仅在有明确无技术错误结算时设置，未知为null。失败业务结果、拒绝或取消不自动等于技术错误；已恢复错误仍可定位，不把父节点标成ERROR。has_error按已确认错误证据合并，后到成功事实不擦除已恢复错误；相互矛盾的结算按既有conflict规则处理。

Run的span_count/error_span_count统计已规范投影的唯一Span及has_error=1的Span，包括明确的后台节点；非完整时为已观测下界，不是全部业务操作数。`metadata.summary_quality`按指标返回`source=observed_spans|owner_summary`、`coverage=observed|complete_for_owner|unknown`、`as_of`、Usage已知覆盖与截断。只有有计数事实才显示0；无事实为null。执行所有者提供的完整摘要与已收到Span数并列，不能混合成虚假的全量统计；晚到Span可更新计数，不重开根Run。Attempt摘要由run_id+attempt_id受限聚合或其可信摘要得到，不另增表。

所有查询仍从已授权Run验证完整scope，模型/工具跨Run筛选用授权Run集合关联spans，不以全局model/tool索引代替鉴权。name/step/关系/模型身份的不同已知值按冲突处理；缺值可由后到相同实体事实补齐。事件中的受控step/model/tool/evidence字段按组存入Span的metadata，不能复制完整事件历史让派生metadata超过24KiB；增长的Events/Links只保留有界摘要并从events分页。

请求模型与响应模型是不同字段；二者不同不构成冲突。model_key从请求模型回退值提升为已知响应模型时允许更新，并保留model_key_source=request_fallback|response；只有同一实际请求的同一身份字段出现互斥已知值才标conflict。一个逻辑模型容器跨请求尝试时不把多个实际模型硬合成一个身份，实际模型按请求Span展示。

### 10.3 埋点字段与事实边界

聊天请求获取工作区时立即继续执行。正文证据恢复服务的初始化和工作区登记由后台完成，登记失败由后台重试。恢复服务就绪且当前工作区登记成功后，新产生的正文证据按现有规则关联。登记前已经持久化且可验证的证据信封仍可恢复，未留存关联的证据记录为 `not_saved`。模型元数据采集独立于正文证据登记。

结束事件仍须自包含开始时间、操作身份、父关系、技术/业务结果及本操作有界摘要，开始丢失也能显示partial。同Span重复包装只生成一个事实槽；保持旧埋点及业务调用次序不变。以下是字段设计，不代表项目现在已经产出这些字段：

| 白名单组 | 采集内容与缺失语义 |
|---|---|
| 通用 | name来自注册/固定operation，不含命令/SQL/参数；step_index为所属Agent执行片段内的循环序号，可空；并行不靠序号伪造全序 |
| LLM | provider_key、request_model_key、response_model_key、finish_reason枚举、流式标志、逻辑调用ID、本层request_retry_index；model_key优先实际响应模型，否则请求模型并标来源 |
| 模型重试 | 逻辑模型调用与本项目真实可见的请求尝试用独立Span/受控operation区分，重试仍同Attempt；llm_calls只统计逻辑调用，request attempts另列，Token只取唯一真实请求Usage一次，不再叠加容器摘要；不可见Provider内部HTTP重试不填伪值 |
| Tool | tool_key、tool_call_id、独立tool_execution_id、executor_kind、timeout_budget_ms、retry_index、已知exit_code；一次真实执行一个execution ID，审批replay可同call ID但不同execution ID，不记录输入输出正文 |
| SubAgent | 业务task/run key、启动父Span、启动Attempt、sync/async、执行Agent身份及终态；同一业务子操作的started/ended复用观测Span ID；worker内部细粒度树不自动升为P0承诺 |
| Context/Approval/Checkpoint | 实际压缩前后tokens/策略枚举、approval_request_id/决议枚举/等待区间、checkpoint_id/动作枚举；仅真实存在时产出，缺测为null，不为满足图标创建假Span |
| Resource | 独立采集的service_name/service_version、deployment_environment、producer_id；受控配置值且限长，不透传环境变量集、任意Header或OTel Resource |
| 安全错误/因果 | error.type、error_phase、reason_code、cause/recovery ID和relation_type；不采集异常消息、堆栈或用户自由文本 |
| 正文证据 | evidence_ref含当前Chat/Turn/消息身份、layer、真实occurrence定位或可唯一解析的publication/block业务引用、tool_call_id（适用时）、evidence_role、capture_scope；只存身份，不存文本/文件路径 |

新增通用事件名`span.event`：用于单个retry_scheduled、recovered、evidence.linked、link.added等受控事实；使用`entity_kind=span`、entity_id=obs_span_id、phase_key=event、local_seq由该Span唯一拥有者推进。专门的审批/SubAgent事件仍用其原事件名和独立槽，不通过第二个span.event重复累计同一事实。Events/Links/资产不全部塞入span.ended数组；单事件数组仍最多32项，可分页独立事实，结束摘要给出已发事实数及截断标记。独立事实丢失时不由摘要伪造内容。

Span实体生命周期和span.event序号由一个拥有者产生；worker需要独立事件序列时分配其自己的Span，通过Link关联，不让两个进程抢同一槽。跨进程传递Span身份和不可变父关系；owner摘要不具备全链路送达证明。v1尚未实施，本次补充直接更新v1草案；后续已发布协议变更才按版本兼容规则升级。

SubAgent启动/结束允许显式所有权交接：启动方独占started槽，启动快照传递同一Span和不可变元数据，worker独占ended槽；两者不得同时生成同一phase/seq的事实。该例外不允许各进程分别从0生成同一Span的span.event序列；worker自己的增量事实用独立实体/Span再关联。worker结束的自包含摘要继续携带原始安全name/parent/started_at；启动方不得再生成互相竞争的结束裁决。启动失败且尚未交给worker的情况由启动方结算，不能双重拥有。

投影映射固定：business_ref.session_id→runs.session_key，model.response_model_key（已知）否则request_model_key→spans.model_key，tool.tool_key→spans.tool_key，subagent.business_subagent_key→spans.business_subagent_key；scope.agent_id始终是根Run授权身份，worker执行身份单独保留。顶层name/step_index/technical_status/has_error与DDL同名映射；其余允许组存入metadata，evidence_refs不在派生行无限累积。span.event不增加span_count或llm/tool调用数，不凭单个非区间事实创建带假开始时刻的Span；生命周期未到时事件保留并标unresolved。

事件先于Span到达时，后续Span生命周期投影需重新关联该Span已有安全事件，补齐错误/证据/关系摘要；不能只处理本批新事件而永久遗漏先到事实。投影结果由规范事实集合决定，与入库顺序无关；摘要仍受字节预算约束。

IO Tab映射为NAS正文证据，区分`input|output|tool_call|tool_result`与`capture_scope=saved_conversation_fragment`，从不声称完整原始模型请求。消息ID或tool_call_id不保证唯一；定位冲突返回unresolved，不能按最近消息猜。正文尚未持久化时pending，未保存not_saved，已压缩compacted；无权/不存在对外统一不可用。真实保存后可追加evidence.linked，不能让观测等待NAS提交或改变已有持久化语义。Raw仅显示校验后的安全元数据，不附正文、原inbox或异常堆栈。不存在的Prompt/Stacktrace不从普通业务日志旁路补齐。

### 10.4 列表、树和Inspector读契约

| 入口 | 补充契约 |
|---|---|
| GET /runs | time、source、agent、业务status、min_duration_us、精确run_id/attempt_id/session_id；ID查询先授权并保留scope；精确Run/Attempt深链接不因默认24h而被隐藏 |
| 排序/计数 | 默认(started_at,run_id)倒序；可选(duration_us,run_id)倒序，只查已结束且duration已知；运行中耗时单列elapsed不参与终态排序；默认has_more，无无界总COUNT |
| GET /runs/{id} | Run/Attempt摘要、root_span_id若真实存在、首技术错误定位、known_span_count、quality和服务器observed_at；超多Attempt也分页，不无限嵌套 |
| GET /runs/{id}/spans | attempt_id过滤，按(started_at,span_id)稳定分页；支持error_only、受控name/operation/tool/model精确或字面子串q；不查正文/任意attributes，不开放SQL/正则 |
| GET /runs/{id}/spans/{span_id} | 单节点受控详情、分页祖先摘要/必要路径、关系和指标；返回attempt_id以定位正确Attempt，不受已加载首屏限制 |
| GET /runs/{id}/events | 可选span_id/attempt_id，按(occurred_at,event_id)分页；生命周期事件和span.event/专用事实统一列表，seq只保证拥有者顺序，跨进程同刻不声称因果先后 |

列表搜索框显式选择Run ID、Attempt ID或Session ID，完整匹配，不把三个命名空间混猜；Span ID由已知Run的深链接/节点入口定位。视图原稿的Trace ID输入在本方案改为Attempt ID，不接受旧SDK Trace ID。Inspector的Events/Links/Assets/Evidence各自继续分页，可用受控view参数过滤已校验事件种类/关系投影；父事件数组与独立事实按事实ID或固定关系键去重，不因多个Tab重复展示而重复累计。未知资产/证据数量不声明完整，预算耗尽显式提示继续或缩小范围。

列表主扫描和排序索引与组合过滤不可能全部同时最优；每请求默认最多扫描50000个候选行、5秒，超出返回range_too_large，不返回有偏的“完整结果”。候选预算在应用分批读取实际主索引候选时累计，不能仅凭LIMIT保证扫描有界；实际DB执行仍有超时约束。聚合沿用50000已结束样本上限。管理员显式跨Source按授权范围分页归并，游标携带每个范围的继续位置及排序/filter指纹，限制Source个数为32，超过提示缩小范围；不能直接无范围全表排序。duration排序的候选时间过滤仍要验EXPLAIN/容量，不预先承诺响应时间。

Span默认200/最多1000，事件默认100/最多500；单次响应JSON建议最大1MiB，按实际字节提前分页且has_more，不能一次塞入1000×24KiB详情。行列表只给身份/父关系/受控name/类型/时刻/状态/小指标，大属性和正文按需读。深链接、首/上下个错误以及搜索由服务端在选定Attempt全集内定位后返回节点及祖先；错误按(started_at,span_id)确定顺序，首错误不等于根因。祖先每页最多64个并返回继续键；用visited检测循环，超过预算显示质量提示，不悄悄截断当Root。

“Parent missing”只在同一授权Run/Attempt内定点查询仍不存在父时显示；父只是没加载标parent_not_loaded，过滤没命中保留ancestor_context。匹配/错误计数标明覆盖、是否已扫描完；首200条不是全树。2,000节点用多页窄行集合加共享虚拟行模型；20/200/2000三档验收必须覆盖末页错误、末页搜索、跨页祖先。只对加载完整且时长已知的集合计算慢节点前10%，未齐时仅允许明确阈值筛选。

轮询返回projected_at/observed_at，不以最晚时间保证此前事件全到达。新节点可能插入已读游标之前；每次刷新需重建当前查询分页遍历，合并时按ID去重，后端不许只用started_at大于上次游标做增量。P0不承诺跨多次HTTP的原子快照，API标read_consistency=eventual；有投影变更提示时重读已加载页，未完整刷新要明确。根已结束但已知后台仍运行时仍允许有界轮询；所有已知执行终止后停止，手动刷新查询未知晚到事实。

### 10.5 时间、摘要与视图范围约束

持久化UTC DATETIME(6)，API给UTC带微秒字符串及相对所选视窗的offset_us；不向JavaScript传纳秒epoch Number。单调duration_us与墙钟起止不一致时分别保留并标clock_skew，end早于start等异常不改写成正常耗时。统一窗口取已知起止边界并包含后台晚结束；进行中延长至服务端observed_at，未知/缺结束用unknown/partial样式，不能仅因无end永远宣称仍在业务执行。浏览器可外推显示估计elapsed但不作为业务裁决。

首有效chunk/首可见文本沿用第8节的真实定义；Provider未证明为首token时UI不将time_to_first_chunk标成精确TTFT。请求模型与实际模型分别展示。成本仍是P1：没有执行时价格版本、币种与实际用量口径，本期隐藏成本列或显示unavailable，不按现价估算。

Run 摘要按唯一模型请求尝试分别累加已获取的输入和输出 Token。两次请求中仅一次返回 20 个输入 Token 时，摘要显示 20。部分覆盖时标明数值为已观测用量。某项完全缺失时，API 返回 `null`。请求详情逐字段展示已知值，真实零值显示 `0`，缺失显示「未获取」。单次请求的 Token 合计在输入与输出均已获取时显示。

请求级 Usage 上线前的历史 Run 如缺少请求级用量证据，相应的 Token 字段在 API 中返回 `null`，Console 留空。按已有请求级证据展示已知值，输入和输出分别判断。历史逻辑调用上的用量摘要不用于补算请求级用量，缺失值不计作零。

视图的Tool命令预览、完整Prompt、原始异常堆栈、下载原文件、全部OTel attributes不是本期承诺。Loop/Context/Checkpoint支持安全类型化呈现，但只对项目真实边界埋点。Transcript、关键路径、原生OTel接入/导出、价格成本、反馈评测和新全文搜索不由本次视图对照自动进入P0。Chromium108与hideMenu兼容要求保持不变。

## 11. 实施顺序与验收

模型请求埋点修复先完成 OpenAI 兼容适配器的请求事实采集、请求侧业务隔离和 MySQL 投影。首批 Console 交付 Run 摘要与单次模型请求详情，用同一条执行链路验收从模型调用到页面展示。跨 Run 性能总览中的模型请求次数、用量覆盖和首输出延迟分布安排在后续批次。

1. 独立协议/全局配置/上下文/输出：关闭零正文工作量、队满不阻塞、取消不变、JSON白名单/UTF-8限额；旧可观测无导入依赖。
2. MySQL DDL/readiness/inbox投影：样例导入、重复/不同ID同槽/冲突、乱序、并发、晚提交小ID、事务回滚；先实际数据库验DDL/索引。
3. 主请求/模型/工具/审批/SubAgent观察点：正常链、未请求模型的短路、内部retry、审批跨Turn/重复批准、流中取消、背景晚结束、新旧开关组合独立。
4. 读API/NAS/统计：manager/admin/普通用户、scope冲突、归档/已删除/未保存/压缩/迟提交、Usage缺失、已确认分母/unresolved；数据库不可用不影响业务。
5. Console与容量报告：查询计划、P50/P95/内存/CPU、节点数和数据量有实测记录；真实Chromium108+当前浏览器验收。生产TPS/全局采集延迟不编造，采集不在验收内。

本期不交付：采集代理/部署日志路由、旧追踪迁移/修复、ES、TTL/清理、SSO改造、新实时推送、模型完整Prompt副本、确定性重演、未确认的计费和评测。最终共同理解确认后才实施。
