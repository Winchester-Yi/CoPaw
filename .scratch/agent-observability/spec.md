# 独立 Agent 可观测：元数据日志与 NAS 正文设计

Status: implemented-pending-site-verification
Type: grilling
日期：2026-11-15
阶段：本轮剩余埋点已提交，现场验收待完成。见[功能完成报告](followup-completion-report.md)与[测试报告](followup-test-report.md)。

**最终评审入口**：[独立 Agent 可观测实施设计](../../docs/superpowers/specs/2026-11-15-independent-agent-observability-design.md)。本文件保留代码核查证据；实现以最终评审通过的设计为准。
输入：[能力蓝图](../../Agent_可观测增强设计_Span与五大能力.md)；[决策树](map.md)；[ADR 0047](../../docs/adr/0047-independent-agent-observability-uses-metadata-logs.md)。

## 1. 已确认边界

**这是独立方案，不是现有追踪体系的升级、改造、迁移、适配或合并。**

- 新建独立埋点、上下文、事件契约、开关、日志输出和查询展示逻辑。运行时不导入、不调用、不消费 swe.tracing、AgentTraceSDK 或其已有埋点与数据。既有逻辑保持原样，本方案不以旧 SDK 是否安装、启用或验收为发布前提。
- 新埋点只输出元数据日志。正文不进入该日志，不创建正文采集开关；正文展示权限与可观测开关分离，展示时按业务定位从已有 NAS 正文存储授权读取。
- 第一版面向授权 Source 运营人员和系统管理员。Source 范围、跨用户、跨 Source 和正文权限由服务端控制。
- P0：主对话、专家主执行、后台 SubAgent 的基础关联与终态。Cron 与独立 Workflow 为 P1。
- Observability Run 是一次用户问题从受理至终态的逻辑单元，不是整个会话或 Goal。
- 新详情上报优先保护业务响应，允许显式降级，不承诺所有 Span 零丢失。受理数/终态由只读会话和 SubAgent 业务元数据确定，日志明细与性能必须标采集覆盖。
- 在当前 SWE/Console 内交付独立后端模块、API、页面，不新增必须独立部署的服务。专用 logger/队列、单行 JSON 写 stdout。
- 元数据使用独立MySQL表与索引、独立连接配置，不引入ES/OpenSearch；本期不做数据清理，不设置TTL。
- Attempt边界、manager/admin及同范围正文权限已确认，沿用项目已有身份判定；只有一个全局开关，默认关闭并在新Run准入时固定采集快照。
- 用户负责日志采集与入库操作；本项目不设计、实现或验收部署侧采集方案，但负责独立索引/存储设计、入库数据契约、查询/API/展示。
- 前轮“以 SDK 为事实源、兼容读取旧事件、关联两条追踪链、改现有 chat 埋点/内容策略、迁移旧数据”的建议全部撤回。新功能放在 SWE/Console 独立模块与页面，不接旧 Monitor/Tracing 接口。

这里的独立指与既有可观测体系没有运行依赖。新埋点必须位于真实业务边界，也需要业务身份及 NAS 内容定位；不重新实现 Agent、会话持久化或工具执行器。对业务代码的新增观测调用不能改变已有埋点的装饰器、次序、返回、异常、重试或取消语义。

## 2. 方案骨架

```text
主对话 / 专家主执行 / SubAgent worker
  → 新增独立业务边界埋点
  → 独立上下文与元数据投影
  → 有界、非阻塞的元数据日志输出
  → 用户负责采集入库（按本项目契约）
  → 本项目设计的独立元数据存储与索引
  → 新可观测查询和聚合接口
  → 新执行列表 / 树 / 瀑布 / Inspector

正文请求
  → 服务端 Source / Tenant / Agent / 用户 / Chat 授权
  → 业务定位解析与已提交元数据
  → NAS 受限正文读取、展示脱敏
  → Inspector 正文展示（不回写日志）
```

OTel/GenAI 名称只作概念与字段语义参考，不表示采用任何 OTel SDK、Exporter、Collector 或既有追踪身份。是否需要标准协议导出不是 P0 前提。日志中的 Span 是独立事件还原出的操作区间。

## 3. 需要遵守的项目事实

本次为静态源码核查，没有连接生产网关/日志平台/NAS，没有负载或浏览器验收。仓库根无 .codegraph，按项目约定使用 rg。以下为本轮代码快照行号。

### 3.1 NAS 内容与定位

| 事实 | 证据 | 对本方案的约束 |
|---|---|---|
| 用户问题先 durable publish 再执行 | src/swe/app/conversation_storage/runner_integration.py:72-129 | 可以用已受理问题作为业务锚点，不以新日志收到时间认定受理成功 |
| 正文来自 agent.state_dict，拆出 memory/task/error 等消息 | runner_integration.py:223-242,251-289；session_projection.py:13-18,50-104 | NAS 是会话/恢复事实，不是每次 Provider 原始请求审计库 |
| 运行中正文按语义边界和批次发布 | semantic_runtime.py:20,36-60,124-169 | 执行日志可能先到，正文尚未提交；pending 和 permanently unavailable 不同 |
| 临时 hint 可只存在于本次 reasoning，并刻意不持久化 | src/swe/agents/react_agent.py:2115-2133 | 不能从 NAS 承诺重构每次真实完整模型输入；媒体重试也可能改变输入 |
| 工具结果写 memory 前可已压缩 | src/swe/agents/tool_output_budget_mixin.py:25-26,40-105 | 只能展示已保存内容，不能称 Provider/工具原始全量正文 |
| scope 含 service_unit/tenant/source/agent；session 再加 user/channel/session | conversation_storage/identity.py:56-85,108-130 | 日志自身携带足够作用域，不能只靠 session 或 msgid |
| 原始 Msg.id 可能多次 occurrence | conversation_storage/message_installs.py:35-51；GLOSSARY 的 Message Occurrence | 精确工具结果/单条消息须有 occurrence/tool_call 等关联，不只按名称或时间猜 |
| 读取经 MySQL locator 和 pin 再读 NAS extent | conversation_storage/history.py:209-227,370-423 | 从 NAS 查正文仍要先解析已提交业务元数据；不能绕过 publication 直接扫文件 |
| 普通 /chats/answer-turn 有 owner/source/agent 授权，但只查在线消息 | runner/api.py:763-791,1500-1545；history.py:646-717,728-781 | 可研究业务读取能力；不能把当前用户 API 当运营跨用户授权。归档精确定位要另补独立正文解析流程 |
| 归档用业务 history API 分页 | runner/api.py:1676-1700；history.py:527-598 | 不能在点击 Span 时无界扫描全部归档来碰运气 |
| NAS durable 在 MySQL commit 前，删除立即撤销可见性 | publications.py:416-446,881-905,987-1022；collection_queries.py:52-56 | 文件还在不代表可展示；日志存留不延长正文访问权；读取需检查最新授权/删除状态 |

上述未写完整路径的 conversation_storage 文件位于 src/swe/app/conversation_storage/；runner/api.py 位于 src/swe/app/runner/。

**关键缺口**：仅新增元数据日志、正文只查现有 NAS 时，不能展示 NAS 从未保存的完整模型请求、临时提示、未保留中间响应或原始全量工具结果。UI 应显示“未保存/不可用/已压缩/尚未提交”，不能用最终会话拼装后声称原始请求。该能力边界已由 Q9 确认。

### 3.2 普通应用日志与部署

- src/swe/utils/my_logging.py:119-179,308-358：普通 swe logger 有自己的全局异步队列，实际写 stderr；仅命名 swe.xxx 会继承级别与队列，不构成独立输出链。
- 同文件 :337-358：setup_logger 会清理直接挂在 swe logger 的额外 handler；新专用输出不能通过改全局 formatter/普通日志初始化实现。
- deploy/config/supervisord.conf.template:14-21 把应用 stdout/stderr 重定向 /var/log/app.out.log、app.err.log；默认镜像未证明“写 stdout 即可进入集中日志”。
- 仓库未发现通用日志搜索 API，也没有核实生产采集/索引/检索协议及保留策略。实例操作记录接口不是运行时日志搜索。

建议新建独立 logger/队列/级别/formatter/生命周期，propagate=false，元数据事件使用单行 JSON 与固定 schema。Q8 已确认使用 stdout。本项目只验证正确、单行、有界的输出及其对业务的影响；不修改 Supervisor/采集器，不验证生产采集链。最终采集与入库由用户负责。

## 4. 独立事件契约草案（未定稿）

建议命名空间为 agentobs，事件 schema 固定版本，所有事件显式白名单投影，不透传任意 Python 对象或异常 repr。

| 类别 | 建议字段 / 规则 |
|---|---|
| 协议 | schema_version、event_id、event_name、occurred_at_utc、instance/process、进程启动身份 |
| 新身份 | obs_run_id、obs_attempt_id、obs_span_id、parent_obs_span_id；新生成并自主管理，不读取旧 trace/span ID |
| 业务作用域 | service_unit_id、source_id、tenant/user、agent_profile_id、chat_id、question_msgid，适用时 channel/session 与 SubAgent run ID |
| 生命周期 | run/span start/end、operation/type、业务状态、原因枚举、link/relationship；迟到/重复事件按自有 ID 处理 |
| 度量 | 本地单调时钟时长、首有效输出/可见文本时间、逻辑模型调用/物理尝试计数、真实 Usage（未知为 null） |
| 资产 | 模型/provider 标识、Skill/Tool/Agent 的可信版本/digest；不存在的修订为 unknown |
| 正文关联 | message/occurrence/tool_call 等业务引用，内容种类、已知保存状态；不暴露 NAS 绝对路径作为权限凭证 |
| 数据质量 | 采集缺失/丢弃/上下文缺失/未结束/Usage未知/正文不可用，各自原因，不合并为一个 complete 布尔值 |

禁止写入：用户问题/回答、prompt/messages、工具 schema 描述/参数/结果、检索 Query/片段、文件正文、URL query、原始异常消息/堆栈、人工自由文本理由、密钥。工具/模型注册名和错误类型仍需受限长度与字符处理，防止正文或用户自由文本借字段逃逸。

Span 生命周期建议用 start/end 两种元数据事件，区分运行中与已结束；完整性、乱序与事件丢失不能仅靠根事件到达推断。业务 ID 可作查询关联，不作无界指标标签。Q12 已确认由会话/SubAgent 业务元数据提供受理与终态事实；仍需按业务支持范围标明总体。

## 5. 生命周期与正文展示建议

- Run 锚定一个用户问题；技术自动重试、审批重放与重新连接分别建模，不以每次 HTTP 或每次聊天轮询自动新建 Run。Q10 已确认终态不可重开；终态后重执行新建 Run 并关联前驱。
- 模型元数据在实际模型调用边界生成，逻辑调用覆盖内部 retry，物理尝试独立计数；无模型请求的审批/计划短路不增加推理调用数。新增观测逻辑独立实现，直接在真实业务调用前后记录；不得以旧 SDK/tracing wrapper 的状态或回调作为事实来源，已有调用栈继续原样工作。
- Tool 记录实际执行；审批、策略判定、等待是独立事件/操作。结构化失败不只看 Python 异常；恢复成功不自动判根失败。
- SubAgent 在启动快照中传新的观测上下文，worker 显式绑定；启动成功不等于子任务完成，父 Run 结束后子任务仍可独立报终态。P0 只承诺基础关联与终态。
- Skill 是资产使用时只记使用事件/资产引用，没有真实独立调度生命周期就不造执行时长；RAG 等同理。
- 正文 resolver 先鉴权，再解析已提交业务定位；同时覆盖在线与归档的精确定位，不直接给浏览器 NAS 路径、不按旧 Trace ID 查正文。
- metadata Raw 也只返回本方案的白名单元数据。正文每次读取重新检查授权/删除；服务端脱敏，不写回日志、浏览器持久化或新的内容仓。
- “尚未提交”“已删/无权”“从未保存”“压缩后内容”“读取失败”要可辨别，同时不向无权用户泄露存在性。

## 6. 五大能力的 P0 边界

| 能力 | P0 建议交付 | 不应承诺 |
|---|---|---|
| 执行可观测 | 新 Run、操作树/瀑布、实际模型/工具、重试与取消、SubAgent 基础关联 | 由旧事件补全、未埋路径自动可见 |
| 结果可解释 | 对应问题/回答/已保存工具消息的授权正文与关联标记 | 完整模型内部思考、NAS 未保存输入、自动证明答案正确 |
| 过程可追溯 | 执行时真实模型/资产身份与可信修订，业务正文定位 | 用当前版本冒充历史、虚构版本、历史确定性重演 |
| 异常可定位 | 错误类型/阶段/原因枚举、失败发生点、恢复与最终结果 | 在日志存敏感异常原文以补诊断 |
| 性能可分析 | 明确边界的时长、调用数、TTFT/可见文本延迟、Usage已知覆盖 | 缺数据补零、累加父子重叠、采样日志冒充全量分位/成功率 |

Q7 已确认 SWE/Console 独立模块及页面；Q11 已确认人工采集入库，索引/存储设计和其余查询展示由本项目负责。P1 扩展 Cron/独立 Workflow 及真实存在的 RAG/Memory/Human 专项证据。UI 须遵守真实 Chromium 108 与当前浏览器、适用 hideMenu=true 的验收要求。

## 7. 验证顺序

1. 独立性：新模块不导入旧可观测体系，新旧开关任意组合互不影响；新日志关闭不构建正文或改业务返回。
2. 事件正确性：正常/拒绝/内部 retry/取消、父子/跨进程、重复/迟到/缺事件；只允许白名单字段，敏感正文注入测试不得出现在日志。
3. NAS 展示：作用域/owner/operator 授权、在线/归档 occurrence、待提交/已删除/被压缩/从未保存、多 Pod 业务定位。
4. 本项目交付链路：stdout JSON 契约测试 + 按契约准备的入库测试数据→索引/存储→新查询→UI；验证乱序、重复、缺失、队满与进程退出。不把部署侧采集纳入任务或验收。
5. 指标：明确统计总体、已采集覆盖、时间窗与分位算法；业务元数据若用于分母则验证覆盖范围，不宣称日志零丢失。

尚未确认量级和性能预算，本期不做数据清理，不能把默认参数写成达标证据。

## 8. 存储与索引交付

[独立存储与查询设计](storage-and-query-design.md) 已拆出项目/人工入库边界、原始事件/查询视图、精确检索模式、去重/乱序与内容引用约束。数据库已确定为独立MySQL，本期不做元数据清理；生产采集不在任务内。

## 9. 生命周期与API细化

[生命周期与API草案](lifecycle-and-api-design.md) 记录真实审批Turn的completed与新Run等待状态的差异、Attempt/子代理/上下文载体、独立API和验收边界。obs_context载体和独立审批状态已确认；全局开关与既有身份模式已同步。当前不据此修改业务代码。

## 10. 后续决策

见 [当前决策前沿](map.md)。本轮已将独立方案、元数据/正文边界和 Run 定义写入全局词汇表，将架构取舍写入 accepted ADR 0047。开发实施须等设计树收敛并确认；当前只进行用户已授权的文档分析与设计。
